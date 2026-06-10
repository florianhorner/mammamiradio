"""Segment production pipeline for music, banter, and ad breaks."""

from __future__ import annotations

import asyncio
import copy
import datetime
import logging
import os
import random
import re
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx

import mammamiradio.hosts.scriptwriter as _sw
from mammamiradio.audio.audio_quality import AudioQualityError, AudioToolError, validate_segment_audio
from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.audio.normalizer import (
    concat_files,
    crossfade_voice_over_music,
    generate_bumper_jingle,
    generate_silence,
    generate_station_id_bed,
    generate_tone,
    humanize_norm_filename,
    load_track_metadata,
    mix_oneshot_sfx,
    mix_quiet_bleed,
    mix_voice_with_bed,
    mix_voice_with_sting,
    normalize,
    probe_duration_sec,
    reconcile_cached_music,
    save_track_metadata,
)
from mammamiradio.audio.tts import synthesize, synthesize_ad, synthesize_dialogue
from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import (
    AdHistoryEntry,
    ChaosSubtype,
    InterruptSpec,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.home.ha_context import (
    ENTITY_LABELS,
    GOLD_ENTITIES,
    HomeContext,
    check_reactive_triggers,
    fetch_home_context,
    push_state_to_ha,
)
from mammamiradio.hosts.ad_creative import _cast_voices, _pick_brand, _select_ad_creative
from mammamiradio.hosts.context_cues import generate_impossible_line
from mammamiradio.playlist.downloader import (
    download_track,
    evict_cache_lru,
    is_rejected_cache_key,
    reject_cached_download,
    validate_download,
)
from mammamiradio.playlist.playlist import fetch_chart_refresh
from mammamiradio.playlist.track_rationale import classify_track_crate, generate_track_rationale
from mammamiradio.scheduling.scheduler import next_segment_type

logger = logging.getLogger(__name__)
CHAOS_AUDIO_FAILURE_BACKOFF_SECONDS = 0.5
CHAOS_AUDIO_FAILURE_LIMIT = 5

MUSIC_SELECTION_RETRIES = 20
MUSIC_QUALITY_GATE_REJECTION_LIMIT = 3
CACHE_EVICTION_INTERVAL_SECONDS = 3600
PLAYLIST_REFRESH_INTERVAL_SECONDS = 5400.0


@dataclass(frozen=True)
class RenderedMusicTrack:
    track: Track
    path: Path
    cache_path: Path
    cache_hit: bool


def _probe_segment_duration(path: Path) -> float:
    """Run ffprobe on path and return duration in seconds; 0.0 if probe fails."""
    return probe_duration_sec(path) or 0.0


def _is_tmp_render(segment: Segment, tmp_dir: Path) -> bool:
    if segment.ephemeral:
        return True
    try:
        return segment.path.resolve().is_relative_to(tmp_dir.resolve())
    except OSError:
        return False


def _unlink_if_tmp_render(segment: Segment, tmp_dir: Path) -> None:
    if _is_tmp_render(segment, tmp_dir):
        segment.path.unlink(missing_ok=True)


def _normalized_cache_path(track: Track, config: StationConfig) -> Path:
    return config.cache_dir / f"norm_{track.cache_key}_{config.audio.bitrate}k.mp3"


async def _render_music_track(
    track: Track,
    config: StationConfig,
    *,
    temp_prefix: str,
    context: str,
    cache_write_required: bool = False,
) -> RenderedMusicTrack | None:
    """Download, validate, normalize, and cache one music track."""
    audio_path = await download_track(track, config.cache_dir, music_dir=Path("music"))
    loop = asyncio.get_running_loop()
    ok, reason = await loop.run_in_executor(None, validate_download, audio_path)
    if not ok:
        reject_cached_download(config.cache_dir, track.cache_key, reason)
        logger.warning("Skipping %s track due to invalid download (%s): %s", context, track.display, reason)
        return None

    norm_cached = _normalized_cache_path(track, config)
    if norm_cached.exists():
        logger.debug("Normalization cache hit%s: %s", f" ({context})" if context else "", norm_cached.name)
        # A cache hit skips normalize() + its reconcile pass, so a file produced
        # before reconciliation existed would air at its old level. Reconcile it on
        # hit (off the event loop) so every song lands at the target; skipped once
        # the sidecar marks it done, so steady-state cache hits stay instant.
        await loop.run_in_executor(None, reconcile_cached_music, norm_cached)
        return RenderedMusicTrack(track=track, path=norm_cached, cache_path=norm_cached, cache_hit=True)

    norm_path = config.tmp_dir / f"{temp_prefix}_{uuid4().hex[:8]}.mp3"
    _norm_fn = partial(normalize, audio_path, norm_path, config, loudnorm=True, music_eq=True)
    await loop.run_in_executor(None, _norm_fn)
    try:
        await loop.run_in_executor(None, shutil.copy2, str(norm_path), str(norm_cached))
    except OSError as exc:
        logger.warning(
            "Normalization cache write failed%s %s -> %s: %s",
            f" ({context})" if context else "",
            norm_path,
            norm_cached,
            exc,
        )
        if cache_write_required:
            norm_cached.unlink(missing_ok=True)
            norm_path.unlink(missing_ok=True)
            raise
    else:
        save_track_metadata(norm_cached, track.title, track.artist)
    return RenderedMusicTrack(track=track, path=norm_path, cache_path=norm_cached, cache_hit=False)


async def _queue_drain_recovery_bridge(
    queue_segment: Callable[[Segment], Awaitable[bool]],
    state: StationState,
    config: StationConfig,
) -> bool:
    """Queue the best available continuity bridge when active playback drains."""
    fallback = _pick_canned_clip("banter", state=state) or _pick_canned_clip("welcome")
    if fallback:
        logger.warning("Queue empty during active playback — inserting canned clip as bridge")
        return await queue_segment(
            Segment(
                type=SegmentType.BANTER,
                path=fallback,
                metadata={
                    "type": "banter",
                    "canned": True,
                    "queue_drain_recovery": True,
                    "title": "Recovery banter",
                },
            )
        )

    norm_files = sorted(config.cache_dir.glob("norm_*.mp3"))
    if norm_files:
        norm_path = norm_files[0]
        _meta = load_track_metadata(norm_path) or {}
        logger.warning(
            "Queue empty during active playback — inserting norm-cache bridge: %s",
            norm_path.name,
        )
        return await queue_segment(
            Segment(
                type=SegmentType.MUSIC,
                path=norm_path,
                metadata={
                    "title": _meta.get("title") or humanize_norm_filename(norm_path.name),
                    "artist": _meta.get("artist", ""),
                    "queue_drain_recovery": True,
                    "audio_source": "norm_cache",
                },
                ephemeral=False,
            )
        )

    tone_path = config.tmp_dir / f"drain_tone_{uuid4().hex[:8]}.mp3"
    logger.error("No canned clips or norm cache available — inserting emergency tone bridge")
    try:
        await asyncio.to_thread(generate_tone, tone_path, 440, 2.0)
    except Exception:
        logger.exception("Emergency tone bridge generation failed")
        return False
    return await queue_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=tone_path,
            metadata={
                "title": "Station continuity",
                "artist": "",
                "queue_drain_recovery": True,
                "audio_source": "emergency_tone",
            },
            ephemeral=True,
        )
    )


def _banter_title(script: list[dict] | None, *, canned: bool, host_order: list[str] | None = None) -> str:
    """Produce a user-facing label for a BANTER segment.

    Prefers unique host names from the script (joined with ' & '). Falls back
    to "Pre-recorded banter" when the audio came from a canned clip with no
    script attached, and finally to a generic label. The goal is that queue
    rows never render a bare "banter" type name to operators or listeners.

    host_order pins the display order to the config host list so adjacent
    segments always show the same canonical ordering regardless of which host
    the LLM chose to open with.
    """
    if canned:
        return "Pre-recorded banter"
    hosts: list[str] = []
    for line in script or []:
        name = (line or {}).get("host", "").strip() if isinstance(line, dict) else ""
        if name and name not in hosts:
            hosts.append(name)
    if hosts and host_order:
        rank = {h: i for i, h in enumerate(host_order)}
        hosts.sort(key=lambda h: rank.get(h, len(host_order)))
    if hosts:
        return " & ".join(hosts[:2])
    return "Banter"


def _expected_banter_duration_sec(texts: list[str]) -> float | None:
    """Conservative floor for generated multi-line host exchanges."""
    if len(texts) <= 1:
        return None
    word_count = sum(len(re.findall(r"\w+", text)) for text in texts)
    return 0.8 * max(len(texts) * 2.0, word_count * 0.22)


def _ad_title(brands: list[str] | None) -> str:
    """Produce a user-facing label for an AD break.

    One brand → brand name. Multiple brands → "BrandA +N more". Empty list
    falls back to "Ad break" so the row never shows the bare "ad" type.
    """
    clean = [b.strip() for b in (brands or []) if b and b.strip()]
    if not clean:
        return "Ad break"
    if len(clean) == 1:
        return f"Ad: {clean[0]}"
    return f"Ad: {clean[0]} +{len(clean) - 1} more"


async def _record_motif(state: StationState, track, config=None, *, listen_duration_s: float | None = None) -> None:
    """Record a completed streamed track in persona memory and play history."""
    persona_store = getattr(state, "persona_store", None)
    if not persona_store:
        return
    try:
        await persona_store.record_motif(track.artist, track.title)
        # Also record to play_history for cross-session anthem/skip detection
        yt_id = getattr(track, "youtube_id", "") or ""
        if yt_id:
            await persona_store.record_play(
                yt_id,
                persona_store._session_id,
                skipped=False,
                listen_duration_s=listen_duration_s,
            )
            if config:
                from mammamiradio.playlist.song_cues import detect_anthem

                db_path = config.cache_dir / "mammamiradio.db"
                persona_cfg = getattr(config, "persona", None)
                anthem_t = persona_cfg.anthem_threshold if persona_cfg else 3
                await detect_anthem(db_path, yt_id, threshold=anthem_t)
    except Exception:
        logger.warning("Failed to record motif", exc_info=True)


async def _maybe_start_session(state: StationState) -> None:
    """Check if this is a new listening session and increment the counter."""
    persona_store = getattr(state, "persona_store", None)
    if not persona_store:
        return
    if persona_store.maybe_new_session():
        await persona_store.increment_session()
        persona = await persona_store.get_persona()
        logger.info("Listener session #%d started", persona.session_count)


# Directory for pre-bundled banter and ad clips that ship with the package.
# These provide station personality on day 1 without an Anthropic API key.
_DEMO_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "demo"

# SFX assets (alert jingle used as interrupt bridge audio).
_SFX_DIR = Path(__file__).resolve().parent.parent / "assets" / "sfx"

# Global cooldown for interrupt firing — kept separate from per-entity
# spec.cooldown so a timer configured with cooldown=300 doesn't suppress a
# different timer's interrupt for 5 minutes.
_GLOBAL_INTERRUPT_COOLDOWN_SECONDS = 60


# Tracks the most recent music file to avoid repeated glob scans on every banter.
_last_music_file: Path | None = None

_MUSIC_TYPES = {SegmentType.MUSIC}
_SPEECH_TYPES = {
    SegmentType.BANTER,
    SegmentType.NEWS_FLASH,
    SegmentType.AD,
    SegmentType.STATION_ID,
    SegmentType.SWEEPER,
    SegmentType.TIME_CHECK,
}


def _set_last_music_file(path: Path) -> None:
    """Update the cached last music file (called after each music segment)."""
    global _last_music_file
    _last_music_file = path


def _remember_rendered_music(rendered: RenderedMusicTrack, state: StationState) -> None:
    """Remember a playable music path, even if cache copy failed."""
    path = rendered.cache_path if rendered.cache_path.exists() else rendered.path
    _set_last_music_file(path)
    state.last_music_file = path


def _latest_music_file(tmp_dir: Path) -> Path | None:
    """Return the most recently written music_*.mp3, using cached path when available."""
    if _last_music_file and _last_music_file.exists():
        return _last_music_file
    # Fallback: scan directory (only on first call or after cache invalidation)
    files = list(tmp_dir.glob("music_*.mp3"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _get_last_music_file(state: StationState) -> Path | None:
    """Return a playable last-known-good music file for recovery paths.

    Prefers the state-attached path (test-isolatable, per-session); falls back to
    module-level cache when state hasn't tracked one yet.
    """
    candidate = state.last_music_file
    if candidate and candidate.exists():
        return candidate
    if _last_music_file and _last_music_file.exists():
        return _last_music_file
    return None


def _make_imaging_lib(config: StationConfig) -> ImagingLibrary:
    """Construct a station ImagingLibrary from config."""
    return ImagingLibrary(
        config.sonic_brand.motif_notes,
        config.tmp_dir,
        bed_volume_db=config.imaging.bed_volume_db,
        assets_dir=Path(config.imaging.assets_dir) if config.imaging.assets_dir else None,
    )


def _crosses_music_speech_boundary(prev_type: SegmentType, next_type: SegmentType) -> bool:
    return (prev_type in _MUSIC_TYPES and next_type in _SPEECH_TYPES) or (
        prev_type in _SPEECH_TYPES and next_type in _MUSIC_TYPES
    )


def _segment_type_from_value(value: object) -> SegmentType | None:
    if isinstance(value, SegmentType):
        return value
    if isinstance(value, str):
        try:
            return SegmentType(value)
        except ValueError:
            return None
    return None


def _initial_previous_segment_type(queue: asyncio.Queue[Segment], state: StationState) -> SegmentType | None:
    """Infer the last audible segment when producer starts after prewarm/playback."""
    queued = list(getattr(queue, "_queue", ()))
    if queued:
        return queued[-1].type
    now_type = _segment_type_from_value(state.now_streaming.get("type"))
    if now_type is not None:
        return now_type
    if state.current_track is not None:
        return SegmentType.MUSIC
    return None


def _front_insert_queue_and_shadow(
    queue: asyncio.Queue[Segment], state: StationState, segment: Segment, shadow_entry: dict
) -> bool:
    """Air an operator-triggered segment NEXT instead of behind the buffered
    lookahead. Synchronously drains the queue, puts the segment at the front, and
    repushes — no await between draining the real queue and updating the shadow, so
    the streamer cannot interleave (mirrors ``_purge_queue_and_shadow`` and the
    ``/api/queue/remove`` critical section). Drops the furthest-future tail if the
    bounded queue would otherwise overflow ``maxsize`` (which would raise QueueFull
    and risk dead air); dropped renders are re-produced on a later cycle. Returns
    False (dropping the segment) if the session was stopped mid-build.
    """
    if state.session_stopped:
        if segment.ephemeral:
            segment.path.unlink(missing_ok=True)
        logger.info("Discarding forced %s because the session is stopped", segment.type.value)
        return False
    items: list[Segment] = []
    while not queue.empty():
        try:
            items.append(queue.get_nowait())
            queue.task_done()
        except asyncio.QueueEmpty:
            break
    items.insert(0, segment)
    dropped: list[Segment] = []
    while queue.maxsize and len(items) > queue.maxsize:
        dropped.append(items.pop())  # furthest-future first
    for item in items:
        queue.put_nowait(item)
    # Shadow mirrors the real queue: prepend the new entry, then drop the same
    # number of tail entries (never the new front one) so the one-directional drift
    # guard never has to "correct" a shadow > queue overshoot and log a false alarm
    # on every air-next.
    state.queued_segments.insert(0, shadow_entry)
    for seg in dropped:
        if getattr(seg, "ephemeral", False):
            seg.path.unlink(missing_ok=True)
    if dropped and len(state.queued_segments) > 1:
        drop_n = min(len(dropped), len(state.queued_segments) - 1)
        del state.queued_segments[len(state.queued_segments) - drop_n :]
    # The operator's pick is now queued — the trigger is fulfilled. Clearing the
    # in-flight guard HERE (not at render-start) is what makes "one at a time" hold
    # through a slow render: a second tap stays rejected until this pick airs, so it
    # can never be front-inserted ahead of it.
    state.operator_force_pending = None
    logger.info(
        "Air-next: front-inserted %s%s",
        segment.type.value,
        f" (dropped {len(dropped)} buffered tail segment(s))" if dropped else "",
    )
    return True


async def _apply_talk_bed(
    audio_path: Path,
    config: StationConfig,
    state: StationState,
    *,
    prefix: str,
) -> Path:
    """Mix a quiet music bed under a generated spoken segment."""
    loop = asyncio.get_running_loop()
    last_track = _get_last_music_file(state) if config.imaging.use_music_queue_for_beds else None
    bed_path = config.tmp_dir / f"{prefix}_bed_{uuid4().hex[:8]}.mp3"
    duration = await loop.run_in_executor(None, _probe_segment_duration, audio_path)
    imaging_lib = _make_imaging_lib(config)
    bedded_path = config.tmp_dir / f"{prefix}_bedded_{uuid4().hex[:8]}.mp3"
    try:
        await loop.run_in_executor(None, imaging_lib.pick_talk_bed, duration, bed_path, last_track)
        await loop.run_in_executor(
            None,
            mix_voice_with_bed,
            audio_path,
            bed_path,
            bedded_path,
            config.imaging.bed_volume_db,
        )
    except Exception:
        bedded_path.unlink(missing_ok=True)
        bed_path.unlink(missing_ok=True)
        raise
    finally:
        bed_path.unlink(missing_ok=True)
    audio_path.unlink(missing_ok=True)
    return bedded_path


async def _try_crossfade(
    voice_path: Path,
    config: StationConfig,
    output_path: Path,
    tail_seconds: float = 8.0,
    music_fade_volume: float = 0.5,
) -> Path:
    """Attempt to crossfade voice over the last music file. Returns voice_path on failure."""
    last_music = _latest_music_file(config.tmp_dir)
    if not last_music or not last_music.exists():
        return voice_path
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            crossfade_voice_over_music,
            last_music,
            voice_path,
            output_path,
            tail_seconds,
            1.0,
            music_fade_volume,
        )
        voice_path.unlink(missing_ok=True)
        logger.info("Crossfade over %s", last_music.name)
        return output_path
    except Exception as exc:
        logger.warning("Crossfade failed, using standalone: %s", exc)
        output_path.unlink(missing_ok=True)
        return voice_path


async def _synthesize_impossible_moment(
    line: str,
    config: StationConfig,
    state: StationState,
) -> Path:
    """Synthesize an impossible-moment line via TTS with crossfade. Raises on failure."""
    host = random.choice(config.hosts)
    imp_path = config.tmp_dir / f"impossible_{uuid4().hex[:8]}.mp3"
    await synthesize(
        line,
        host.voice,
        imp_path,
        engine=host.engine,
        edge_fallback_voice=host.edge_fallback_voice,
        state=state,
    )
    xfade_out = config.tmp_dir / f"impossible_xf_{uuid4().hex[:8]}.mp3"
    audio_path = await _try_crossfade(imp_path, config, xfade_out)
    state.last_banter_script = [{"host": host.name, "text": line, "type": "impossible"}]
    return audio_path


_recently_played_clips: deque[str] = deque(maxlen=50)

# Cache directory listings for demo asset clips (avoid repeated glob on every call).
_canned_clip_cache: dict[str, list[Path]] = {}

SHAREWARE_CANNED_LIMIT = 3


def _pick_canned_clip(subdir: str, *, state: StationState | None = None) -> Path | None:
    """Pick a pre-bundled clip from assets/demo/{subdir}/, avoiding recent repeats.

    For banter clips, respects the shareware trial limit: after SHAREWARE_CANNED_LIMIT
    clips have been streamed to the listener, returns None to force TTS fallback.
    Welcome clips are not subject to the limit.
    """
    # Shareware gate: stop serving canned banter after the trial limit
    if subdir == "banter" and state and state.canned_clips_streamed >= SHAREWARE_CANNED_LIMIT:
        logger.info("Shareware limit reached (%d clips streamed), forcing TTS", state.canned_clips_streamed)
        return None
    if subdir not in _canned_clip_cache:
        clip_dir = _DEMO_ASSETS_DIR / subdir
        _canned_clip_cache[subdir] = list(clip_dir.glob("*.mp3")) if clip_dir.is_dir() else []
    clips = _canned_clip_cache[subdir]
    if not clips:
        return None
    # Avoid recently played clips
    eligible = [c for c in clips if c.name not in _recently_played_clips]
    if not eligible:
        _recently_played_clips.clear()
        eligible = clips
    pick = random.choice(eligible)
    _recently_played_clips.append(pick.name)
    return pick


async def _prefetch_next(
    state: StationState,
    config: StationConfig,
    _failed_keys: set[str] | None = None,
) -> None:
    """Pre-normalize the predicted next music track into the norm cache.

    Fires as a background task immediately after a music segment is queued so
    that slow-hardware normalization (~75s on Pi) completes during the current
    track's playback (~3-4 min) rather than after the queue drains.

    Uses a non-mutating peek: finds the first track outside the repeat-cooldown
    window without calling select_next_track (which has weighted-random side
    effects). Falls back to playlist[0] if all tracks are in cooldown.
    Non-fatal — any failure is swallowed after a DEBUG log.

    _failed_keys: caller-owned set; on failure the candidate's cache_key is added
    so the caller can skip it on the next cycle, preventing repeated retries of
    the same broken track on slow hardware.
    """
    norm_path: Path | None = None
    candidate_key: str | None = None
    try:
        if not state.playlist:
            return
        cooldown = config.playlist.repeat_cooldown
        recent_keys = {t.cache_key for t in list(state.played_tracks)[-cooldown:]}
        candidate = next(
            (
                t
                for t in state.playlist
                if t.cache_key not in recent_keys and (_failed_keys is None or t.cache_key not in _failed_keys)
            ),
            state.playlist[0],
        )
        candidate_key = candidate.cache_key
        if _failed_keys is not None and candidate_key in _failed_keys:
            return  # all candidates have failed — nothing useful to prefetch
        norm_cached = _normalized_cache_path(candidate, config)
        if norm_cached.exists():
            logger.debug("Prefetch: norm already cached for %s", candidate.display)
            return
        if is_rejected_cache_key(candidate.cache_key):
            logger.debug("Prefetch: skipping denylisted candidate %s", candidate.display)
            return
        logger.info("Prefetch: pre-normalizing %s in background", candidate.display)
        rendered = await _render_music_track(
            candidate,
            config,
            temp_prefix="prefetch",
            context="prefetch",
            cache_write_required=True,
        )
        if rendered is None:
            logger.debug("Prefetch: skipping invalid download for %s", candidate.display)
            return
        norm_path = None if rendered.cache_hit else rendered.path
        logger.info("Prefetch: cached norm for %s", candidate.display)
    except asyncio.CancelledError:
        logger.debug("Prefetch task cancelled")
        raise
    except Exception as exc:
        logger.debug("Prefetch failed (non-fatal): %s", exc)
        if _failed_keys is not None and candidate_key is not None:
            _failed_keys.add(candidate_key)
    finally:
        if norm_path is not None:
            norm_path.unlink(missing_ok=True)


async def prewarm_first_segment(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
) -> bool:
    """Pre-produce one music segment at startup so audio is ready before any listener connects.

    Returns True if a segment was queued, False on failure (non-fatal).
    """
    if not state.playlist:
        return False
    if state.session_stopped:
        logger.info("Skipping prewarm: session is stopped")
        return False
    try:
        track = state.select_next_track(
            repeat_cooldown=config.playlist.repeat_cooldown,
            artist_cooldown=config.playlist.artist_cooldown,
        )
        logger.info("Pre-warming first track: %s", track.display)
        rendered = await _render_music_track(track, config, temp_prefix="music", context="prewarm")
        if rendered is None:
            return False
        loop = asyncio.get_running_loop()
        norm_path = rendered.path
        if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
            try:
                await loop.run_in_executor(None, validate_segment_audio, norm_path, SegmentType.MUSIC)
            except AudioToolError as exc:
                logger.warning("Audio tool unavailable, skipping prewarm quality check: %s", exc)
            except AudioQualityError as exc:
                logger.warning("Prewarm quality gate rejected track (%s): %s", norm_path.name, exc)
                if not rendered.cache_hit:
                    norm_path.unlink(missing_ok=True)
                return False
        rationale = generate_track_rationale(track, source=state.playlist_source, listener=state.listener)
        crate = classify_track_crate(track, state.playlist_source)
        segment = Segment(
            type=SegmentType.MUSIC,
            path=norm_path,
            metadata={
                "title": track.display,
                "artist": track.artist,
                "title_only": track.title,
                "youtube_id": track.youtube_id,
                "spotify_id": track.spotify_id,
                "album_art": track.album_art,
                "duration_ms": track.duration_ms,
                "rationale": rationale,
                "crate": crate,
                "audio_source": "prewarm",
            },
            ephemeral=not rendered.cache_hit,
        )
        segment.duration_sec = await loop.run_in_executor(None, _probe_segment_duration, norm_path)
        await queue.put(segment)
        state.after_music(track)
        _remember_rendered_music(rendered, state)
        logger.info("Pre-warmed first segment: %s (ready for instant playback)", track.display)
        return True
    except Exception:
        logger.warning("Pre-warm failed (non-fatal, producer will generate normally)", exc_info=True)
        return False


async def _fire_interrupt(
    state: StationState,
    spec: InterruptSpec,
    queue: asyncio.Queue[Segment],
    skip_event: asyncio.Event | None,
    *,
    enforce_global_cooldown: bool = False,
    bridge_tmp_dir: Path | None = None,
) -> bool:
    """Immediately interrupt the stream with bridge audio + pissed banter.

    Uses alert.mp3 or a generated tone as a bridge clip (plays in ≤2s), drains
    the lookahead queue so no buffered music plays between bridge and banter,
    injects the directive, and fires skip_event to cut the current segment.

    Returns True if the interrupt fired, False if suppressed by the global
    cooldown gate. Per-entity cooldowns are enforced upstream by
    check_reactive_triggers.
    """
    now = time.time()
    if enforce_global_cooldown:
        elapsed = now - state.last_interrupt_ts
        if elapsed < _GLOBAL_INTERRUPT_COOLDOWN_SECONDS:
            logger.info(
                "Interrupt suppressed — global cooldown %ds remaining",
                int(_GLOBAL_INTERRUPT_COOLDOWN_SECONDS - elapsed),
            )
            return False
    state.last_interrupt_ts = now

    # Release any bridge clip a prior interrupt generated but never played.
    if state.interrupt_slot_ephemeral and state.interrupt_slot is not None:
        state.interrupt_slot.unlink(missing_ok=True)
    state.interrupt_slot = None
    state.interrupt_slot_ephemeral = False

    # Drain the lookahead queue so no buffered music leaks between bridge and banter.
    purged = 0
    while not queue.empty():
        try:
            seg = queue.get_nowait()
            if seg.ephemeral:
                seg.path.unlink(missing_ok=True)
            queue.task_done()
            purged += 1
        except Exception:
            break
    if purged:
        logger.info("Interrupt: purged %d buffered segments", purged)
    state.queued_segments.clear()

    # Inject directive + pissed tone, then cut the current segment FIRST so the
    # interrupt feels immediate. Bridge-tone generation (below) can take seconds
    # on a loaded Pi — it must never block the skip.
    state.ha_pending_directive = spec.directive
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    state.force_next = SegmentType.BANTER  # safety belt if chaos_pending is raced
    state.chaos_cutover_epoch += 1
    if skip_event is not None:
        skip_event.set()

    # Bridge audio: canned alert jingle for immediate playback. Best-effort —
    # the playback loop picks up interrupt_slot on its next iteration, so a
    # late or missing bridge just means the banter starts a beat sooner.
    alert_sfx = _SFX_DIR / "alert.mp3"
    if alert_sfx.exists():
        state.interrupt_slot = alert_sfx
    else:
        tmp_dir = bridge_tmp_dir or Path(os.getenv("MAMMAMIRADIO_TMP_DIR", "/tmp"))
        tmp_dir.mkdir(parents=True, exist_ok=True)
        bridge_path = tmp_dir / f"interrupt_bridge_{uuid4().hex[:8]}.mp3"
        try:
            await asyncio.to_thread(generate_tone, bridge_path, 1046.5, 0.75)
            state.interrupt_slot = bridge_path
            state.interrupt_slot_ephemeral = True
        except Exception:
            bridge_path.unlink(missing_ok=True)
            logger.warning("Interrupt bridge generation failed; continuing without bridge", exc_info=True)

    logger.info(
        "Interrupt fired: directive=%r urgency=%r bridge=%s",
        spec.directive,
        spec.urgency,
        state.interrupt_slot,
    )
    return True


def _emit_segment_prepared(
    state,
    *,
    segment_id: str,
    role: str,
    final_script: list[str],
    collector,
) -> None:
    """Tier-2: record the FINAL spoken script (post-processing) for one segment.

    Joins back to the Tier-1 ``llm_call`` rows via ``llm_call_refs`` (the ids the
    collector accumulated while the segment's calls fanned out) and forward to the
    Tier-3 ``stream_result`` row via the shared ``segment_id``. Enabled-check first;
    never raises into the producer.
    """
    led = getattr(state, "ledger", None)
    if led is None or not led.enabled:
        return
    try:
        import time as _time

        from mammamiradio.core.ledger import SCHEMA_VERSION

        led.record(
            {
                "schema_version": SCHEMA_VERSION,
                "ts": _time.time(),
                "record": "segment_prepared",
                "segment_id": segment_id,
                "role": role,
                "final_script": final_script,
                "llm_call_refs": [c.get("llm_call_id") for c in collector.calls] if collector else [],
            }
        )
    except Exception as exc:  # pragma: no cover - provenance must never break audio
        logger.debug("Provenance Tier-2 emit failed: %s", exc)


async def run_producer(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
    skip_event: asyncio.Event | None = None,
) -> None:
    """Keep the lookahead queue filled with rendered segments for live playback."""
    prev_seg_type = _initial_previous_segment_type(queue, state)
    logger.info("Producer started. Playlist: %d tracks", len(state.playlist))

    async def _queue_segment(segment: Segment) -> bool:
        """Queue a segment unless the operator stopped the session mid-generation."""
        nonlocal prev_seg_type
        if state.session_stopped:
            if segment.ephemeral:
                segment.path.unlink(missing_ok=True)
            logger.info("Discarding %s because the session is stopped", segment.type.value)
            return False
        await queue.put(segment)
        if "error" not in segment.metadata:
            prev_seg_type = segment.type
        return True

    # Home Assistant context cache
    ha_cache: HomeContext | None = None

    _music_qg_rejections = 0  # consecutive music quality gate rejections (circuit breaker)
    _loop = asyncio.get_running_loop()
    _last_cache_eviction = 0.0  # epoch time of last eviction check
    _cache_eviction_interval = CACHE_EVICTION_INTERVAL_SECONDS  # run eviction at most once per hour
    _last_playlist_refresh = _loop.time()  # monotonic time of last chart refresh
    _playlist_refresh_interval = PLAYLIST_REFRESH_INTERVAL_SECONDS  # refresh charts every 90 minutes
    _humanity_event_fired = False  # one-shot studio humanity event per session
    _segments_produced = 0  # count for humanity event gating
    _producer_idle_logged = False
    _was_idle = False
    _was_stopped = state.session_stopped  # True when transitioning out of a stopped state
    _prefetch_task: asyncio.Task[None] | None = None  # background norm prefetch for next track
    _drain_guard_queued = False  # True after a drain-recovery clip is inserted, until a real segment lands
    _prefetch_failed_keys: set[str] = set()  # tracks whose prefetch failed — skip until playlist rotates
    _ha_tasks: set[asyncio.Task[None]] = set()

    def _track_ha_task(task: asyncio.Task[None]) -> None:
        _ha_tasks.add(task)
        task.add_done_callback(_ha_tasks.discard)

    if config.homeassistant.enabled and config.ha_token and config.homeassistant.url:

        async def _ha_heartbeat() -> None:
            interval = 30.0
            while True:
                await asyncio.sleep(interval)
                if config.homeassistant.enabled and config.ha_token and config.homeassistant.url:
                    try:
                        await push_state_to_ha(
                            ha_url=config.homeassistant.url,
                            ha_token=config.ha_token,
                            now_streaming=copy.deepcopy(state.now_streaming),
                            current_track=state.current_track,
                            listeners_active=state.listeners_active,
                            session_stopped=state.session_stopped,
                            queue_depth=len(state.queued_segments),
                            station_name=config.display_station_name,
                        )
                        interval = 30.0
                    except Exception:
                        interval = min(interval * 2, 300.0)

        _ha_heartbeat_task = asyncio.create_task(_ha_heartbeat())
        _track_ha_task(_ha_heartbeat_task)
        producer_task = asyncio.current_task()
        if producer_task is not None:
            producer_task.add_done_callback(lambda _task: _ha_heartbeat_task.cancel())

        # Lightweight timer interrupt poll — runs every timer_poll_interval seconds.
        # Only fetches the timer entity states, not the full 200+ entity context.
        if config.homeassistant.timer_interrupts:
            _timer_entity_ids = {t.entity_id for t in config.homeassistant.timer_interrupts}
            # Pre-populate old_states for timer entities with "idle" so the first
            # active→idle transition is detected correctly (cold-start fix).
            _timer_old_states: dict[str, dict] = {eid: {"state": "idle"} for eid in _timer_entity_ids}

            async def _timer_poll_loop() -> None:
                poll_interval = max(1.0, float(config.homeassistant.timer_poll_interval))
                client = httpx.AsyncClient(timeout=5.0)
                try:
                    while True:
                        await asyncio.sleep(poll_interval)
                        if state.session_stopped:
                            continue
                        try:
                            base = config.homeassistant.url.rstrip("/")
                            headers = {
                                "Authorization": f"Bearer {config.ha_token}",
                                "Content-Type": "application/json",
                            }
                            timer_states: dict[str, dict] = {}
                            for eid in _timer_entity_ids:
                                r = await client.get(f"{base}/api/states/{eid}", headers=headers)
                                if r.status_code == 200:
                                    timer_states[eid] = r.json()
                                else:
                                    logger.warning(
                                        "Timer poll skipped %s — HA returned %s",
                                        eid,
                                        r.status_code,
                                    )
                            from mammamiradio.home.ha_enrichment import diff_states

                            timer_events = diff_states(
                                _timer_old_states,
                                timer_states,
                                None,
                                entity_labels={eid: eid for eid in _timer_entity_ids},
                                state_translations={},
                                now=time.time(),
                            )
                            _timer_old_states.update(timer_states)
                            if timer_events:
                                result = check_reactive_triggers(
                                    timer_events,
                                    timer_states,
                                    config.homeassistant.timer_interrupts,
                                )
                                if isinstance(result, InterruptSpec):
                                    await _fire_interrupt(
                                        state,
                                        result,
                                        queue,
                                        skip_event,
                                        enforce_global_cooldown=True,
                                        bridge_tmp_dir=config.tmp_dir,
                                    )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.debug("Timer poll error (non-fatal)", exc_info=True)
                finally:
                    await client.aclose()

            _timer_poll_task = asyncio.create_task(_timer_poll_loop())
            _track_ha_task(_timer_poll_task)
            if producer_task is not None:
                producer_task.add_done_callback(lambda _task: _timer_poll_task.cancel())

    while True:
        if state.session_stopped:
            if not _was_stopped and config.homeassistant.enabled and config.ha_token and config.homeassistant.url:
                _track_ha_task(
                    asyncio.create_task(
                        push_state_to_ha(
                            ha_url=config.homeassistant.url,
                            ha_token=config.ha_token,
                            now_streaming={},
                            current_track=None,
                            listeners_active=state.listeners_active,
                            session_stopped=True,
                            queue_depth=0,
                            station_name=config.display_station_name,
                        )
                    )
                )
            if _prefetch_task is not None and not _prefetch_task.done():
                _prefetch_task.cancel()
            _was_stopped = True
            try:
                await asyncio.wait_for(state.resume_event.wait(), timeout=1.0)
            except TimeoutError:
                pass
            state.resume_event.clear()
            continue

        # Resume bridge: when transitioning out of a stopped state, immediately seed
        # audio so the listener hears something within ~1s rather than waiting 55s+
        # for the first track to normalize on slow hardware (Pi).
        if _was_stopped:
            _was_stopped = False
            if queue.empty():
                bridge = _pick_canned_clip("banter", state=state) or _pick_canned_clip("welcome")
                if bridge:
                    await _queue_segment(
                        Segment(
                            type=SegmentType.BANTER,
                            path=bridge,
                            metadata={
                                "type": "banter",
                                "canned": True,
                                "resume_bridge": True,
                                "title": "Resume bridge",
                            },
                            ephemeral=False,
                        )
                    )
                else:
                    # No canned clips — grab the first pre-normalized track from the
                    # norm cache (already processed, no FFmpeg wait needed).
                    norm_files = sorted(config.cache_dir.glob("norm_*.mp3"))
                    if norm_files:
                        norm_path = norm_files[0]
                        logger.info("Resume bridge: seeding pre-normalized track %s", norm_path.name)
                        _meta = load_track_metadata(norm_path) or {}
                        await _queue_segment(
                            Segment(
                                type=SegmentType.MUSIC,
                                path=norm_path,
                                metadata={
                                    "title": _meta.get("title") or humanize_norm_filename(norm_path.name),
                                    "artist": _meta.get("artist", ""),
                                    "resume_bridge": True,
                                    "audio_source": "norm_cache",
                                },
                                ephemeral=False,
                            )
                        )

        if state.listeners_active == 0:
            if not _producer_idle_logged:
                logger.info("Producer idle: no listeners connected")
                _producer_idle_logged = True
            _was_idle = True
            await asyncio.sleep(1)
            continue

        if _was_idle:
            logger.info("Producer resuming (%d listener(s) connected)", state.listeners_active)
            # Queue is empty after idle — immediately seed audio so the listener hears
            # something while the producer generates real content.
            if queue.empty():
                fallback = _pick_canned_clip("banter", state=state) or _pick_canned_clip("welcome")
                if fallback:
                    await _queue_segment(
                        Segment(
                            type=SegmentType.BANTER,
                            path=fallback,
                            metadata={
                                "type": "banter",
                                "canned": True,
                                "warmup": True,
                                "title": "Station warm-up",
                            },
                        )
                    )
                else:
                    norm_files = sorted(config.cache_dir.glob("norm_*.mp3"))
                    if norm_files:
                        norm_path = norm_files[0]
                        logger.info("Idle bridge: seeding pre-normalized track %s", norm_path.name)
                        _meta = load_track_metadata(norm_path) or {}
                        await _queue_segment(
                            Segment(
                                type=SegmentType.MUSIC,
                                path=norm_path,
                                metadata={
                                    "title": _meta.get("title") or humanize_norm_filename(norm_path.name),
                                    "artist": _meta.get("artist", ""),
                                    "idle_bridge": True,
                                    "audio_source": "norm_cache",
                                },
                                ephemeral=False,
                            )
                        )
            _was_idle = False
        _producer_idle_logged = False

        # Mid-playback drain guard: if the queue hits zero during active playback
        # (after at least one real segment has been produced), insert a canned clip
        # to bridge the gap while the producer or prefetch task catches up.
        # _drain_guard_queued prevents re-firing until a real segment lands.
        if (
            queue.empty()
            and _segments_produced > 0
            and not _drain_guard_queued
            and await _queue_drain_recovery_bridge(_queue_segment, state, config)
        ):
            _drain_guard_queued = True

        if (
            queue.qsize() >= config.pacing.lookahead_segments
            and state.force_next is None
            and state.chaos_pending is None
        ):
            # Periodically evict stale cache files while the producer is idle
            now = asyncio.get_running_loop().time()
            if now - _last_cache_eviction >= _cache_eviction_interval:
                _last_cache_eviction = now
                # Protect norm files currently in the playback queue from eviction.
                # Evicting a queued file would break audio delivery mid-stream.
                queued_paths = {seg.path for seg in list(queue._queue) if seg.path}  # type: ignore[attr-defined]
                await asyncio.to_thread(
                    evict_cache_lru,
                    config.cache_dir,
                    config.max_cache_size_mb,
                    queued_paths,
                )
            # Periodically refresh the chart playlist mid-session so long-running
            # stations don't loop the same 50 tracks after ~3 hours.  Only merges
            # tracks not already in the playlist — played_tracks history is preserved.
            if (
                state.playlist_source is not None
                and state.playlist_source.kind == "charts"
                and now - _last_playlist_refresh >= _playlist_refresh_interval
            ):
                _last_playlist_refresh = now
                existing_ids = {t.spotify_id for t in state.playlist}
                new_tracks = await asyncio.to_thread(fetch_chart_refresh, existing_ids)
                if new_tracks:
                    state.playlist.extend(new_tracks)
                    logger.info(
                        "Chart refresh: merged %d new track(s) into playlist (%d total)",
                        len(new_tracks),
                        len(state.playlist),
                    )
            await asyncio.sleep(0.5)
            continue

        # Chaos enable flow:
        # /api/chaos -> purge prebuffer + bump epoch
        # producer sees chaos_pending -> queues BANTER
        # stale in-flight segments fail the epoch check below.
        #
        # Epoch is captured BEFORE reading chaos_pending so that a disable call
        # between the two reads increments the epoch and causes the epoch check
        # to discard the in-flight chaos segment correctly.
        generation_chaos_epoch = state.chaos_cutover_epoch
        chaos_subtype: ChaosSubtype | None = None
        is_operator_forced = False  # operator /api/trigger -> air-next (front-insert)
        if state.chaos_pending is not None:
            chaos_subtype = state.chaos_pending
            state.chaos_last_degraded_reason = ""
            seg_type = SegmentType.BANTER
            logger.info("Chaos first-strike: %s", chaos_subtype.value)
        elif state.force_next is not None:
            seg_type = state.force_next
            state.force_next = None
            # An operator trigger (not the 60s-silence rescue or other internal
            # forces) gets air-next: it is front-inserted so it airs at the next
            # boundary instead of behind the buffered lookahead.
            is_operator_forced = state.operator_force_pending is not None
            # Keep operator_force_pending set through the whole render (cleared only
            # when the segment actually queues, in _front_insert_queue_and_shadow, or
            # on a discard below). That makes the second-trigger rejection hold for the
            # full render, so a later tap can't be front-inserted ahead of this pick.
            # The panel's "Triggered" row is suppressed once production.current shows
            # this kind building, so there is no duplicate row.
            logger.info("Forced trigger: %s (air-next=%s)", seg_type.value, is_operator_forced)
        else:
            seg_type = next_segment_type(state, config.pacing)
        segment: Segment | None = None
        generation_revision = state.playlist_revision
        success_callback: Callable[[], None] | None = None
        # Render-latency deep-dive: total wall time to build this segment, logged
        # at INFO on the Queued line below. Per-stage ffmpeg breakdown is at DEBUG
        # in audio/normalizer.py (set LOG_LEVEL=DEBUG for a soak).
        _t_render = time.perf_counter()

        # Refresh Home Assistant context for banter/ad segments
        if (
            config.homeassistant.enabled
            and config.ha_token
            and seg_type
            in (
                SegmentType.BANTER,
                SegmentType.AD,
            )
        ):
            ha_cache = await fetch_home_context(
                ha_url=config.homeassistant.url,
                ha_token=config.ha_token,
                poll_interval=float(config.homeassistant.poll_interval),
                _cache=ha_cache,
            )
            state.ha_context = ha_cache.summary
            state.ha_events_summary = ha_cache.events_summary
            state.ha_home_mood = ha_cache.mood
            state.ha_weather_arc = ha_cache.weather_arc
            state.ha_home_mood_en = ha_cache.mood_en
            state.ha_weather_arc_en = ha_cache.weather_arc_en
            state.ha_events_summary_en = ha_cache.events_summary_en
            state.ha_scored_entities = [entity.to_status_dict() for entity in ha_cache.scored]
            state.ha_denylist_hits = dict(ha_cache.denylist_hits)
            state.ha_catalog_hit_rate = ha_cache.catalog_hit_rate
            # Dashboard HA moments: pick the most notable recent non-person event.
            # Restrict listener-visible events to the curated set: pre-Phase-A only
            # vetted entities could surface here, and Phase A's full-snapshot ingest
            # would otherwise leak any HA entity's friendly_name (e.g.
            # binary_sensor.bedroom_motion, lock.gun_safe) to /public-status.
            state.ha_recent_event_count = len(ha_cache.events)
            _public_events = [
                e for e in ha_cache.events if not e.entity_id.startswith("person.") and e.entity_id in ENTITY_LABELS
            ]
            if _public_events:
                _gold_set = set(GOLD_ENTITIES)
                best = max(
                    _public_events,
                    key=lambda e: (
                        e.entity_id in _gold_set,
                        e.timestamp,
                    ),
                )
                state.ha_last_event_label = best.label
                state.ha_last_event_ts = best.timestamp
                scored_labels_en = {entity["entity_id"]: entity["label"] for entity in state.ha_scored_entities}
                state.ha_last_event_label_en = scored_labels_en.get(
                    best.entity_id, ha_cache.last_event_label_en or best.label
                )
            else:
                state.ha_last_event_label = ""
                state.ha_last_event_ts = 0.0
                state.ha_last_event_label_en = ""
            # Phase 4: reactive triggers — interrupt takes priority over ambient directives
            if not state.ha_pending_directive:
                result = check_reactive_triggers(
                    ha_cache.events,
                    ha_cache.raw_states,
                    config.homeassistant.timer_interrupts or None,
                )
                if isinstance(result, InterruptSpec):
                    await _fire_interrupt(
                        state,
                        result,
                        queue,
                        skip_event,
                        enforce_global_cooldown=True,
                        bridge_tmp_dir=config.tmp_dir,
                    )
                elif isinstance(result, str):
                    state.ha_pending_directive = result

            # Impossible Moments v2 (A): fold new events into the evening ledger
            # (watermark-deduped) and, for banter only, surface one eligible
            # running-gag. Ads stay gag-free in v0. The ledger persists across
            # the addon's frequent restarts.
            if state.evening_ledger is not None:
                _now = time.time()
                state.evening_ledger.observe(ha_cache.events, now=_now)
                if seg_type == SegmentType.BANTER:
                    # Offer (don't spend) — the cooldown is marked in the banter
                    # success callback only if generated banter actually airs, so
                    # an LLM failure that falls back to a canned clip does not burn
                    # the callback.
                    offered = state.evening_ledger.offer_gag(now=_now)
                    if offered is not None:
                        state.ha_running_gag_key, state.ha_running_gag = offered
                    else:
                        state.ha_running_gag = ""
                        state.ha_running_gag_key = ""
                else:
                    state.ha_running_gag = ""
                    state.ha_running_gag_key = ""
                state.evening_ledger.save_if_dirty(config.cache_dir)

        if generation_chaos_epoch != state.chaos_cutover_epoch:
            logger.info("Restarting producer cycle after interrupt cutover")
            continue

        try:
            if seg_type == SegmentType.MUSIC:
                track = None
                playlist_idx: int = -1
                for _ in range(MUSIC_SELECTION_RETRIES):
                    candidate = state.select_next_track(
                        repeat_cooldown=config.playlist.repeat_cooldown,
                        artist_cooldown=config.playlist.artist_cooldown,
                    )
                    if not is_rejected_cache_key(candidate.cache_key):
                        track = candidate
                        break
                    logger.debug(
                        "Skipping denylisted track (already rejected this session): %s",
                        candidate.display,
                    )
                if track is None:
                    # All recent candidates denylisted — yield to event loop and retry.
                    await asyncio.sleep(0.1)
                    continue
                logger.info("Producing MUSIC: %s", track.display)
                playlist_idx = next(
                    (i for i, t in enumerate(state.playlist) if t is track),
                    -1,
                )

                loop = asyncio.get_running_loop()
                state.set_gen("finding", "music", f"Finding {track.display}")
                _gen_ok = False
                try:
                    rendered = await _render_music_track(track, config, temp_prefix="music", context="music")
                    _gen_ok = rendered is not None
                finally:
                    state.end_gen(ok=_gen_ok)
                if rendered is None:
                    continue
                norm_path = rendered.path
                norm_cached = rendered.cache_path
                norm_is_cached = rendered.cache_hit
                audio_source = "download"

                # Quality gate: reject truncated/silent downloads before queueing.
                # Circuit breaker: after MUSIC_QUALITY_GATE_REJECTION_LIMIT consecutive rejections, either serve a
                # pre-bundled banter clip (when the rejection is due to silence — i.e. all
                # tracks are silence placeholders and playing them would cause dead air) or
                # let the track through as-is (when rejected for other reasons such as being
                # short — silence is still worse than a slightly-short real track).
                if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
                    _music_loop = asyncio.get_running_loop()
                    try:
                        await _music_loop.run_in_executor(None, validate_segment_audio, norm_path, SegmentType.MUSIC)
                        _music_qg_rejections = 0
                    except AudioToolError as exc:
                        logger.warning("Audio tool unavailable, skipping music quality check: %s", exc)
                    except AudioQualityError as exc:
                        _music_qg_rejections += 1
                        if _music_qg_rejections >= MUSIC_QUALITY_GATE_REJECTION_LIMIT:
                            _music_qg_rejections = 0
                            if "silence" in str(exc).lower():
                                # All available tracks are silence placeholders.  Playing
                                # them would break the illusion with dead air.  Insert a
                                # bundled banter clip instead so the stream stays alive.
                                fallback = _pick_canned_clip("banter", state=state) or _pick_canned_clip("welcome")
                                if fallback:
                                    logger.warning(
                                        "Quality gate circuit breaker: %d consecutive silence rejections — "
                                        "inserting fallback banter to prevent dead air (%s: %s)",
                                        MUSIC_QUALITY_GATE_REJECTION_LIMIT,
                                        norm_path.name,
                                        exc,
                                    )
                                    if not norm_is_cached:
                                        norm_path.unlink(missing_ok=True)
                                    await _queue_segment(
                                        Segment(
                                            type=SegmentType.BANTER,
                                            path=fallback,
                                            metadata={
                                                "type": "banter",
                                                "canned": True,
                                                "silence_fallback": True,
                                                "title": "Recovery banter",
                                            },
                                            ephemeral=False,
                                        )
                                    )
                                    continue
                                # No banter clips — recycle the last known-good music
                                # norm rather than letting a silent file through.
                                last_good = _get_last_music_file(state)
                                if last_good:
                                    logger.warning(
                                        "Quality gate circuit breaker: silence with no banter fallback — "
                                        "recycling last-known-good music (%s: %s)",
                                        norm_path.name,
                                        exc,
                                    )
                                    if not norm_is_cached:
                                        norm_path.unlink(missing_ok=True)
                                    await _queue_segment(
                                        Segment(
                                            type=SegmentType.MUSIC,
                                            path=last_good,
                                            metadata={
                                                "type": "music",
                                                "recycled": True,
                                                "silence_fallback": True,
                                                "title": last_good.name,
                                            },
                                            ephemeral=False,
                                        )
                                    )
                                    continue
                                # No banter, no last-known-good.  Drop this track and let
                                # the streamer's rescue path handle the gap — queueing a
                                # silent file would break the illusion.
                                logger.error(
                                    "Quality gate circuit breaker: silence, no banter, "
                                    "no last-known-good music — dropping track (%s: %s)",
                                    norm_path.name,
                                    exc,
                                )
                                if not norm_is_cached:
                                    norm_path.unlink(missing_ok=True)
                                continue
                            else:
                                # Short/quiet track — likely a real file that just barely
                                # missed the threshold.  Let it through; it's better than silence.
                                logger.warning(
                                    "Quality gate circuit breaker: %d consecutive rejections, "
                                    "allowing track through to prevent stream starvation (%s: %s)",
                                    MUSIC_QUALITY_GATE_REJECTION_LIMIT,
                                    norm_path.name,
                                    exc,
                                )
                        else:
                            # Drop the cached normalization so a poisoned output
                            # doesn't get re-served.  We intentionally do NOT
                            # session-denylist the source cache_key here: quality-gate
                            # rejections are normalization artifacts, and the
                            # circuit breaker is the
                            # right escape valve, not a per-track block.
                            norm_cached.unlink(missing_ok=True)
                            logger.warning("Quality gate rejected music track (%s): %s", norm_path.name, exc)
                            if not norm_is_cached:
                                norm_path.unlink(missing_ok=True)
                            continue

                # Generate "Why this track?" rationale for listener UI
                rationale = generate_track_rationale(
                    track,
                    source=state.playlist_source,
                    listener=state.listener,
                )
                crate = classify_track_crate(track, state.playlist_source)

                # Studio bleed: mix faint prior banter under the start of the
                # music segment to create the "someone left a mic on" atmosphere.
                if state.recent_banter_paths and random.random() < 0.35:
                    bleed_src = random.choice(list(state.recent_banter_paths))
                    if bleed_src.exists():
                        bleed_out = config.tmp_dir / f"bleed_{uuid4().hex[:8]}.mp3"
                        try:
                            await loop.run_in_executor(None, mix_quiet_bleed, norm_path, bleed_src, bleed_out)
                            if not norm_is_cached:
                                norm_path.unlink(missing_ok=True)
                            norm_path = bleed_out
                            norm_is_cached = False
                            logger.debug("Studio bleed applied to %s", norm_path.name)
                        except Exception as exc:
                            logger.debug("Studio bleed skipped: %s", exc)
                            bleed_out.unlink(missing_ok=True)

                segment = Segment(
                    type=SegmentType.MUSIC,
                    path=norm_path,
                    metadata={
                        "title": track.display,
                        "artist": track.artist,
                        "title_only": track.title,
                        "youtube_id": track.youtube_id,
                        "spotify_id": track.spotify_id,
                        "album_art": track.album_art,
                        "duration_ms": track.duration_ms,
                        "rationale": rationale,
                        "crate": crate,
                        "audio_source": audio_source,
                        "playlist_index": playlist_idx,
                        "source_kind": getattr(track, "source", ""),
                    },
                    ephemeral=not norm_is_cached,
                )
                _bound_track = track
                _remember_rendered_music(rendered, state)

                def _music_callback(_t=_bound_track) -> None:
                    state.after_music(_t)

                success_callback = _music_callback

            elif seg_type == SegmentType.BANTER:
                logger.info("Producing BANTER")

                # Track listening sessions for compounding persona
                await _maybe_start_session(state)

                # Capture new-listener count (defer clearing until segment succeeds)
                _new_listener_count = state.new_listeners_pending
                _is_new_listener = _new_listener_count > 0
                _is_first_listener = _is_new_listener and state.listeners_active == 1

                impossible_tts = False
                canned = None
                listener_request_commit = None
                has_music_tail = False
                loop = asyncio.get_running_loop()

                if chaos_subtype is None and not _sw.has_script_llm(config):
                    # No LLM — use canned clips + impossible TTS lines
                    if _is_new_listener:
                        line = generate_impossible_line(
                            segments_produced=state.segments_produced,
                            listener_patterns=state.listener.patterns,
                            is_new_listener=True,
                            is_first_listener=_is_first_listener,
                        )
                        logger.info("Impossible moment (new listener): %s", line[:60])
                        try:
                            audio_path = await _synthesize_impossible_moment(line, config, state)
                            impossible_tts = True
                        except Exception as exc:
                            logger.warning("Impossible moment TTS failed: %s", exc)

                    if not impossible_tts:
                        # Use canned clips for first 2, then impossible TTS as the gold closer
                        if state.canned_clips_streamed < SHAREWARE_CANNED_LIMIT - 1:
                            canned = _pick_canned_clip("banter", state=state)
                        if not canned:
                            line = generate_impossible_line(
                                segments_produced=state.segments_produced,
                                listener_patterns=state.listener.patterns,
                            )
                            logger.info("Impossible moment (no LLM): %s", line[:60])
                            try:
                                audio_path = await _synthesize_impossible_moment(line, config, state)
                                impossible_tts = True
                            except Exception as exc:
                                logger.warning("Impossible TTS failed, falling back to canned: %s", exc)
                                canned = _pick_canned_clip("banter", state=state)

                banter_expected_min_duration_sec: float | None = None
                banter_expected_line_count: int | None = None
                _banter_attempt_id: str = ""

                if canned:
                    logger.info("Using pre-bundled banter clip: %s", canned.name)
                    audio_path = canned
                    state.last_banter_script = [{"host": "Radio", "text": "(pre-recorded banter)"}]
                elif not impossible_tts:
                    try:
                        from mammamiradio.core.provenance_ctx import (
                            CallCollector,
                            reset_collector,
                            set_collector,
                        )

                        if chaos_subtype is not None:
                            _banter_attempt_id = uuid4().hex
                            _banter_collector = CallCollector(attempt_id=_banter_attempt_id)
                            _prov_tok = set_collector(_banter_collector)
                            state.set_gen("writing", "banter", "Writing banter")
                            _gen_ok = False
                            try:
                                lines, listener_request_commit = await _sw.write_banter(
                                    state,
                                    config,
                                    is_new_listener=_is_new_listener,
                                    is_first_listener=_is_first_listener,
                                    chaos_subtype=chaos_subtype,
                                )
                                _gen_ok = True
                            finally:
                                reset_collector(_prov_tok)
                                state.end_gen(ok=_gen_ok)
                            line_texts = [text for _host, text in lines]
                            _emit_segment_prepared(
                                state,
                                segment_id=_banter_attempt_id,
                                role="banter",
                                final_script=line_texts,
                                collector=_banter_collector,
                            )
                            banter_expected_min_duration_sec = _expected_banter_duration_sec(line_texts)
                            banter_expected_line_count = len(line_texts) if len(line_texts) > 1 else None
                            audio_path = await synthesize_dialogue(lines, config.tmp_dir, state=state)
                            state.last_banter_script = [
                                {
                                    "host": h.name,
                                    "text": t,
                                    "type": "chaos_banter",
                                    "chaos_subtype": chaos_subtype.value,
                                }
                                for h, t in lines
                            ]
                        else:
                            # Generate transition voice + banter in parallel
                            _banter_attempt_id = uuid4().hex
                            _banter_collector = CallCollector(attempt_id=_banter_attempt_id)
                            _prov_tok = set_collector(_banter_collector)
                            state.set_gen("writing", "banter", "Writing banter")
                            _gen_ok = False
                            try:
                                transition_task = _sw.write_transition(state, config, next_segment="banter")
                                banter_task = _sw.write_banter(
                                    state,
                                    config,
                                    is_new_listener=_is_new_listener,
                                    is_first_listener=_is_first_listener,
                                )
                                (trans_host, trans_text), (lines, listener_request_commit) = await asyncio.gather(
                                    transition_task, banter_task
                                )
                                _gen_ok = True
                            finally:
                                reset_collector(_prov_tok)
                                state.end_gen(ok=_gen_ok)
                            line_texts = [trans_text] + [text for _host, text in lines]
                            _emit_segment_prepared(
                                state,
                                segment_id=_banter_attempt_id,
                                role="banter",
                                final_script=line_texts,
                                collector=_banter_collector,
                            )
                            banter_expected_min_duration_sec = _expected_banter_duration_sec(line_texts)
                            banter_expected_line_count = len(line_texts) if len(line_texts) > 1 else None

                            # Synthesize transition + dialogue in parallel
                            trans_voice_path = config.tmp_dir / f"trans_{uuid4().hex[:8]}.mp3"
                            prosody: dict[str, str] = {}
                            if trans_host.personality.energy > 50:
                                prosody["rate"] = "+5%"

                            async def _do_transition(
                                _text=trans_text,
                                _host=trans_host,
                                _path=trans_voice_path,
                                _prosody=prosody,
                            ):
                                await synthesize(
                                    _text,
                                    _host.voice,
                                    _path,
                                    **_prosody,
                                    engine=_host.engine,
                                    edge_fallback_voice=_host.edge_fallback_voice,
                                    state=state,
                                )
                                xfade_out = config.tmp_dir / f"banter_trans_{uuid4().hex[:8]}.mp3"
                                result = await _try_crossfade(_path, config, xfade_out)
                                return result, result == xfade_out

                            banter_path: Path
                            (trans_voice_path, has_music_tail), banter_path = await asyncio.gather(
                                _do_transition(),
                                synthesize_dialogue(lines, config.tmp_dir, state=state),
                            )

                            # Concat: transition + banter (both pre-normalized)
                            audio_path = config.tmp_dir / f"banter_full_{uuid4().hex[:8]}.mp3"
                            loop = asyncio.get_running_loop()
                            try:
                                await loop.run_in_executor(
                                    None,
                                    partial(
                                        concat_files,
                                        [trans_voice_path, banter_path],
                                        audio_path,
                                        200,
                                        False,
                                        strict_duration=True,
                                    ),
                                )
                            except Exception:
                                audio_path.unlink(missing_ok=True)
                                raise
                            finally:
                                trans_voice_path.unlink(missing_ok=True)
                                banter_path.unlink(missing_ok=True)

                            state.recent_transition_texts.append(trans_text)
                            state.last_banter_script = [
                                {"host": trans_host.name, "text": trans_text, "type": "transition"},
                            ] + [{"host": h.name, "text": t} for h, t in lines]
                    except Exception as exc:
                        if chaos_subtype is not None:
                            state.chaos_audio_failures += 1
                            state.chaos_last_degraded_reason = "audio_failure"
                            logger.warning("Chaos audio generation failed; trying canned fallback: %s", exc)
                            canned = _pick_canned_clip("banter", state=state)
                            if canned:
                                banter_expected_min_duration_sec = None
                                banter_expected_line_count = None
                                audio_path = canned
                                state.last_banter_script = [
                                    {
                                        "host": "Radio",
                                        "text": "(pre-recorded chaos fallback)",
                                        "type": "chaos_audio_fallback",
                                        "chaos_subtype": chaos_subtype.value,
                                    }
                                ]
                            else:
                                if state.chaos_audio_failures >= CHAOS_AUDIO_FAILURE_LIMIT:
                                    state.chaos_pending = None
                                    state.chaos_last_degraded_reason = "strike_abandoned"
                                    logger.error(
                                        "Chaos first-strike abandoned after %d failures",
                                        state.chaos_audio_failures,
                                    )
                                else:
                                    await asyncio.sleep(CHAOS_AUDIO_FAILURE_BACKOFF_SECONDS)
                                continue
                        else:
                            logger.warning("Banter TTS failed, skipping segment: %s", exc)
                            continue

                if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
                    try:
                        expected_min_duration_sec = None if canned else banter_expected_min_duration_sec
                        expected_line_count = None if canned else banter_expected_line_count
                        await loop.run_in_executor(
                            None,
                            partial(
                                validate_segment_audio,
                                audio_path,
                                SegmentType.BANTER,
                                expected_min_duration_sec=expected_min_duration_sec,
                                expected_line_count=expected_line_count,
                            ),
                        )
                    except AudioToolError as exc:
                        logger.warning("Audio tool unavailable, skipping banter quality check: %s", exc)
                    except AudioQualityError as exc:
                        logger.warning("Quality gate rejected banter (%s): %s", audio_path.name, exc)
                        if chaos_subtype is not None:
                            state.chaos_audio_failures += 1
                            state.chaos_last_degraded_reason = "audio_failure"
                        if canned is None:
                            audio_path.unlink(missing_ok=True)
                        fallback_canned = _pick_canned_clip("banter", state=state)
                        if fallback_canned:
                            try:
                                await loop.run_in_executor(
                                    None, validate_segment_audio, fallback_canned, SegmentType.BANTER
                                )
                                logger.info(
                                    "Using canned banter fallback after quality reject: %s", fallback_canned.name
                                )
                                audio_path = fallback_canned
                                canned = fallback_canned
                                fallback_text = "(pre-recorded banter)"
                                fallback_type = "banter"
                                if chaos_subtype is not None:
                                    fallback_text = "(pre-recorded chaos fallback)"
                                    fallback_type = "chaos_audio_fallback"
                                state.last_banter_script = [
                                    {
                                        "host": "Radio",
                                        "text": fallback_text,
                                        "type": fallback_type,
                                        "chaos_subtype": chaos_subtype.value if chaos_subtype else "",
                                    }
                                ]
                            except AudioToolError as fallback_tool_exc:
                                logger.warning(
                                    "Audio tool unavailable during fallback quality check: %s", fallback_tool_exc
                                )
                            except AudioQualityError as fallback_exc:
                                logger.error(
                                    "ASSET CORRUPTION: canned banter fallback also rejected (%s): %s",
                                    fallback_canned.name,
                                    fallback_exc,
                                )
                                if chaos_subtype is not None:
                                    if state.chaos_audio_failures >= CHAOS_AUDIO_FAILURE_LIMIT:
                                        state.chaos_pending = None
                                        state.chaos_last_degraded_reason = "strike_abandoned"
                                        logger.error(
                                            "Chaos first-strike abandoned after %d failures",
                                            state.chaos_audio_failures,
                                        )
                                    else:
                                        await asyncio.sleep(CHAOS_AUDIO_FAILURE_BACKOFF_SECONDS)
                                continue
                        else:
                            if chaos_subtype is not None:
                                if state.chaos_audio_failures >= CHAOS_AUDIO_FAILURE_LIMIT:
                                    state.chaos_pending = None
                                    state.chaos_last_degraded_reason = "strike_abandoned"
                                    logger.error(
                                        "Chaos first-strike abandoned after %d failures",
                                        state.chaos_audio_failures,
                                    )
                                else:
                                    await asyncio.sleep(CHAOS_AUDIO_FAILURE_BACKOFF_SECONDS)
                            continue

                if canned is None:
                    try:
                        audio_path = await _apply_talk_bed(audio_path, config, state, prefix="banter")
                    except Exception as exc:
                        logger.warning("Talk bed generation failed, using dry banter: %s", exc)

                # One-shot studio humanity event: cough, paper rustle, etc.
                # Only fires once per session, only after 15+ segments produced.
                if not _humanity_event_fired and _segments_produced >= 15 and canned is None and random.random() < 0.10:
                    sfx_studio_dir = _DEMO_ASSETS_DIR / "sfx" / "studio"
                    if sfx_studio_dir.is_dir():
                        sfx_files = list(sfx_studio_dir.glob("*.mp3"))
                        if sfx_files:
                            sfx_pick = random.choice(sfx_files)
                            humanity_out = config.tmp_dir / f"humanity_{uuid4().hex[:8]}.mp3"
                            try:
                                await loop.run_in_executor(
                                    None, mix_oneshot_sfx, audio_path, sfx_pick, humanity_out, 2.0, -18.0
                                )
                                if canned is None:
                                    audio_path.unlink(missing_ok=True)
                                audio_path = humanity_out
                                _humanity_event_fired = True
                                logger.info("Studio humanity event: %s", sfx_pick.name)
                            except Exception as exc:
                                logger.debug("Humanity event skipped: %s", exc)
                                humanity_out.unlink(missing_ok=True)

                segment = Segment(
                    type=SegmentType.BANTER,
                    path=audio_path,
                    metadata={
                        "type": "banter",
                        "lines": state.last_banter_script,
                        "canned": canned is not None,
                        "title": _banter_title(
                            state.last_banter_script,
                            canned=canned is not None,
                            host_order=[h.name for h in config.hosts],
                        ),
                        "chaos_subtype": chaos_subtype.value if chaos_subtype else "",
                        "chaos_degraded": state.chaos_last_degraded_reason if chaos_subtype else "",
                        "has_music_tail": bool(has_music_tail),
                        "ledger_segment_id": _banter_attempt_id or None,
                    },
                    ephemeral=canned is None,
                )

                def _banter_callback(
                    *,
                    _is_new_listener=_is_new_listener,
                    _new_listener_count=_new_listener_count,
                    _listener_request_commit=listener_request_commit,
                    _used_generated_banter=(canned is None and not impossible_tts),
                    _gag_key=state.ha_running_gag_key,
                    _ledger=state.evening_ledger,
                    _cache_dir=config.cache_dir,
                ) -> None:
                    state.after_banter()
                    if _is_new_listener:
                        state.new_listeners_pending = max(0, state.new_listeners_pending - _new_listener_count)
                    if _used_generated_banter and _listener_request_commit is not None:
                        _listener_request_commit.apply(state)
                    # Spend the running-gag cooldown only when generated banter
                    # (which carried the gag) actually airs — not on canned or
                    # failed-LLM fallbacks. Honors EveningLedger.offer_gag's contract.
                    if _used_generated_banter and _ledger is not None and _gag_key:
                        _ledger.mark_spoken(_gag_key, now=time.time())
                        _ledger.save_if_dirty(_cache_dir)
                    state.ha_running_gag_key = ""

                success_callback = _banter_callback

            elif seg_type == SegmentType.NEWS_FLASH:
                logger.info("Producing NEWS FLASH")

                try:
                    state.set_gen("writing", "news_flash", "Writing a news flash")
                    _gen_ok = False
                    try:
                        host, text, category = await _sw.write_news_flash(state, config)
                        _gen_ok = True
                    finally:
                        state.end_gen(ok=_gen_ok)
                    flash_path = config.tmp_dir / f"flash_{uuid4().hex[:8]}.mp3"

                    # Keep news flashes intelligible; only traffic gets a small urgency nudge.
                    flash_rate: str | None = None
                    if category == "traffic":
                        flash_rate = "+10%"

                    await synthesize(
                        text,
                        host.voice,
                        flash_path,
                        rate=flash_rate,
                        engine=host.engine,
                        edge_fallback_voice=host.edge_fallback_voice,
                        state=state,
                    )

                    # Try to overlay on the tail of the last music segment
                    crossfade_out = config.tmp_dir / f"flash_transition_{uuid4().hex[:8]}.mp3"
                    audio_path = await _try_crossfade(flash_path, config, crossfade_out, tail_seconds=6.0)
                    has_music_tail = audio_path == crossfade_out
                    if audio_path is flash_path:
                        # Crossfade failed — add a static bed instead
                        try:
                            audio_path = await _apply_talk_bed(audio_path, config, state, prefix="news")
                        except Exception as exc:
                            logger.warning("Talk bed generation failed, using dry news flash: %s", exc)

                    state.last_banter_script = [{"host": host.name, "text": text, "type": "news_flash"}]
                except Exception as exc:
                    logger.warning("News flash TTS failed, skipping: %s", exc)
                    continue

                segment = Segment(
                    type=SegmentType.NEWS_FLASH,
                    path=audio_path,
                    metadata={
                        "type": "news_flash",
                        "category": category,
                        "host": host.name,
                        "title": f"News flash: {category}" if category else "News flash",
                        "has_music_tail": bool(has_music_tail),
                    },
                )

                _bound_cat = category

                def _news_callback(_c=_bound_cat) -> None:
                    state.after_news_flash(_c)

                success_callback = _news_callback

            elif seg_type == SegmentType.STATION_ID:
                logger.info("Producing STATION ID")
                sb = config.sonic_brand
                # Use full ident text, or fall back to station name
                ident_text = sb.full_ident or config.station.name

                try:
                    # Generate voice tag + musical sting in parallel
                    voice_path = config.tmp_dir / f"stid_voice_{uuid4().hex[:8]}.mp3"
                    sting_path = config.tmp_dir / f"stid_sting_{uuid4().hex[:8]}.mp3"

                    # Use configured sweeper voice, or a random host
                    sweeper_voice = sb.sweeper_voice
                    sweeper_engine = sb.sweeper_engine
                    sweeper_fallback = sb.sweeper_edge_fallback_voice
                    if not sweeper_voice:
                        sweeper_host = random.choice(config.hosts)
                        sweeper_voice = sweeper_host.voice
                        sweeper_engine = sweeper_host.engine
                        sweeper_fallback = sweeper_host.edge_fallback_voice
                    loop = asyncio.get_running_loop()

                    voice_task = synthesize(
                        ident_text,
                        sweeper_voice,
                        voice_path,
                        engine=sweeper_engine,
                        edge_fallback_voice=sweeper_fallback,
                        state=state,
                    )
                    sting_task = loop.run_in_executor(None, generate_station_id_bed, sting_path, 3.0, sb.motif_notes)
                    await asyncio.gather(voice_task, sting_task)

                    # Mix voice over sting
                    audio_path = config.tmp_dir / f"stid_{uuid4().hex[:8]}.mp3"
                    await loop.run_in_executor(None, mix_voice_with_sting, voice_path, sting_path, audio_path)
                    voice_path.unlink(missing_ok=True)
                    sting_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("Station ID generation failed: %s", exc)
                    continue

                segment = Segment(
                    type=SegmentType.STATION_ID,
                    path=audio_path,
                    metadata={"type": "station_id", "text": ident_text, "title": "Station ID"},
                )
                success_callback = state.after_station_id

            elif seg_type == SegmentType.SWEEPER:
                logger.info("Producing SWEEPER")
                sb = config.sonic_brand

                try:
                    sweeper_text = random.choice(sb.sweepers) if sb.sweepers else config.station.name

                    sweeper_voice = sb.sweeper_voice
                    sweeper_engine = sb.sweeper_engine
                    sweeper_fallback = sb.sweeper_edge_fallback_voice
                    if not sweeper_voice:
                        sweeper_host = random.choice(config.hosts)
                        sweeper_voice = sweeper_host.voice
                        sweeper_engine = sweeper_host.engine
                        sweeper_fallback = sweeper_host.edge_fallback_voice

                    audio_path = config.tmp_dir / f"sweeper_{uuid4().hex[:8]}.mp3"
                    await synthesize(
                        sweeper_text,
                        sweeper_voice,
                        audio_path,
                        engine=sweeper_engine,
                        edge_fallback_voice=sweeper_fallback,
                        state=state,
                    )
                    loop = asyncio.get_running_loop()
                    sting_path = config.tmp_dir / f"sweeper_sting_{uuid4().hex[:8]}.mp3"
                    imaging_lib = _make_imaging_lib(config)
                    mixed_path = config.tmp_dir / f"sweeper_mixed_{uuid4().hex[:8]}.mp3"
                    dry_sweeper_path = audio_path
                    try:
                        await loop.run_in_executor(None, imaging_lib.pick_sweeper_sting, sting_path)
                        await loop.run_in_executor(None, mix_voice_with_sting, audio_path, sting_path, mixed_path)
                    except Exception:
                        mixed_path.unlink(missing_ok=True)
                        dry_sweeper_path.unlink(missing_ok=True)
                        raise
                    finally:
                        sting_path.unlink(missing_ok=True)
                    dry_sweeper_path.unlink(missing_ok=True)
                    audio_path = mixed_path
                except Exception as exc:
                    logger.warning("Sweeper generation failed: %s", exc)
                    continue

                segment = Segment(
                    type=SegmentType.SWEEPER,
                    path=audio_path,
                    metadata={"type": "sweeper", "text": sweeper_text, "title": "Station sweeper"},
                )
                success_callback = state.after_sweeper

            elif seg_type == SegmentType.TIME_CHECK:
                logger.info("Producing TIME CHECK")
                dt_now = datetime.datetime.now()
                hour = dt_now.hour
                minute = dt_now.minute
                station_name = config.station.name
                # Italian grammar: "È l'una" for 1:00/13:00, "Sono le N" otherwise
                hour_str = "È l'una" if hour in (1, 13) else f"Sono le {hour}"
                if minute == 0:
                    time_text = f"{hour_str} su {station_name}."
                else:
                    time_text = f"{hour_str} e {minute} su {station_name}."

                try:
                    voice_path = config.tmp_dir / f"time_voice_{uuid4().hex[:8]}.mp3"
                    chime_path = config.tmp_dir / f"time_chime_{uuid4().hex[:8]}.mp3"
                    host = random.choice(config.hosts)
                    loop = asyncio.get_running_loop()
                    # Voice + chime in parallel (independent)
                    await asyncio.gather(
                        synthesize(
                            time_text,
                            host.voice,
                            voice_path,
                            engine=host.engine,
                            edge_fallback_voice=host.edge_fallback_voice,
                            state=state,
                        ),
                        loop.run_in_executor(None, generate_tone, chime_path, 1047, 0.3),
                    )
                    audio_path = config.tmp_dir / f"time_{uuid4().hex[:8]}.mp3"
                    await loop.run_in_executor(None, concat_files, [chime_path, voice_path], audio_path, 200, False)
                    chime_path.unlink(missing_ok=True)
                    voice_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("Time check generation failed: %s", exc)
                    continue

                segment = Segment(
                    type=SegmentType.TIME_CHECK,
                    path=audio_path,
                    metadata={"type": "time_check", "time": time_text, "title": f"Time check — {time_text}"},
                )
                success_callback = state.after_time_check

            elif seg_type == SegmentType.AD:
                if not config.ads.brands:
                    logger.warning("No brands configured — skipping ad, resetting ad pacing counter")
                    state.songs_since_ad = 0
                    continue

                num_spots = max(1, config.pacing.ad_spots_per_break)
                logger.info("Producing AD BREAK: %d spot(s)", num_spots)
                break_parts: list[Path] = []
                break_brands: list[str] = []
                break_summaries: list[str] = []
                break_texts: list[str] = []
                break_sonic_worlds: list[str] = []

                loop = asyncio.get_running_loop()
                sfx_dir = Path(config.ads.sfx_dir) if config.ads.sfx_dir else None

                # ── Pre-compute brand selections (pure sync, no I/O) ──
                used_brands_this_break: list[str] = []
                break_formats: list[str] = []
                break_roles: list[list[str]] = []
                spot_params = []
                for spot_idx in range(num_spots):
                    brand = _pick_brand(
                        config.ads.brands,
                        list(state.ad_history)
                        + [AdHistoryEntry(brand=b, summary="", timestamp=0) for b in used_brands_this_break],
                    )
                    used_brands_this_break.append(brand.name)
                    num_voices = len(config.ads.voices) if config.ads.voices else 1
                    ad_format, sonic, roles_needed = _select_ad_creative(brand, state, num_voices)
                    voice_map = _cast_voices(brand, config.ads.voices, config.hosts, roles_needed)
                    logger.info(
                        "  Spot %d/%d: %s (format=%s, roles=%s)",
                        spot_idx + 1,
                        num_spots,
                        brand.name,
                        ad_format,
                        list(voice_map.keys()),
                    )
                    spot_params.append((brand, ad_format, sonic, voice_map))

                # ── PHASE 1: Fan out intro pipeline + all LLM calls + bumpers in parallel ──
                # These are all independent: intro doesn't need scripts, scripts don't need bumpers

                async def _build_intro():
                    """Intro: transition LLM → TTS → crossfade + promo tag."""
                    parts = []
                    try:
                        ihost, itext = await _sw.write_transition(state, config, next_segment="ad")
                    except Exception:
                        ihost = random.choice(config.hosts)
                        itext = random.choice(_sw.AD_BREAK_INTROS)
                    ipath = config.tmp_dir / f"ad_intro_{uuid4().hex[:8]}.mp3"
                    await synthesize(
                        itext,
                        ihost.voice,
                        ipath,
                        engine=ihost.engine,
                        edge_fallback_voice=ihost.edge_fallback_voice,
                        state=state,
                    )
                    xout = config.tmp_dir / f"ad_trans_{uuid4().hex[:8]}.mp3"
                    ipath = await _try_crossfade(ipath, config, xout)
                    has_music_tail = ipath == xout
                    parts.append(ipath)
                    # Promo compliance tag
                    try:
                        ppath = config.tmp_dir / f"promo_tag_{uuid4().hex[:8]}.mp3"
                        if config.ads.voices:
                            pvoice = config.ads.voices[0].voice
                            pengine = "edge"
                            pfallback = ""
                        else:
                            pvoice = ihost.voice
                            pengine = ihost.engine
                            pfallback = ihost.edge_fallback_voice
                        await synthesize(
                            "Messaggio promozionale.",
                            pvoice,
                            ppath,
                            rate="+40%",
                            pitch="-10Hz",
                            engine=pengine,
                            edge_fallback_voice=pfallback,
                            state=state,
                        )
                        parts.append(ppath)
                    except Exception:
                        pass
                    return parts, itext, has_music_tail

                async def _build_bumpers(_num_spots=num_spots, _loop=loop):
                    """Opening bumper + sparse mid-spot bumpers.

                    Mid-bumpers only play ~25% of the time to avoid harsh
                    synthetic SFX overwhelming the ad break.
                    """
                    bumper_in = config.tmp_dir / f"bumper_in_{uuid4().hex[:8]}.mp3"
                    mid_bumpers = [
                        config.tmp_dir / f"bumper_mid_{uuid4().hex[:8]}.mp3"
                        for _ in range(max(0, _num_spots - 1))
                        if random.random() < 0.25
                    ]
                    tasks = [_loop.run_in_executor(None, generate_bumper_jingle, bumper_in)]
                    for mb in mid_bumpers:
                        tasks.append(_loop.run_in_executor(None, generate_bumper_jingle, mb, 0.8))
                    await asyncio.gather(*tasks)
                    return bumper_in, mid_bumpers

                # Fan out: intro + LLM scripts + bumpers all in parallel
                from mammamiradio.core.provenance_ctx import (
                    CallCollector,
                    reset_collector,
                    set_collector,
                )

                _ad_attempt_id = uuid4().hex
                _ad_collector = CallCollector(attempt_id=_ad_attempt_id, ad_break_id=_ad_attempt_id)
                _ad_prov_tok = set_collector(_ad_collector)
                _ad_brand = spot_params[0][0] if spot_params else ""
                state.set_gen("writing", "ad", f"Writing the {_ad_brand} spot" if _ad_brand else "Writing an ad break")
                _gen_ok = False
                try:
                    (
                        (intro_parts, intro_text, intro_has_music_tail),
                        scripts,
                        (bumper_in, mid_bumpers),
                    ) = await asyncio.gather(
                        _build_intro(),
                        asyncio.gather(
                            *(
                                _sw.write_ad(brand, vm, state, config, ad_format=af, sonic=sn, spot_index=i)
                                for i, (brand, af, sn, vm) in enumerate(spot_params)
                            )
                        ),
                        _build_bumpers(),
                    )
                    _gen_ok = True
                finally:
                    reset_collector(_ad_prov_tok)
                    state.end_gen(ok=_gen_ok)

                # ── PHASE 2: Fan out all ad TTS synthesis in parallel ──
                ad_paths = await asyncio.gather(
                    *(
                        synthesize_ad(script, vm, config.tmp_dir, sfx_dir, state=state)
                        for script, (_, _, _, vm) in zip(scripts, spot_params, strict=False)
                    )
                )

                # ── PHASE 3: Assemble break_parts in order ──
                if intro_text:
                    state.recent_transition_texts.append(intro_text)
                break_parts.extend(intro_parts)
                break_parts.append(bumper_in)

                for spot_idx, (script, ad_path) in enumerate(zip(scripts, ad_paths, strict=False)):
                    brand = spot_params[spot_idx][0]
                    break_parts.append(ad_path)
                    break_brands.append(brand.name)
                    break_summaries.append(script.summary)
                    break_formats.append(script.format)
                    break_sonic_worlds.append(script.sonic.music_bed if script.sonic else "")
                    break_roles.append(script.roles_used or [])
                    full_text = " ".join(p.text for p in script.parts if p.type == "voice" and p.text)
                    break_texts.append(full_text)
                    state.record_ad_spot(
                        brand=brand.name,
                        summary=script.summary,
                        format=script.format,
                        sonic_signature=brand.campaign.sonic_signature if brand.campaign else "",
                        environment=script.sonic.environment if script.sonic else "",
                        music_bed=script.mood or (script.sonic.music_bed if script.sonic else ""),
                        transition_motif=script.sonic.transition_motif if script.sonic else "",
                    )
                    if spot_idx < num_spots - 1 and spot_idx < len(mid_bumpers):
                        break_parts.append(mid_bumpers[spot_idx])

                _emit_segment_prepared(
                    state,
                    segment_id=_ad_attempt_id,
                    role="ad_break",
                    final_script=break_texts,
                    collector=_ad_collector,
                )

                # ── PHASE 4: Closing bumper + outro in parallel ──
                bumper_out = config.tmp_dir / f"bumper_out_{uuid4().hex[:8]}.mp3"
                outro_host = random.choice(config.hosts)
                outro_path = config.tmp_dir / f"ad_outro_{uuid4().hex[:8]}.mp3"
                outro_text = random.choice(_sw.AD_BREAK_OUTROS)
                await asyncio.gather(
                    loop.run_in_executor(None, generate_bumper_jingle, bumper_out),
                    synthesize(
                        outro_text,
                        outro_host.voice,
                        outro_path,
                        engine=outro_host.engine,
                        edge_fallback_voice=outro_host.edge_fallback_voice,
                        state=state,
                    ),
                )
                break_parts.append(bumper_out)
                break_parts.append(outro_path)

                # ── PHASE 5: Final concat (skip loudnorm — all parts pre-normalized) ──
                if len(break_parts) == 1:
                    ad_break_path = break_parts[0]
                else:
                    ad_break_path = config.tmp_dir / f"adbreak_{uuid4().hex[:8]}.mp3"
                    try:
                        await loop.run_in_executor(
                            None,
                            concat_files,
                            break_parts,
                            ad_break_path,
                            300,
                            False,
                        )
                    finally:
                        for p in break_parts:
                            p.unlink(missing_ok=True)

                if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
                    try:
                        await loop.run_in_executor(None, validate_segment_audio, ad_break_path, SegmentType.AD)
                    except AudioToolError as exc:
                        logger.warning("Audio tool unavailable, skipping ad quality check: %s", exc)
                    except AudioQualityError as exc:
                        logger.warning("Quality gate rejected ad break (%s): %s", ad_break_path.name, exc)
                        ad_break_path.unlink(missing_ok=True)
                        # Prevent scheduler lock on AD if we reject a full break.
                        state.songs_since_ad = 0
                        continue

                # Dashboard display: show all brands in the break
                state.last_ad_script = {
                    "brands": break_brands,
                    "texts": break_texts,
                    "summaries": break_summaries,
                    "formats": break_formats,
                    "spots": num_spots,
                    "sonic_worlds": break_sonic_worlds,
                    "roles_used": break_roles,
                }
                segment = Segment(
                    type=SegmentType.AD,
                    path=ad_break_path,
                    metadata={
                        "type": "ad_break",
                        "brands": break_brands,
                        "spots": num_spots,
                        "formats": break_formats,
                        "sonic_worlds": break_sonic_worlds,
                        "roles_used": break_roles,
                        "title": _ad_title(break_brands),
                        "has_music_tail": bool(intro_has_music_tail),
                        "ledger_segment_id": _ad_attempt_id,
                    },
                )
                _bound_brands = break_brands

                def _ad_callback(_b=_bound_brands) -> None:
                    state.after_ad(brands=_b)

                success_callback = _ad_callback

        except Exception as e:
            # Recoverable: network/ffmpeg/disk/httpx errors — use canned banter or silence
            logger.error("Failed to produce %s segment: %s", seg_type.value, e)
            state.failed_segments += 1
            # Backoff on persistent failures to avoid CPU-burning tight loop
            consecutive = state.failed_segments
            if consecutive > 1:
                backoff = min(30.0, 2.0 ** min(consecutive, 5))
                logger.warning("Consecutive failures: %d — backing off %.0fs", consecutive, backoff)
                await asyncio.sleep(backoff)
            # Prefer a canned banter clip over raw silence — at least it sounds intentional
            fallback_path = _pick_canned_clip("banter", state=state) or _pick_canned_clip("welcome")
            if fallback_path:
                logger.info("Error recovery: using canned clip instead of silence")
                segment = Segment(
                    type=SegmentType.BANTER,
                    path=fallback_path,
                    metadata={
                        "type": "banter",
                        "canned": True,
                        "error_recovery": True,
                        "title": "Recovery banter",
                    },
                    ephemeral=False,
                )
            else:
                logger.warning("No canned clips available — inserting silence (check assets/demo/banter/)")
                silence_path = config.tmp_dir / f"silence_{uuid4().hex[:8]}.mp3"
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, generate_silence, silence_path, 5.0)
                except Exception as silence_err:
                    logger.error("Cannot generate silence (ffmpeg broken?): %s", silence_err)
                    await asyncio.sleep(0.5)
                    continue
                segment = Segment(
                    type=seg_type,
                    path=silence_path,
                    metadata={"error": str(e), "title": "Brief silence"},
                )
            # Do NOT advance state counters — failed segment doesn't count

        if segment:
            actual_seg_type = segment.type
            if (
                prev_seg_type is not None
                and _crosses_music_speech_boundary(prev_seg_type, actual_seg_type)
                and not segment.metadata.get("has_music_tail")
            ):
                try:
                    loop = asyncio.get_running_loop()
                    sting_path = config.tmp_dir / f"transition_{uuid4().hex[:8]}.mp3"
                    imaging_lib = _make_imaging_lib(config)
                    await loop.run_in_executor(
                        None,
                        imaging_lib.pick_stinger,
                        prev_seg_type,
                        actual_seg_type,
                        sting_path,
                    )
                    merged_path = config.tmp_dir / f"segment_with_sting_{uuid4().hex[:8]}.mp3"
                    pre_sting_path = segment.path
                    pre_sting_ephemeral = segment.ephemeral
                    try:
                        await loop.run_in_executor(
                            None,
                            concat_files,
                            [sting_path, segment.path],
                            merged_path,
                            0,
                            False,
                        )
                    except Exception:
                        merged_path.unlink(missing_ok=True)
                        raise
                    finally:
                        sting_path.unlink(missing_ok=True)
                    if pre_sting_ephemeral:
                        pre_sting_path.unlink(missing_ok=True)
                    segment = replace(segment, path=merged_path, ephemeral=True)
                except Exception as exc:
                    logger.warning("Transition sting generation failed, using clean cut: %s", exc)
            segment.duration_sec = await asyncio.to_thread(_probe_segment_duration, segment.path)
            if generation_revision != state.playlist_revision:
                logger.info("Discarding stale %s segment after playlist source switch", seg_type.value)
                _unlink_if_tmp_render(segment, config.tmp_dir)
                if is_operator_forced:
                    state.operator_force_pending = None  # render abandoned — let the operator retry
                continue
            if generation_chaos_epoch != state.chaos_cutover_epoch:
                logger.info("Discarding stale %s segment after chaos cutover", seg_type.value)
                _unlink_if_tmp_render(segment, config.tmp_dir)
                if is_operator_forced:
                    state.operator_force_pending = None  # render abandoned — let the operator retry
                continue
            # Stable per-segment id: stamped on the Segment metadata AND the
            # shadow-list entry so /api/queue/remove can target a segment by
            # identity rather than position (the position shifts every time the
            # streamer consumes the head).
            queue_id = uuid4().hex
            segment.metadata["queue_id"] = queue_id
            shadow_entry = {
                "id": queue_id,
                "type": segment.type.value,
                "label": segment.metadata.get("title", segment.type.value),
                "spotify_id": segment.metadata.get("spotify_id", ""),
                "reason": segment.metadata.get("queue_reason", "Rendered and queued for playback."),
                "playlist_index": segment.metadata.get("playlist_index", -1),
                "source_kind": segment.metadata.get("source_kind", ""),
                "duration_sec": round(segment.duration_sec or 0, 1),
            }
            if is_operator_forced:
                # Air-next: front-insert past the buffered lookahead so the operator
                # hears their pick at the next boundary, never minutes later.
                if not _front_insert_queue_and_shadow(queue, state, segment, shadow_entry):
                    continue
                if "error" not in segment.metadata:
                    prev_seg_type = segment.type
            else:
                if not await _queue_segment(segment):
                    continue
                state.queued_segments.append(shadow_entry)
            if chaos_subtype is not None and state.chaos_pending == chaos_subtype:
                state.chaos_pending = None
            if chaos_subtype == ChaosSubtype.URGENT_INTERRUPT:
                state.ha_pending_directive = ""
                # The safety-belt force_next was set when the interrupt fired.
                # chaos_pending already produced the banter; clearing here
                # prevents the producer from queueing an extra banter next cycle.
                if state.force_next == SegmentType.BANTER:
                    state.force_next = None
            _segments_produced += 1
            # Queue appended → up_next changed → integration consumers polling
            # ``changed_at`` need to see this even without a segment transition.
            state.last_state_change_at = time.time()
            if "error" not in segment.metadata:
                if success_callback:
                    success_callback()
                state.failed_segments = 0  # Reset backoff on success
                _drain_guard_queued = False  # Real segment landed — allow drain guard to fire again if needed
                # #144/#146: Launch background normalization of the predicted next music track.
                # By the time the current track finishes playing (~3-4 min), the next norm
                # is already cached — avoids the 75-second Pi stall when the queue drains.
                if segment.type == SegmentType.MUSIC and state.force_next is None and state.playlist:
                    if _prefetch_task and not _prefetch_task.done():
                        _prefetch_task.cancel()
                    _prefetch_task = asyncio.create_task(
                        _prefetch_next(state, config, _prefetch_failed_keys),
                        name="prefetch-norm",
                    )
            logger.info(
                "Queued %s in %.1fs (queue size: %d)",
                segment.type.value,
                time.perf_counter() - _t_render,
                queue.qsize(),
            )
