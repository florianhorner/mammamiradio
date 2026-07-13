"""Segment production pipeline for music, banter, and ad breaks."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime
import logging
import os
import random
import re
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx

import mammamiradio.hosts.scriptwriter as _sw
from mammamiradio.audio.audio_quality import AudioQualityError, AudioToolError, validate_segment_audio
from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.audio.norm_cache import select_norm_cache_rescue
from mammamiradio.audio.normalizer import (
    apply_broadcast_chain,
    broadcast_chain_version,
    concat_files,
    crossfade_voice_over_music,
    generate_bumper_jingle,
    generate_station_id_bed,
    generate_tone,
    humanize_norm_filename,
    load_track_metadata,
    mix_oneshot_sfx,
    mix_quiet_bleed,
    mix_voice_with_bed,
    mix_voice_with_sting,
    norm_cache_duration_sec,
    normalize,
    probe_duration_sec,
    reconcile_cached_music,
    refresh_track_metadata,
    save_track_metadata,
)
from mammamiradio.audio.tts import synthesize, synthesize_ad, synthesize_dialogue
from mammamiradio.core.config import RadioEventRule, StationConfig
from mammamiradio.core.models import (
    AdHistoryEntry,
    ChaosSubtype,
    GenerationWasteReason,
    InterruptSpec,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR as _DEMO_ASSETS_DIR
from mammamiradio.core.packaged_assets import is_packaged_asset
from mammamiradio.home.authorization import (
    HomeAuthorization,
    HomeAuthorizationMode,
    expand_muted_with_ambient_sources,
)
from mammamiradio.home.catalog import schedule_label_generation
from mammamiradio.home.context_director import DirectorObservation, PromptFact
from mammamiradio.home.entity_policy import (
    load_entity_policy,
    muted_entity_ids,
)
from mammamiradio.home.ha_context import (
    ENTITY_LABELS,
    GOLD_ENTITIES,
    HomeContext,
    _fetch_home_context_outcome,
    _HomeContextFetchOutcome,
    _publish_home_context_outcome,
    apply_entity_mute_policy,
    check_reactive_triggers,
    fetch_home_context,
    get_cached_home_context,
    push_state_to_ha,
    revalidate_home_context_mutes,
)
from mammamiradio.home.ha_enrichment import HomeEvent
from mammamiradio.home.radio_events import RadioEventMatch, commit_radio_event_directive
from mammamiradio.home.ritual_recipes import RitualRecipeMatch, commit_ritual_recipe_match
from mammamiradio.home.scene_namer import resolve_home_mood
from mammamiradio.hosts.ad_creative import _cast_voices, _pick_brand, _select_ad_creative
from mammamiradio.hosts.context_cues import generate_impossible_line
from mammamiradio.hosts.station_name_guard import strip_foreign_station_name
from mammamiradio.playlist.downloader import (
    download_track,
    evict_cache_lru,
    is_rejected_cache_key,
    reject_cached_download,
    validate_download,
)
from mammamiradio.playlist.music_admission import classify_youtube_candidate, is_youtube_music_candidate
from mammamiradio.playlist.playlist import fetch_chart_refresh, filter_blocklisted
from mammamiradio.playlist.track_rationale import classify_track_crate, generate_track_rationale
from mammamiradio.restart_handoff import RestartHandoffCandidate, try_write_restart_handoff_spool
from mammamiradio.scheduling.scheduler import buffered_audio_seconds, next_segment_type

logger = logging.getLogger(__name__)
# Kept as a module-level compatibility seam for existing downstream test
# fixtures.  The producer's live path uses _fetch_home_context_outcome above.
_LEGACY_FETCH_HOME_CONTEXT = fetch_home_context
_REAL_ASYNCIO_SLEEP = asyncio.sleep
CHAOS_AUDIO_FAILURE_BACKOFF_SECONDS = 0.5
CHAOS_AUDIO_FAILURE_LIMIT = 5

MUSIC_SELECTION_RETRIES = 20
MUSIC_QUALITY_GATE_REJECTION_LIMIT = 3
CACHE_EVICTION_INTERVAL_SECONDS = 3600
PLAYLIST_REFRESH_INTERVAL_SECONDS = 5400.0
RECOVERY_SWEEPER_TIMEOUT_SECONDS = 3.0
RECOVERY_SWEEPER_LINES = (
    "{station} resta in onda. La musica sta tornando.",
    "Restate con noi. Stiamo rimettendo la puntina al posto giusto.",
    "Un attimo in cabina. La musica torna tra pochissimo.",
    "Respiriamo un secondo e ripartiamo. Sempre qui su {station}.",
)


@contextlib.contextmanager
def _timed_render_stage(state: StationState | None, stage: str) -> Iterator[None]:
    """Measure one real producer boundary without letting diagnostics affect audio."""
    if state is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        state.add_render_stage_timing(stage, (time.monotonic() - started) * 1000)


_RUNWAY_GOVERNED_TYPES = {
    SegmentType.BANTER,
    SegmentType.AD,
    SegmentType.NEWS_FLASH,
    SegmentType.STATION_ID,
    SegmentType.TIME_CHECK,
}
RUNWAY_FLOOR_SECONDS = 240
FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE = (
    "FIRST CONNECTED HOME MOMENT: Use one or two concrete home details naturally. "
    "Do not list sensors. Make it feel like a host casually noticing the home."
)
FIRST_HOME_CONTEXT_MIN_ENTITIES = 3
# The one-time cold HA warm-up (states + registry websocket snapshot + weather,
# all on a cache miss) legitimately takes far longer than a steady-state refresh.
# Budgeting that first load at the tight context_refresh_timeout would cancel the
# registry websocket every time on a sluggish HA, so the device-label catalog
# would never populate. Give the first load (no usable cache yet) this longer
# budget — still bounded so a fully-hung HA can't block production forever — and
# apply the tight steady-state budget to every refresh after.
_HA_CONTEXT_COLD_LOAD_TIMEOUT = 20.0
# The foreground deadline protects the audio path.  It intentionally does not
# cancel the owned HA request: a late result can still improve the *next* safe
# prompt boundary, but never a segment already being rendered or queued.
_HA_CONTEXT_BACKGROUND_TIMEOUT = 30.0
_HA_CONTEXT_MIN_STALE_SECONDS = 120.0


def _legacy_mock_home_context(value: object) -> HomeContext:
    """Normalize older producer-fixture returns into the typed outcome contract.

    Production always calls ``_fetch_home_context_outcome``.  This small
    compatibility seam keeps third-party/test fixtures that historically
    replaced the producer-local ``fetch_home_context`` dependency from turning
    into an untyped background task; it is inactive unless that dependency was
    explicitly rebound.
    """
    if isinstance(value, HomeContext):
        return value

    def _text(name: str) -> str:
        candidate = getattr(value, name, "")
        return candidate if isinstance(candidate, str) else ""

    def _mapping(name: str) -> dict:
        candidate = getattr(value, name, {})
        return dict(candidate) if isinstance(candidate, dict) else {}

    def _list(name: str) -> list:
        candidate = getattr(value, name, [])
        return list(candidate) if isinstance(candidate, list | tuple) else []

    raw_events = getattr(value, "events", ())
    events = (
        deque(raw_events, maxlen=getattr(raw_events, "maxlen", None) or 20)
        if isinstance(raw_events, deque | list | tuple)
        else deque(maxlen=20)
    )
    raw_timestamp = getattr(value, "timestamp", 0.0)
    timestamp = (
        float(raw_timestamp) if isinstance(raw_timestamp, int | float) and not isinstance(raw_timestamp, bool) else 0.0
    )
    raw_catalog_hit_rate = getattr(value, "catalog_hit_rate", 0.0)
    catalog_hit_rate = (
        float(raw_catalog_hit_rate)
        if isinstance(raw_catalog_hit_rate, int | float) and not isinstance(raw_catalog_hit_rate, bool)
        else 0.0
    )
    return HomeContext(
        raw_states=_mapping("raw_states"),
        summary=_text("summary"),
        events=events,
        radio_events=_list("radio_events"),
        ritual_recipe_matches=_list("ritual_recipe_matches"),
        ritual_public_families=_list("ritual_public_families"),
        ritual_recipe_audit=_list("ritual_recipe_audit"),
        events_summary=_text("events_summary"),
        timestamp=timestamp,
        mood=_text("mood"),
        weather_arc=_text("weather_arc"),
        mood_en=_text("mood_en"),
        weather_arc_en=_text("weather_arc_en"),
        events_summary_en=_text("events_summary_en"),
        last_event_label_en=_text("last_event_label_en"),
        scored=_list("scored"),
        catalog_hit_rate=catalog_hit_rate,
        label_stats=_mapping("label_stats"),
        registry_source=_text("registry_source"),
        denylist_hits=_mapping("denylist_hits"),
    )


def _uses_injected_legacy_fetch() -> bool:
    """Whether an embedding replaced the historical producer fetch seam."""
    return fetch_home_context is not _LEGACY_FETCH_HOME_CONTEXT


async def _fetch_producer_context_outcome(
    *,
    ha_url: str,
    ha_token: str,
    poll_interval: float,
    cache: HomeContext | None,
    cache_dir: Path,
    radio_event_rules: list[RadioEventRule] | None,
    authorization: HomeAuthorization | None = None,
    observed_entity_ids_callback: Callable[[frozenset[str]], None] | None = None,
) -> _HomeContextFetchOutcome:
    """Fetch the typed mailbox outcome, preserving the legacy injected seam."""
    if not _uses_injected_legacy_fetch():
        return await _fetch_home_context_outcome(
            ha_url=ha_url,
            ha_token=ha_token,
            poll_interval=poll_interval,
            _cache=cache,
            cache_dir=cache_dir,
            radio_event_rules=radio_event_rules,
            authorization=authorization,
            observed_entity_ids_callback=observed_entity_ids_callback,
        )

    started_at = time.time()
    started_monotonic = time.monotonic()
    context = _legacy_mock_home_context(
        await fetch_home_context(
            ha_url=ha_url,
            ha_token=ha_token,
            poll_interval=poll_interval,
            _cache=cache,
            cache_dir=cache_dir,
            radio_event_rules=radio_event_rules,
            authorization=authorization,
            observed_entity_ids_callback=observed_entity_ids_callback,
        )
    )
    # Fixture contexts sometimes omit a timestamp.  It is still a completed
    # injected fetch, so give the synthetic source snapshot an adoption stamp.
    snapshot_timestamp = max(context.timestamp, time.time())
    if context.timestamp <= 0:
        context = replace(context, timestamp=snapshot_timestamp)
    return _HomeContextFetchOutcome(
        kind="fresh",
        context=context,
        snapshot_timestamp=snapshot_timestamp,
        attempt_started_at=started_at,
        attempt_finished_at=time.time(),
        duration_seconds=max(0.0, time.monotonic() - started_monotonic),
    )


@dataclass(frozen=True)
class _PendingRitualInterrupt:
    match: RitualRecipeMatch
    spec: InterruptSpec


@dataclass(frozen=True)
class RenderedMusicTrack:
    track: Track
    path: Path
    cache_path: Path
    cache_hit: bool


def _select_accepted_music_track(state: StationState, config: StationConfig) -> Track | None:
    rejected_keys = {track.cache_key for track in state.playlist if is_rejected_cache_key(track.cache_key)}
    if state.pinned_track is not None and is_rejected_cache_key(state.pinned_track.cache_key):
        rejected_keys.add(state.pinned_track.cache_key)
    try:
        candidate = state.select_next_track(
            repeat_cooldown=config.playlist.repeat_cooldown,
            artist_cooldown=config.playlist.artist_cooldown,
            excluded_cache_keys=rejected_keys,
        )
    except RuntimeError as exc:
        if not state.playlist or str(exc) == "Playlist is empty":
            raise
        if rejected_keys:
            logger.debug("No eligible music tracks remain after excluding session-rejected cache keys")
            return None
        raise
    return candidate


def _arm_accepted_heading_announcement(state: StationState, track: Track) -> None:
    state._arm_heading_announcement_if_needed(track)


def _probe_segment_duration(path: Path, *, rescue: bool = False) -> float:
    """Run ffprobe on path and return duration in seconds; 0.0 if probe fails.

    ``rescue`` routes the probe through the bounded rescue ffmpeg slot —
    bridge and error-recovery fills must never queue behind ordinary
    normalization jobs holding both plain slots (#2 INSTANT AUDIO).
    """
    return probe_duration_sec(path, rescue=rescue) or 0.0


def _is_packaged_asset(path: Path) -> bool:
    return is_packaged_asset(path, _DEMO_ASSETS_DIR)


def _is_tmp_render(segment: Segment, tmp_dir: Path) -> bool:
    if _is_packaged_asset(segment.path):
        return False
    if segment.ephemeral:
        return True
    try:
        return segment.path.resolve().is_relative_to(tmp_dir.resolve())
    except OSError:
        return False


def _unlink_if_tmp_render(segment: Segment, tmp_dir: Path) -> None:
    if _is_tmp_render(segment, tmp_dir):
        segment.path.unlink(missing_ok=True)


def _record_generated_waste(
    state: StationState,
    seg_type: SegmentType,
    path: Path,
    reason: str,
    duration_sec: float = 0.0,
    *,
    ephemeral: bool = True,
) -> None:
    """Record a render dropped before broadcast as generation waste (#397).

    ``record_discard`` only reads a Segment's type/duration, so build a minimal
    one at quality-gate reject sites that drop a render before a full Segment
    object exists. Best-effort — never gates the audio path.
    """
    state.record_discard(
        Segment(type=seg_type, path=path, duration_sec=duration_sec, ephemeral=ephemeral),
        reason=reason,
    )


def _is_under(path: Path, directory: Path) -> bool:
    """True when ``path`` resolves to a location inside ``directory`` (best-effort)."""
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:
        return False


def _normalized_cache_path(track: Track, config: StationConfig) -> Path:
    return config.cache_dir / f"norm_{track.cache_key}_{config.audio.bitrate}k.mp3"


def _norm_cache_bridge_payload(
    norm_path: Path,
    bridge_flag: str,
    station_name: str,
    *,
    bitrate_kbps: int | float | None = None,
) -> tuple[dict, str]:
    _meta = load_track_metadata(norm_path) or {}
    raw_title = str(_meta.get("title") or humanize_norm_filename(norm_path.name))
    # Illusion guard: a poisoned sidecar (a foreign "Radio X" station name) must
    # never surface as the now-playing artist/title on the listener UI / Music
    # Assistant provider. Strip the artist (drop to title-only) and prefix-strip
    # the title ("Radio X - Song" -> "Song", but keep a song really titled
    # "Radio Ga Ga") — mirroring the streamer rescue paths and the HA now-playing
    # path, so every sidecar->metadata source scrubs at its origin instead of one
    # surface getting protected while a sibling bridge leaks.
    title = strip_foreign_station_name(raw_title, station_name, prefix_only=True) or raw_title
    artist = strip_foreign_station_name(str(_meta.get("artist") or ""), station_name)
    duration_sec = norm_cache_duration_sec(norm_path, bitrate_kbps=bitrate_kbps)
    duration_fields = {"duration_ms": round(duration_sec * 1000)} if duration_sec > 0 else {}
    detail = f"{artist} - {title}" if artist else title
    return (
        {
            "title": title,
            "artist": artist,
            **duration_fields,
            bridge_flag: True,
            "rescue": True,
            "audio_source": "norm_cache",
        },
        f"{norm_path.name} ({detail})",
    )


def _duration_sec_from_metadata(metadata: dict) -> float:
    duration_ms = metadata.get("duration_ms")
    if isinstance(duration_ms, bool):
        return 0.0
    if isinstance(duration_ms, int | float) and duration_ms > 0:
        return float(duration_ms) / 1000.0
    return 0.0


def _record_bridge_fire(state: StationState, bridge_type: str, source: str) -> None:
    """Record a producer rescue-bridge fire and emit a structured log event.

    Wraps ``StationState.record_bridge_fire`` (#547 observability) and logs a
    ``producer_bridge_fire`` event in the same ``extra={"event": ...}`` house
    style as ``provider_health_state``. Best-effort: a telemetry failure must
    never break the audio path, so everything is contained.
    """
    try:
        state.record_bridge_fire(bridge_type, source)
        logger.info(
            "producer_bridge_fire",
            extra={"event": "producer_bridge_fire", "bridge_type": bridge_type, "source": source},
        )
    except Exception:  # pragma: no cover - telemetry must never kill the producer
        logger.debug("bridge fire telemetry failed", exc_info=True)


async def _render_music_track(
    track: Track,
    config: StationConfig,
    *,
    temp_prefix: str,
    context: str,
    cache_write_required: bool = False,
    background: bool = False,
    playlist: list[Track] | None = None,
    timing_state: StationState | None = None,
) -> RenderedMusicTrack | None:
    """Download, validate, normalize, and cache one music track."""
    audio_path = await download_track(track, config.cache_dir, music_dir=Path("music"), background=background)
    loop = asyncio.get_running_loop()
    validate_fn = partial(validate_download, audio_path, background=background)
    ok, reason = await loop.run_in_executor(None, validate_fn)
    if not ok:
        reject_cached_download(config.cache_dir, track.cache_key, reason)
        logger.warning("Skipping %s track due to invalid download (%s): %s", context, track.display, reason)
        return None

    try:
        should_probe_actual = audio_path.exists()
    except OSError:
        should_probe_actual = False
    actual_duration_ms: int | None = None
    if should_probe_actual and is_youtube_music_candidate(track):
        actual_duration_sec = await loop.run_in_executor(None, _probe_segment_duration, audio_path)
        if actual_duration_sec > 0:
            actual_duration_ms = round(actual_duration_sec * 1000)
        envelope_playlist = (
            [candidate for candidate in playlist if candidate.cache_key != track.cache_key]
            if playlist is not None
            else []
        )
        verdict = classify_youtube_candidate(
            track,
            envelope_playlist,
            config.pacing,
            actual_duration_sec=actual_duration_sec if actual_duration_sec > 0 else None,
        )
        if not verdict.accepted:
            reject_cached_download(config.cache_dir, track.cache_key, verdict.reason)
            logger.warning(
                "Skipping %s track held out of rotation (%s): %s",
                context,
                verdict.reason,
                track.display,
            )
            return None
        if actual_duration_ms is not None:
            track.duration_ms = actual_duration_ms

    # The producer's existing ``finding`` phase owns source/download timing.
    # Close it before normalization so a slow Pi encode cannot be misreported as
    # source latency. Direct helper callers omit timing_state and stay unchanged.
    if timing_state is not None:
        timing_state.end_gen(ok=True)

    with _timed_render_stage(timing_state, "normalize"):
        norm_cached = _normalized_cache_path(track, config)
        if norm_cached.exists():
            logger.debug("Normalization cache hit%s: %s", f" ({context})" if context else "", norm_cached.name)
            # A cache hit skips normalize() + its reconcile pass, so a file produced
            # before reconciliation existed would air at its old level. Reconcile it on
            # hit (off the event loop) so every song lands at the target; skipped once
            # the sidecar marks it done, so steady-state cache hits stay instant.
            reconcile_fn = partial(reconcile_cached_music, norm_cached, background=background)
            await loop.run_in_executor(None, reconcile_fn)
            await loop.run_in_executor(
                None,
                partial(refresh_track_metadata, norm_cached, track.title, track.artist, duration_ms=track.duration_ms),
            )
            return RenderedMusicTrack(track=track, path=norm_cached, cache_path=norm_cached, cache_hit=True)

        norm_path = config.tmp_dir / f"{temp_prefix}_{uuid4().hex[:8]}.mp3"
        _norm_fn = partial(
            normalize,
            audio_path,
            norm_path,
            config,
            loudnorm=True,
            music_eq=True,
            background=background,
        )
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
            # copy2 can leave a partial norm_cached behind. Remove it on every
            # failure so a corrupt file can never be selected for recovery or
            # continuity playback, not only on the cache-required path.
            with contextlib.suppress(OSError):
                norm_cached.unlink(missing_ok=True)
            if cache_write_required:
                norm_path.unlink(missing_ok=True)
                raise
        else:
            save_track_metadata(norm_cached, track.title, track.artist, duration_ms=track.duration_ms)
        return RenderedMusicTrack(track=track, path=norm_path, cache_path=norm_cached, cache_hit=False)


_RECOVERY_CLIP_SUBDIRS = ("recovery", "banter", "welcome")


def _pick_recovery_clip(state: StationState) -> Path | None:
    """Pick a packaged continuity clip for recovery ladders."""
    for subdir in _RECOVERY_CLIP_SUBDIRS:
        clip = _pick_canned_clip(subdir, state=state)
        if clip:
            return clip
    return None


async def _queue_continuity_bridge(
    queue_segment: Callable[[Segment], Awaitable[bool]],
    state: StationState,
    config: StationConfig,
    *,
    bridge_type: str,
    bridge_flag: str,
    canned_title: str,
    canned_metadata: dict | None = None,
    music_runway: bool = False,
) -> bool:
    """Queue the best available producer-side continuity bridge."""
    fallback = _pick_recovery_clip(state)
    if fallback:
        duration_sec = await asyncio.to_thread(_probe_segment_duration, fallback, rescue=True)
        duration_fields = {"duration_ms": round(duration_sec * 1000)} if duration_sec > 0 else {}
        metadata = {
            "type": "banter",
            "canned": True,
            bridge_flag: True,
            "rescue": True,
            "title": canned_title,
            **duration_fields,
        }
        if canned_metadata:
            protected_keys = {"type", "canned", bridge_flag, "rescue", "title", "duration_ms"}
            metadata.update({key: value for key, value in canned_metadata.items() if key not in protected_keys})
        logger.warning("%s bridge: inserting packaged recovery clip", bridge_type.capitalize())
        ok = await queue_segment(
            Segment(
                type=SegmentType.BANTER,
                path=fallback,
                duration_sec=duration_sec,
                metadata=metadata,
                ephemeral=False,
            )
        )
        if ok:
            _record_bridge_fire(state, bridge_type, "canned")
            if music_runway and not await _queue_norm_cache_bridge_segment(
                queue_segment,
                state,
                config,
                bridge_type=bridge_type,
                bridge_flag=bridge_flag,
            ):
                logger.info(
                    "%s bridge: no runway music segment queued behind the canned clip",
                    bridge_type.capitalize(),
                )
        return ok

    ok = await _queue_norm_cache_bridge_segment(
        queue_segment,
        state,
        config,
        bridge_type=bridge_type,
        bridge_flag=bridge_flag,
    )
    if ok:
        _record_bridge_fire(state, bridge_type, "norm_cache")
        return ok

    tone_path = _DEMO_ASSETS_DIR / "recovery" / "emergency_tone.mp3"
    if not tone_path.is_file():
        logger.error("%s bridge: packaged emergency tone is missing", bridge_type.capitalize())
        return False
    logger.error(
        "%s bridge: no canned clips or norm cache available — inserting packaged emergency tone",
        bridge_type.capitalize(),
    )
    ok = await queue_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=tone_path,
            duration_sec=2.0,
            metadata={
                "title": "Station continuity",
                "artist": "",
                "duration_ms": 2000,
                bridge_flag: True,
                "rescue": True,
                "audio_source": "emergency_tone",
            },
            ephemeral=False,
        )
    )
    if ok:
        _record_bridge_fire(state, bridge_type, "emergency_tone")
    return ok


async def _queue_norm_cache_bridge_segment(
    queue_segment: Callable[[Segment], Awaitable[bool]],
    state: StationState,
    config: StationConfig,
    *,
    bridge_type: str,
    bridge_flag: str,
) -> bool:
    norm_path = select_norm_cache_rescue(config.cache_dir, state)
    if not norm_path:
        return False
    metadata, log_label = _norm_cache_bridge_payload(
        norm_path,
        bridge_flag,
        config.display_station_name,
        bitrate_kbps=config.audio.bitrate,
    )
    logger.warning(
        "%s bridge: inserting norm-cache bridge: %s",
        bridge_type.capitalize(),
        log_label,
    )
    return await queue_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=norm_path,
            duration_sec=_duration_sec_from_metadata(metadata),
            metadata=metadata,
            ephemeral=False,
        )
    )


async def _producer_error_recovery_segment(state: StationState, config: StationConfig) -> Segment | None:
    """Build the best non-silent segment for broad producer exception recovery."""
    fallback_path = _pick_recovery_clip(state)
    if fallback_path:
        duration_sec = await asyncio.to_thread(_probe_segment_duration, fallback_path, rescue=True)
        duration_fields = {"duration_ms": round(duration_sec * 1000)} if duration_sec > 0 else {}
        logger.info("Error recovery: using packaged recovery clip")
        return Segment(
            type=SegmentType.BANTER,
            path=fallback_path,
            duration_sec=duration_sec,
            metadata={
                "type": "banter",
                "canned": True,
                "error_recovery": True,
                "rescue": True,
                "title": "Station continuity",
                **duration_fields,
            },
            ephemeral=False,
        )

    norm_path = select_norm_cache_rescue(config.cache_dir, state)
    if norm_path:
        metadata, log_label = _norm_cache_bridge_payload(norm_path, "error_recovery", config.display_station_name)
        logger.warning("Error recovery: using norm-cache rescue instead of silence: %s", log_label)
        return Segment(
            type=SegmentType.MUSIC,
            path=norm_path,
            duration_sec=_duration_sec_from_metadata(metadata),
            metadata=metadata,
            ephemeral=False,
        )

    last_good = _get_last_music_file(state)
    last_good_title = ""
    last_good_artist = ""
    if last_good:
        last_good_meta = load_track_metadata(last_good) or {}
        last_good_raw_title = str(last_good_meta.get("title") or "").strip()
        last_good_raw_artist = str(last_good_meta.get("artist") or "").strip()
        if state.blocklist:
            if last_good_raw_title and last_good_raw_artist:
                last_good_key = (last_good_raw_artist.lower(), last_good_raw_title.lower())
                if last_good_key in state.blocklist:
                    logger.warning(
                        "Error recovery: skipping blocklisted last-known-good music: %s - %s",
                        last_good_raw_artist,
                        last_good_raw_title,
                    )
                    last_good = None
            else:
                logger.warning(
                    "Error recovery: skipping unidentified last-known-good music while blocklist is active: %s",
                    last_good.name,
                )
                last_good = None
        if last_good_raw_title:
            last_good_title = strip_foreign_station_name(
                last_good_raw_title, config.display_station_name, prefix_only=True
            )
        elif last_good:
            last_good_title = last_good.name
        last_good_artist = strip_foreign_station_name(last_good_raw_artist, config.display_station_name)
    if last_good:
        duration_sec = await asyncio.to_thread(_probe_segment_duration, last_good, rescue=True)
        logger.warning(
            "Error recovery: no packaged recovery clips or norm cache — recycling last-known-good music: %s",
            last_good.name,
        )
        return Segment(
            type=SegmentType.MUSIC,
            path=last_good,
            duration_sec=duration_sec,
            metadata={
                "type": "music",
                "recycled": True,
                "error_recovery": True,
                "rescue": True,
                "title": last_good_title or last_good.name,
                "artist": last_good_artist,
                "title_only": last_good_title or last_good.name,
                "audio_source": "last_known_good",
            },
            ephemeral=False,
        )

    try:
        logger.warning(
            "No packaged recovery clips, norm cache, or last-known-good music available — inserting recovery sweeper"
        )
        return await asyncio.wait_for(
            _build_recovery_sweeper_segment(config, state),
            timeout=RECOVERY_SWEEPER_TIMEOUT_SECONDS,
        )
    except Exception as sweeper_err:
        logger.warning("Recovery sweeper failed — inserting emergency tone: %s", sweeper_err)

    tone_path = _DEMO_ASSETS_DIR / "recovery" / "emergency_tone.mp3"
    logger.error(
        "No packaged recovery clips, norm cache, or recovery sweeper available — inserting packaged emergency tone"
    )
    if not tone_path.is_file():
        logger.error("Packaged emergency tone recovery asset is missing")
        return None
    return Segment(
        type=SegmentType.MUSIC,
        path=tone_path,
        duration_sec=2.0,
        metadata={
            "title": "Station continuity",
            "artist": "",
            "duration_ms": 2000,
            "error_recovery": True,
            "rescue": True,
            "audio_source": "emergency_tone",
        },
        ephemeral=False,
    )


async def _queue_drain_recovery_bridge(
    queue_segment: Callable[[Segment], Awaitable[bool]],
    state: StationState,
    config: StationConfig,
) -> bool:
    """Queue a drain bridge and, when available, cached music runway."""
    return await _queue_continuity_bridge(
        queue_segment,
        state,
        config,
        bridge_type="drain",
        bridge_flag="queue_drain_recovery",
        canned_title="Station continuity",
        music_runway=True,
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


# SFX assets (alert jingle used as interrupt bridge audio).
_SFX_DIR = Path(__file__).resolve().parent.parent / "assets" / "sfx"

# Global cooldown for interrupt firing — kept separate from per-entity
# spec.cooldown so a timer configured with cooldown=300 doesn't suppress a
# different timer's interrupt for 5 minutes.
_GLOBAL_INTERRUPT_COOLDOWN_SECONDS = 60


def _mark_moment_dropped(state: StationState, moment_id: str, reason: str, context: str) -> None:
    """Best-effort receipt demotion; never lets receipt bookkeeping affect audio."""
    if not moment_id or state.moment_store is None:
        return
    try:
        state.moment_store.mark_dropped(moment_id, reason)
    except Exception:  # pragma: no cover - receipts must never break production
        logger.debug("Moment receipt %s drop failed", context, exc_info=True)


def _drop_segment_moment_receipts(state: StationState, segment: Segment, reason: str, context: str) -> None:
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    for key in ("ritual_moment_id", "gag_moment_id"):
        _mark_moment_dropped(state, str(metadata.get(key) or ""), reason, f"{context}:{key}")


# Legacy process-local cache used only by ``_latest_music_file`` as a tmp-directory
# scan shortcut. The post-admission writers ``_remember_rendered_music`` and
# ``_remember_enqueued`` intentionally keep it synchronized with
# ``StationState.last_music_file``. Recovery and speech-bed selection must read only
# the state-scoped value so a replacement station cannot inherit another's audio.
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
    duration_sec = max(0.0, float(rendered.track.duration_ms or 0) / 1000)
    if duration_sec > 0:
        state.immediate_audio_index[path] = duration_sec


def _adjacency_type_for(segment: Segment) -> SegmentType | None:
    """The tail-adjacency classification of a queued segment — the SINGLE rule shared by the
    enqueue funnel, the air-next tail recompute, and the producer-start seed.

    Returns ``None`` for a continuity BREAK — a failed render that aired as recovery audio,
    or the synthetic 440Hz emergency-tone fill — so a non-song MUSIC-shaped segment is never
    treated as an adjacent song a later speech bed could bleed (#641). Otherwise returns the
    segment's real type.
    """
    if "error" in segment.metadata:
        return None
    if segment.type == SegmentType.MUSIC and segment.metadata.get("audio_source") == "emergency_tone":
        return None
    return segment.type


def _seed_adjacency_type(
    queue: asyncio.Queue[Segment], state: StationState, inferred: SegmentType | None
) -> SegmentType | None:
    """Startup value for ``last_enqueued_type``, applying the same continuity-break rule as the
    funnel on BOTH inference paths: a queued tail (inspect the segment) and the
    now-streaming/current-track inference (inspect ``now_streaming.metadata``). A tone/errored
    now-playing is not an adjacent song, so it must not seed MUSIC and let a stale
    ``last_music_file`` bleed under the first speech segment after a restart (#641).
    """
    queued = list(getattr(queue, "_queue", ()))
    if queued:
        return _adjacency_type_for(queued[-1])
    now_meta = state.now_streaming.get("metadata") or {}
    if "error" in now_meta or now_meta.get("audio_source") == "emergency_tone":
        return None
    return inferred


def _remember_enqueued(state: StationState, segment: Segment, source_path: Path) -> None:
    """Record the program-order tail predecessor for speech-bed adjacency.

    Two pieces of state are maintained here at the single enqueue chokepoint:

    * ``last_enqueued_type`` — the type of the segment now at the queue tail, the basis
      for speech-bed eligibility (``_adjacent_music_source``).
    * ``last_music_file`` — the CLEAN song used as a crossfade tail / talk bed — but ONLY
      for rescue & recycled fills. Normally-rendered music records ``last_music_file`` in
      the music ``success_callback`` (after a successful queue commit), via
      ``_remember_rendered_music``, using the clean render path — never the sting-merged /
      FM-baked aired path (either would bleed a processed render under a later announcer).
      The funnel must not overwrite that value at enqueue time with ``segment.path``.
      Rescue & recycled fills never run ``_remember_rendered_music`` and are queued before
      the sting stage, so the funnel is the only place that records their clean bed source —
      ``source_path`` is their pre-egress (clean) path. This is what closes #641.

    Front-insert is intentionally excluded by the caller: air-next changes head order, while
    speech-bed adjacency follows normal tail appends. A continuity-break fill (recovery audio
    or the emergency tone) resolves to ``None`` via ``_adjacency_type_for``, so the next speech
    never beds a song the break severed. ``prev_seg_type`` uses the same classifier at
    queue-time updates so transition stingers do not treat a rescue tone as real music either.
    """
    adj = _adjacency_type_for(segment)
    state.last_enqueued_type = adj
    if (
        adj == SegmentType.MUSIC
        and (segment.metadata.get("rescue") or segment.metadata.get("recycled"))
        and source_path.exists()
    ):
        state.last_music_file = source_path
        _set_last_music_file(source_path)
    if adj == SegmentType.MUSIC and source_path.exists() and segment.duration_sec > 0:
        state.immediate_audio_index[source_path] = float(segment.duration_sec)


def _release_campaign_should_force_first_banter(state: StationState) -> bool:
    campaign = getattr(state, "release_campaign", None)
    if campaign is None:
        return False
    ledger = getattr(campaign, "ledger", None)
    if ledger is None or getattr(ledger, "aired_count", 0) > 0:
        return False
    if state.ha_pending_directive:
        return False
    try:
        return bool(campaign.is_due())
    except Exception:
        logger.warning("Release campaign due check failed", exc_info=True)
        return False


def _release_beat_commit_from_banter(commit):
    return getattr(commit, "release_beat", None)


def _release_beat_metadata_from_commit(commit) -> dict:
    release_commit = _release_beat_commit_from_banter(commit)
    if release_commit is None:
        return {}
    try:
        return release_commit.segment_metadata()
    except Exception:
        logger.warning("Release beat metadata extraction failed", exc_info=True)
        return {}


def _memory_extraction_metadata_from_commit(commit, script_lines: list[dict]) -> dict:
    memory_commit = getattr(commit, "memory_extraction", None)
    if memory_commit is None:
        return {}
    try:
        final_commit = replace(
            memory_commit,
            script_lines=[dict(line) for line in script_lines if isinstance(line, dict)],
        )
        payload = final_commit.to_metadata()
        if not payload.get("script_lines"):
            return {}
        return {"memory_extraction": payload}
    except Exception:
        logger.warning("Memory extraction metadata extraction failed", exc_info=True)
        return {}


def _abandon_release_beat_commit(state: StationState, commit) -> None:
    release_commit = _release_beat_commit_from_banter(commit)
    if release_commit is None:
        return
    try:
        release_commit.abandon(state)
    except Exception:
        logger.warning("Release beat attempt restore failed", exc_info=True)


def _release_campaign_abandon_in_flight(state: StationState) -> None:
    """Commit-free safety net: un-strand a begun-but-never-queued release beat.

    Covers producer paths that raise after begin_attempt() but before enqueue
    where no commit object survives (e.g. a sibling task failing inside the
    transition+banter asyncio.gather). Only touches QUEUED_ATTEMPT.
    """
    campaign = getattr(state, "release_campaign", None)
    if campaign is None:
        return
    try:
        campaign.abandon_in_flight()
    except Exception:
        logger.warning("Release campaign in-flight abandon failed", exc_info=True)


def _schedule_restart_handoff_spool(state: StationState, config: StationConfig, segment: Segment) -> None:
    if segment.type is not SegmentType.MUSIC:
        return
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    if metadata.get("youtube_id"):
        duration_sec = (
            segment.duration_sec
            if segment.duration_sec and segment.duration_sec > 0
            else _duration_sec_from_metadata(metadata)
        )
        track = Track(
            title=str(metadata.get("title_only") or metadata.get("title") or "Music"),
            artist=str(metadata.get("artist") or ""),
            duration_ms=round(duration_sec * 1000) if duration_sec > 0 else 0,
            youtube_id=str(metadata.get("youtube_id") or ""),
            album_art=str(metadata.get("album_art") or ""),
            source="youtube",
        )
        verdict = classify_youtube_candidate(
            track,
            [candidate for candidate in state.playlist if candidate.cache_key != track.cache_key],
            config.pacing,
            actual_duration_sec=duration_sec if duration_sec > 0 else None,
        )
        if not verdict.accepted:
            logger.info(
                "Skipping restart handoff spool for held music segment: %s (%s)",
                track.display,
                verdict.reason,
            )
            return
    try:
        candidate = RestartHandoffCandidate.from_segment(segment)
    except Exception:
        logger.debug("Restart handoff candidate creation failed", exc_info=True)
        return
    tasks = getattr(state, "_restart_handoff_tasks", None)
    if tasks is None:
        tasks = set()
        state._restart_handoff_tasks = tasks
    # Snapshot the still-admitted handoff files on the loop and protect them from
    # the spool prune — the background write replaces the manifest with this one
    # candidate, and the prune would otherwise delete startup-admitted files that
    # are still sitting unplayed in the live queue (dead air on the cold open).
    protected = frozenset(getattr(state, "restart_handoff_admitted_paths", None) or ())
    task = asyncio.create_task(
        asyncio.to_thread(
            try_write_restart_handoff_spool,
            config.cache_dir,
            [candidate],
            blocklist=state.blocklist,
            protected_paths=protected,
        )
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)


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

    The candidate belongs to this station state. Falling back to the process-level
    cache would let a freshly constructed station recycle audio admitted by a
    previous state, including after a cutover or test/runtime reinitialization.
    """
    candidate = state.last_music_file
    if candidate and candidate.exists():
        return candidate
    return None


def _make_imaging_lib(config: StationConfig) -> ImagingLibrary:
    """Construct a station ImagingLibrary from config."""
    return ImagingLibrary(
        config.sonic_brand.motif_notes,
        config.tmp_dir,
        bed_volume_db=config.imaging.bed_volume_db,
        assets_dir=Path(config.imaging.assets_dir) if config.imaging.assets_dir else None,
        cache_dir=config.cache_dir,
    )


def _crosses_music_speech_boundary(prev_type: SegmentType, next_type: SegmentType) -> bool:
    return (prev_type in _MUSIC_TYPES and next_type in _SPEECH_TYPES) or (
        prev_type in _SPEECH_TYPES and next_type in _MUSIC_TYPES
    )


def _adjacent_music_source(state: StationState) -> Path | None:
    """Last-played song, but only when it was the segment that *just* aired.

    A speech segment may reuse real song audio (crossfade tail or talk bed) only
    when the immediately-previous queued segment is MUSIC. After an ad/news/ID/
    banter intervenes the song is stale, so reusing it bleeds a 3-minutes-ago
    track under a later announcer (illusion break). Returns None when no song is
    adjacent, in which case callers fall back to dry voice / synthetic bed.

    This is the single place the eligibility rule lives; tightening it to a
    song-identity/freshness check later is a one-function change.
    """
    if state.last_enqueued_type not in _MUSIC_TYPES:
        return None
    return _get_last_music_file(state)


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
        return _adjacency_type_for(queued[-1])
    now_type = _segment_type_from_value(state.now_streaming.get("type"))
    if now_type is not None:
        now_meta = state.now_streaming.get("metadata") or {}
        if "error" in now_meta or (now_type == SegmentType.MUSIC and now_meta.get("audio_source") == "emergency_tone"):
            return None
        return now_type
    if state.current_track is not None:
        return SegmentType.MUSIC
    return None


def _queue_shadow_entry(segment: Segment, *, reason: str | None = None) -> dict:
    """Create the admin-visible record for audio admitted to playback.

    Scaletta is an honest projection of the real queue, not a scheduler preview.
    Stamp the identity before egress so the eventual queue row can always remove
    the matching segment, including startup prewarms and continuity bridges.
    ``reason`` overrides the default for callers outside the normal egress funnel
    (e.g. restart-handoff admission), so every shadow row shares one dict shape.
    """
    queue_id = str(segment.metadata.get("queue_id") or uuid4().hex)
    segment.metadata["queue_id"] = queue_id
    return {
        "id": queue_id,
        "type": segment.type.value,
        "label": segment.metadata.get("title", segment.type.value),
        "spotify_id": segment.metadata.get("spotify_id", ""),
        "reason": reason or segment.metadata.get("queue_reason", "Rendered and queued for playback."),
        "playlist_index": segment.metadata.get("playlist_index", -1),
        "source_kind": segment.metadata.get("source_kind", ""),
        "duration_sec": round(segment.duration_sec or 0, 1),
    }


def _front_insert_queue_and_shadow(
    queue: asyncio.Queue[Segment], state: StationState, segment: Segment, shadow_entry: dict
) -> bool:
    """Air an operator-triggered segment NEXT instead of behind the buffered
    lookahead. Synchronously drains the queue, puts the segment at the front, and
    repushes — no await between draining the real queue and updating the shadow, so
    the streamer cannot interleave (mirrors ``_purge_queue_and_shadow`` and the
    ``/api/queue/remove`` critical section). Drops the furthest-future tail if the
    bounded queue would otherwise overflow ``maxsize`` (which would raise QueueFull
    and risk dead air). Also drops the queue head outright when it carries a
    ``transition_track_ref`` (a "just finished playing X" claim baked into its
    audio) — front-inserting anything breaks that adjacency claim unconditionally.
    Dropped renders are re-produced on a later cycle. Returns False (dropping the
    segment) if the session was stopped mid-build.
    """
    if state.session_stopped:
        state.record_discard(segment, reason=GenerationWasteReason.SESSION_STOPPED)
        if segment.ephemeral and not _is_packaged_asset(segment.path):
            segment.path.unlink(missing_ok=True)
        # The forced render is abandoned — release the one-at-a-time guard so the
        # operator can retry after resume instead of being locked out until restart.
        state.operator_force_pending = None
        logger.info("Discarding forced %s because the session is stopped", segment.type.value)
        return False
    items: list[Segment] = []
    while not queue.empty():
        try:
            items.append(queue.get_nowait())
            queue.task_done()
        except asyncio.QueueEmpty:
            break
    rows_by_segment = {
        id(item): state.queued_segments[index] for index, item in enumerate(items) if index < len(state.queued_segments)
    }
    # A second air-next render should normally be impossible because the operator
    # one-at-a-time guard stays armed until admission. Keep the queue safe even if
    # a race or an internal caller violates that assumption: when every occupied
    # slot is already air-next, reject the newcomer instead of deleting an earlier
    # operator promise merely to make room for the newer one.
    if queue.maxsize and len(items) >= queue.maxsize and items and all(item.metadata.get("air_next") for item in items):
        for item in items:
            queue.put_nowait(item)
        state.record_discard(segment, reason=GenerationWasteReason.AIR_NEXT_OVERFLOW)
        if segment.ephemeral and not _is_packaged_asset(segment.path):
            segment.path.unlink(missing_ok=True)
        state.operator_force_pending = None
        logger.info("Air-next: rejected %s because every queue slot is already air-next", segment.type.value)
        return False
    # A queue-head speech segment that carries a "just finished playing X" claim
    # (baked into its audio, crossfaded over X's fade) has that claim unconditionally
    # broken the moment anything gets wedged ahead of it — X is no longer what plays
    # right before it. Drop it here rather than airing a now-false claim; a fresh,
    # accurate one is produced on the next normal cycle (see #641 for the sibling
    # audio-level version of this problem).
    stale_head: Segment | None = None
    if items and items[0].metadata.get("transition_track_ref"):
        stale_head = items.pop(0)
    items.insert(0, segment)
    dropped: list[Segment] = []
    while queue.maxsize and len(items) > queue.maxsize:
        # A continuity reservation is the listener-safety tail. A ready
        # operator pick remains air-next, but it may not silently evict the
        # only recovery runway merely because the count-bound queue is full.
        evict_index = next(
            (
                index
                for index in range(len(items) - 1, 0, -1)
                if not items[index].metadata.get("continuity_reservation") and not items[index].metadata.get("air_next")
            ),
            None,
        )
        if evict_index is not None:
            dropped.append(items.pop(evict_index))
            continue
        # There is no ordinary tail to evict. Preserve one protected clip in
        # the capacity-exempt slot; playback serves it only after real queue
        # audio, so it cannot displace the operator's ready air-next segment.
        protected_index = next(
            (index for index in range(len(items) - 1, 0, -1) if items[index].metadata.get("continuity_reservation")),
            None,
        )
        if protected_index is None:
            # Only already-admitted air-next entries remain. Never evict one to
            # make a newer request fit; reject the new head and preserve the
            # established queue order. The all-air-next fast path above handles
            # the normal shape, while this branch keeps the invariant defensive
            # if a future queue layout reaches it.
            items.pop(0)
            state.record_discard(segment, reason=GenerationWasteReason.AIR_NEXT_OVERFLOW)
            if segment.ephemeral and not _is_packaged_asset(segment.path):
                segment.path.unlink(missing_ok=True)
            state.operator_force_pending = None
            for item in items:
                queue.put_nowait(item)
            state.queued_segments = [rows_by_segment.get(id(item)) or _queue_shadow_entry(item) for item in items]
            logger.info("Air-next: rejected %s rather than evict an earlier air-next", segment.type.value)
            return False
        else:
            state.continuity_slot = items.pop(protected_index)
    for item in items:
        queue.put_nowait(item)
    # Rebuild the operator projection from the final real queue. This is a little
    # more deliberate than tail slicing because protected entries may survive an
    # air-next insertion while a different ordinary tail is dropped.
    if shadow_entry.get("id"):
        segment.metadata["queue_id"] = str(shadow_entry["id"])
    segment.metadata["air_next"] = True
    prior_rows = {str(row.get("id")): row for row in state.queued_segments if row.get("id")}
    prior_rows[str(shadow_entry.get("id"))] = shadow_entry
    if stale_head is not None:
        state.record_discard(
            stale_head, reason=GenerationWasteReason.STALE_PLAYED_TRACK_REF, already_counted_in_produced=True
        )
        _drop_segment_moment_receipts(
            state, stale_head, GenerationWasteReason.STALE_PLAYED_TRACK_REF, "air-next-stale-transition"
        )
        if getattr(stale_head, "ephemeral", False) and not _is_packaged_asset(stale_head.path):
            stale_head.path.unlink(missing_ok=True)
    for seg in dropped:
        state.record_discard(seg, reason=GenerationWasteReason.AIR_NEXT_OVERFLOW, already_counted_in_produced=True)
        _drop_segment_moment_receipts(state, seg, GenerationWasteReason.AIR_NEXT_OVERFLOW, "air-next-overflow")
        if getattr(seg, "ephemeral", False) and not _is_packaged_asset(seg.path):
            seg.path.unlink(missing_ok=True)
    state.queued_segments = [
        shadow_entry
        if item is segment
        else rows_by_segment.get(id(item))
        or prior_rows.get(str(item.metadata.get("queue_id")))
        or _queue_shadow_entry(item)
        for item in items
    ]
    # Recompute the tail-adjacency basis from the ACTUAL new queue tail. Air-next puts the
    # segment at the HEAD, but it only leaves tail adjacency unchanged when buffered music
    # still sits behind it. When the queue was empty (the inserted speech segment becomes the
    # real tail) or a full-queue overflow dropped the buffered music tail, leaving a stale
    # last_enqueued_type=MUSIC would bed a song that no longer airs adjacent to the next
    # generated segment (#641). A cache-backed dropped/removed song stays on disk, so the
    # existence check alone would not catch it.
    new_tail = items[-1] if items else None
    if new_tail is None:
        state.last_enqueued_type = None
    elif dropped and _adjacency_type_for(new_tail) == SegmentType.MUSIC:
        # An overflow drop removed buffered music; the remaining music tail's bed source is
        # uncertain (the dropped song may have been its basis) → no adjacent song.
        state.last_enqueued_type = None
    else:
        # Unchanged buffered tail (no drop), or a non-music remaining tail. Use the shared
        # continuity-break rule so a tone/errored MUSIC tail is not mistaken for a song.
        state.last_enqueued_type = _adjacency_type_for(new_tail)
    # The operator's pick is now queued — the trigger is fulfilled. Clearing the
    # in-flight guard HERE (not at render-start) is what makes "one at a time" hold
    # through a slow render: a second tap stays rejected until this pick airs, so it
    # can never be front-inserted ahead of it.
    state.operator_force_pending = None
    logger.info(
        "Air-next: front-inserted %s%s%s",
        segment.type.value,
        f" (dropped {len(dropped)} buffered tail segment(s))" if dropped else "",
        " (dropped stale transition-claim head)" if stale_head is not None else "",
    )
    return True


# Metadata flag, stamped at construction by every emergency / bridge / rescue fill,
# marking a segment that must SKIP the egress FX pass. These fills exist to kill dead
# air the instant the queue runs dry (leadership principle #2, INSTANT AUDIO); a clean
# transmitter sound is never worth a beat of silence, so a rescue is never delayed by
# an extra ffmpeg encode.
#
# This is a single explicit flag set where each rescue is BUILT — deliberately NOT an
# allowlist of other metadata keys. The old allowlist keyed off ``canned``, but that
# key is overloaded: normal-rotation shareware / Demo-mode banter is also a canned
# clip (``canned=True``) yet is NOT a rescue and MUST still be coloured — otherwise
# the first host break a new user hears airs studio-clean next to FM-coloured music,
# the exact seam this stage removes. Rescue-ness is a property of WHY a segment was
# made, which only the construction site knows; inferring it from key presence rots (a
# new bridge marker silently misses the skip) and mis-fires (rotation-canned banter).
_RESCUE_FLAG = "rescue"


def _is_rescue_fill(segment: Segment) -> bool:
    """True when the segment is an emergency / bridge / rescue fill that must skip the
    egress pass. Driven by the explicit ``rescue`` marker the construction site
    stamps — never by sniffing overloaded keys like ``canned``."""
    return bool(segment.metadata.get(_RESCUE_FLAG))


async def _bake_cached_egress(segment: Segment, source: Path, config: StationConfig) -> Segment:
    """Colour a cache-file source once and reuse the baked render on later plays.

    A norm-cache music hit is a stable file that can air many times; the FM pass is a
    full re-encode, expensive on the Pi, and re-running it every replay is wasted work.
    Bake the coloured render into the cache keyed by source identity + chain version, so a
    replay reuses it with no encode, a chain change re-bakes (new key), and stale bakes
    fall out by normal LRU eviction. The baked file is published atomically (encode to a
    staging name, then ``os.replace``) so a reader never sees a half-written file.
    Best-effort: any failure airs the source un-coloured this play and retries next time.

    The key includes the source's mtime+size, not just its name: a norm file can be
    rewritten in place at the same path — ``reconcile_cached_music()`` re-levels it after a
    LUFS-target change, or eviction drops it and it is regenerated — and keying on the name
    alone would serve the stale bake (old/quieter colour) instead of re-baking the updated
    source. A content change moves the key, so the stale bake is orphaned and LRU-evicted.
    """
    chain_ver = broadcast_chain_version()
    if chain_ver is None:  # chain disabled — nothing to colour
        return segment
    try:
        st = source.stat()
    except OSError:
        return segment  # source vanished (e.g. evicted mid-flight) — leave it to the caller
    src_tag = f"{st.st_mtime_ns}_{st.st_size}"  # busts the bake when the source is rewritten
    baked = config.cache_dir / f"fm_{source.stem}_{chain_ver}_{src_tag}.mp3"
    if baked.exists() and baked.stat().st_size > 0:  # cache hit — no re-encode
        return replace(segment, path=baked, ephemeral=False)
    staging = config.cache_dir / f"fm_{source.stem}_{chain_ver}_{src_tag}.staging_{uuid4().hex[:8]}.mp3"
    loop = asyncio.get_running_loop()
    try:
        applied = await loop.run_in_executor(None, apply_broadcast_chain, source, staging)
    except Exception:
        logger.debug("Egress bake failed (non-fatal), airing source clean", exc_info=True)
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)
        return segment
    except BaseException:
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)
        raise
    if not applied:
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)
        return segment
    try:
        os.replace(staging, baked)  # atomic publish within cache_dir
    except OSError:
        logger.debug("Egress bake publish failed (non-fatal), airing source clean", exc_info=True)
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)
        return segment
    return replace(segment, path=baked, ephemeral=False)


async def _apply_egress(segment: Segment, config: StationConfig) -> Segment:
    """Run the outgoing egress FX pipeline on a finished segment.

    Returns the processed segment (a baked cache render or a fresh ephemeral tmp render)
    or the original, unchanged, when no stage applied. The FM broadcast chain is the
    optional final stage — "the transmitter" (default off — studio-clean; opt-in); the
    chaos (#482) and interference (#483)
    content stages will slot in BEFORE it so effects colour the content and the broadcast
    chain colours the channel last. Emergency / bridge / rescue fills skip the pipeline
    entirely (see ``_is_rescue_fill``). Best-effort: a stage failure leaves the prior
    audio in place and never raises, so the listener never hits dead air.

    A cache-file source (a norm-cache music hit) is colour-baked once and reused (see
    ``_bake_cached_egress``); an ephemeral one-shot render (fresh voice/music) is
    coloured to a per-play tmp.
    """
    if _is_rescue_fill(segment):
        return segment
    source = segment.path
    if not segment.ephemeral and _is_under(source, config.cache_dir):
        return await _bake_cached_egress(segment, source, config)
    out = config.tmp_dir / f"egress_{uuid4().hex[:8]}.mp3"
    loop = asyncio.get_running_loop()
    try:
        applied = await loop.run_in_executor(None, apply_broadcast_chain, source, out)
    except Exception:
        logger.debug("Egress broadcast chain failed (non-fatal), airing clean", exc_info=True)
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)
        return segment
    except BaseException:
        # Cancellation/shutdown landed mid-encode: don't leak the half-written egress
        # tmp, then let the cancellation propagate (the pre-egress render is untouched).
        # Suppress an unlink error so it can never mask the CancelledError we re-raise.
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)
        raise
    if not applied:
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)
        return segment
    pre_segment = segment
    segment = replace(segment, path=out, ephemeral=True)
    # Drop the pre-egress tmp render; a cache file (norm-cache music, ephemeral=False)
    # is left in place so the cache is never corrupted by the colouring pass.
    _unlink_if_tmp_render(pre_segment, config.tmp_dir)
    return segment


StaleCheck = Callable[[], bool | str | None]


def _stale_check_reason(stale_check: StaleCheck | None) -> str | None:
    """Return the caller's concrete stale reason, with boolean compatibility."""
    if stale_check is None:
        return None
    verdict = stale_check()
    if not verdict:
        return None
    if isinstance(verdict, str):
        return verdict
    return GenerationWasteReason.EGRESS_STALE


def _enqueue_rejection_reason(
    state: StationState,
    segment: Segment,
    stale_check: StaleCheck | None,
) -> str | None:
    """Classify the current enqueue rejection without storing mutable side state."""
    if state.session_stopped:
        return GenerationWasteReason.SESSION_STOPPED
    if segment.type == SegmentType.MUSIC and state.blocklist:
        metadata = segment.metadata or {}
        key = (
            str(metadata.get("artist", "")).strip().lower(),
            str(metadata.get("title_only") or metadata.get("title") or "").strip().lower(),
        )
        if key in state.blocklist:
            return GenerationWasteReason.BLOCKLIST_GATE
    return _stale_check_reason(stale_check)


def _discard_rejected_admission(state: StationState, segment: Segment, reason: str, *, phase: str) -> None:
    """Record and clean one segment rejected by an enqueue gate."""
    logger.info("Discarding %s: %s (%s)", segment.type.value, reason, phase)
    state.record_discard(segment, reason=reason)
    if segment.ephemeral and not _is_packaged_asset(segment.path):
        segment.path.unlink(missing_ok=True)


def _remove_exact_queued_segment(queue: asyncio.Queue[Segment], target: Segment) -> bool:
    """Synchronously remove one identity-matching item while preserving queue order.

    ``asyncio.Queue.put`` may block for capacity. Once it resumes, a live action
    can already have invalidated the render. Draining, filtering, and rebuilding
    here has no await point, so playback cannot observe the stale admission and
    the unfinished-task counter remains balanced.
    """
    items: list[Segment] = []
    removed = False
    while not queue.empty():
        try:
            item = queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            break
        if item is target and not removed:
            removed = True
        else:
            items.append(item)
    for item in items:
        queue.put_nowait(item)
    return removed


async def _enqueue_with_egress(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
    segment: Segment,
    *,
    front_insert: bool = False,
    shadow_entry: dict | None = None,
    stale_check: StaleCheck | None = None,
) -> bool:
    """The single funnel every segment passes through on its way to the playback queue.

    The outgoing egress FX pipeline (the optional FM broadcast chain, default off; the
    chaos and interference stages slot in later) runs here, so music, dialogue, ads and
    bridges all leave through one chokepoint — the audio equivalent of the
    transmitter every signal passes through last. Operator air-next still routes
    through the synchronous front-insert critical section, just behind this one
    entry point. FX run BEFORE the front-insert critical section so it stays a
    no-await drain→prepend→repush.
    """
    # Final pre-egress gate: stop, blocklist, and captured cutover state are all
    # reclassified by the same pure helper used after each subsequent await.
    rejection_reason = _enqueue_rejection_reason(state, segment, stale_check)
    if rejection_reason is not None:
        _discard_rejected_admission(state, segment, rejection_reason, phase="pre-egress enqueue gate")
        return False

    # Validate the front-insert contract BEFORE any egress work so a programming error
    # never leaves a coloured egress tmp render orphaned on disk.
    if front_insert and shadow_entry is None:  # operator air-next must always supply a shadow entry
        raise ValueError("front_insert enqueue requires a shadow_entry")
    if not front_insert and shadow_entry is None:
        shadow_entry = _queue_shadow_entry(segment)
    pre_egress_path = segment.path  # clean source for speech-bed reuse (see _remember_enqueued)
    egress_started = time.monotonic()
    segment = await _apply_egress(segment, config)
    state.add_render_stage_timing("egress", (time.monotonic() - egress_started) * 1000)
    # Post-egress staleness re-check (opt-in). The egress encode can be slow (the FM
    # broadcast chain is a full extra FFmpeg pass), and a source switch landing DURING it
    # would purge the queue before this put. A caller that captured a generation up front
    # (prewarm) passes stale_check so a now-stale segment is dropped at the last moment
    # instead of put into the freshly-purged queue (#665). Main-loop callers omit it and
    # keep their documented pre-egress-only behavior.
    rejection_reason = _enqueue_rejection_reason(state, segment, stale_check)
    if rejection_reason is not None:
        _discard_rejected_admission(state, segment, rejection_reason, phase="post-egress enqueue gate")
        return False
    admission_started = time.monotonic()
    if front_insert:
        assert shadow_entry is not None  # narrowed by the guard above (mypy)
        admitted = _front_insert_queue_and_shadow(queue, state, segment, shadow_entry)
        state.add_render_stage_timing("admission", (time.monotonic() - admission_started) * 1000)
        return admitted
    await queue.put(segment)
    # ``queue.put`` is an await when capacity is full. Revalidate after that wait,
    # then synchronously retract the exact admitted object before publishing its
    # shadow row or any queue-commit side effect. This closes the cutover race in
    # which a purge/source/chaos action lands after the post-egress check.
    rejection_reason = _enqueue_rejection_reason(state, segment, stale_check)
    if rejection_reason is not None:
        removed = _remove_exact_queued_segment(queue, segment)
        if removed:
            _discard_rejected_admission(state, segment, rejection_reason, phase="post-capacity enqueue gate")
        else:  # Defensive: impossible without a new await/consumer interleaving above.
            logger.error("Stale %s escaped atomic queue retraction", segment.type.value)
        state.add_render_stage_timing("admission", (time.monotonic() - admission_started) * 1000)
        return False
    assert shadow_entry is not None
    state.queued_segments.append(shadow_entry)
    _remember_enqueued(state, segment, pre_egress_path)
    _schedule_restart_handoff_spool(state, config, segment)
    state.add_render_stage_timing("admission", (time.monotonic() - admission_started) * 1000)
    return True


async def _apply_talk_bed(
    audio_path: Path,
    config: StationConfig,
    state: StationState,
    *,
    prefix: str,
    source_track: Path | None = None,
) -> Path:
    """Mix a quiet music bed under a generated spoken segment.

    ``source_track`` is the only place real song audio can enter the bed; callers
    pass it via :func:`_adjacent_music_source` so a stale (non-adjacent) song is
    never reused. When None, ``pick_talk_bed`` falls back to a bundled bed or a
    synthetic drone — never silence, never a stale track.
    """
    loop = asyncio.get_running_loop()
    last_track = source_track if config.imaging.use_music_queue_for_beds else None
    bed_path = config.tmp_dir / f"{prefix}_bed_{uuid4().hex[:8]}.mp3"
    with _timed_render_stage(state, "mix"):
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
    music_path: Path | None,
    tail_seconds: float = 8.0,
    music_fade_volume: float = 0.5,
) -> Path:
    """Crossfade voice over an explicit music tail. Returns voice_path (dry) when
    no eligible music is given or the crossfade fails.

    ``music_path`` is supplied by the caller via :func:`_adjacent_music_source`
    so a stale (non-adjacent) song never bleeds under a later announcer. This
    function no longer reaches for the last-rendered file itself.
    """
    last_music = music_path
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
    music_path: Path | None = None,
) -> Path:
    """Synthesize an impossible-moment line via TTS with crossfade. Raises on failure.

    ``music_path`` (from :func:`_adjacent_music_source`) is the only song the
    crossfade may use; None gives a clean dry line.
    """
    host = random.choice(_sw._regular_hosts(config))
    imp_path = config.tmp_dir / f"impossible_{uuid4().hex[:8]}.mp3"
    with _timed_render_stage(state, "tts"):
        await synthesize(
            line,
            host.voice,
            imp_path,
            engine=host.engine,
            edge_fallback_voice=host.edge_fallback_voice,
            state=state,
        )
    xfade_out = config.tmp_dir / f"impossible_xf_{uuid4().hex[:8]}.mp3"
    with _timed_render_stage(state, "mix"):
        audio_path = await _try_crossfade(imp_path, config, xfade_out, music_path)
    state.last_banter_script = [{"host": host.name, "text": line, "type": "impossible"}]
    return audio_path


_recently_played_clips: deque[str] = deque(maxlen=50)

# Cache directory listings for demo asset clips (avoid repeated glob on every call).
_canned_clip_cache: dict[str, list[Path]] = {}

SHAREWARE_CANNED_LIMIT = 3


def _clip_is_serviceable(path: Path) -> bool:
    """Cheap liveness check for a cached clip Path — no ffprobe on rescue paths."""
    try:
        return path.is_file() and path.stat().st_size > 1024
    except OSError:
        return False


def _producer_buffered_seconds(queue: asyncio.Queue[Segment]) -> float:
    """Return ready-audio seconds from the real producer queue."""
    internal = getattr(queue, "_queue", None)
    if internal is None:
        return 0.0
    return buffered_audio_seconds(seg.duration_sec for seg in list(internal))


def _max_observable_runway_slots(queue: asyncio.Queue[Segment], lookahead_segments: int) -> int:
    """Slots the producer can see filled immediately before a natural decision."""
    maxsize = int(getattr(queue, "maxsize", 0) or 0)
    if maxsize > 0:
        return max(0, maxsize - 1)
    return max(0, int(lookahead_segments) - 1)


def _runway_fill_needed(queue: asyncio.Queue[Segment]) -> bool:
    """Whether the producer should keep filling past lookahead to build seconds runway."""
    if RUNWAY_FLOOR_SECONDS <= 0 or _producer_buffered_seconds(queue) >= RUNWAY_FLOOR_SECONDS:
        return False
    maxsize = int(getattr(queue, "maxsize", 0) or 0)
    return maxsize > 0 and queue.qsize() < maxsize


def _should_defer_for_runway(queue: asyncio.Queue[Segment], lookahead_segments: int) -> tuple[bool, float]:
    """Return whether optional speech should yield to music, plus observed seconds."""
    buffered = _producer_buffered_seconds(queue)
    if RUNWAY_FLOOR_SECONDS <= 0 or buffered >= RUNWAY_FLOOR_SECONDS:
        return False, buffered

    # The producer only makes a natural pacing decision when it is below the
    # bounded queue's hard cap. If the observed queue is already as full as it
    # can get before this decision, the fixed seconds floor is unreachable with
    # the current content mix; let the due speech air instead of starving it.
    observable_slots = _max_observable_runway_slots(queue, lookahead_segments)
    if observable_slots == 0 or queue.qsize() >= observable_slots:
        return False, buffered
    return True, buffered


def _pick_canned_clip(subdir: str, *, state: StationState | None = None) -> Path | None:
    """Pick a pre-bundled clip from assets/demo/{subdir}/, avoiding recent repeats.

    For banter clips, respects the shareware trial limit: after SHAREWARE_CANNED_LIMIT
    clips have been streamed to the listener, returns None to force TTS fallback.
    Recovery and welcome clips are not subject to the limit.
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
    eligible = [c for c in eligible if _clip_is_serviceable(c)]
    if not eligible:
        _recently_played_clips.clear()
        eligible = [c for c in clips if _clip_is_serviceable(c)]
    if not eligible:
        return None
    pick = random.choice(eligible)
    _recently_played_clips.append(pick.name)
    return pick


def _resolve_sweeper_voice(config: StationConfig) -> tuple[str, str, str]:
    """Return voice, engine, and Edge fallback for sonic-brand sweepers."""
    sb = config.sonic_brand
    sweeper_voice = sb.sweeper_voice
    sweeper_engine = sb.sweeper_engine
    sweeper_fallback = sb.sweeper_edge_fallback_voice
    if not sweeper_voice:
        sweeper_host = random.choice(_sw._regular_hosts(config))
        sweeper_voice = sweeper_host.voice
        sweeper_engine = sweeper_host.engine
        sweeper_fallback = sweeper_host.edge_fallback_voice
    return sweeper_voice, sweeper_engine, sweeper_fallback


async def _render_sweeper_audio(
    text: str,
    config: StationConfig,
    state: StationState,
    *,
    prefix: str,
    validate_dry: bool = False,
) -> Path:
    """Render a short station-imaging sweeper with the configured voice and sting."""
    sweeper_voice, sweeper_engine, sweeper_fallback = _resolve_sweeper_voice(config)
    audio_path = config.tmp_dir / f"{prefix}_{uuid4().hex[:8]}.mp3"
    with _timed_render_stage(state, "tts"):
        await synthesize(
            text,
            sweeper_voice,
            audio_path,
            engine=sweeper_engine,
            edge_fallback_voice=sweeper_fallback,
            state=state,
        )
    if validate_dry:
        try:
            with _timed_render_stage(state, "quality"):
                await asyncio.to_thread(validate_segment_audio, audio_path, SegmentType.SWEEPER)
        except (AudioQualityError, AudioToolError):
            audio_path.unlink(missing_ok=True)
            raise
    loop = asyncio.get_running_loop()
    sting_path = config.tmp_dir / f"{prefix}_sting_{uuid4().hex[:8]}.mp3"
    mixed_path = config.tmp_dir / f"{prefix}_mixed_{uuid4().hex[:8]}.mp3"
    dry_sweeper_path = audio_path
    try:
        with _timed_render_stage(state, "mix"):
            imaging_lib = _make_imaging_lib(config)
            await loop.run_in_executor(None, imaging_lib.pick_sweeper_sting, sting_path)
            await loop.run_in_executor(None, mix_voice_with_sting, audio_path, sting_path, mixed_path)
    except Exception:
        mixed_path.unlink(missing_ok=True)
        dry_sweeper_path.unlink(missing_ok=True)
        raise
    finally:
        sting_path.unlink(missing_ok=True)
    dry_sweeper_path.unlink(missing_ok=True)
    return mixed_path


async def _build_recovery_sweeper_segment(config: StationConfig, state: StationState) -> Segment:
    """Build a branded rescue sweeper before falling through to emergency tone."""
    station_name = config.display_station_name or config.station.name
    sweeper_text = random.choice(RECOVERY_SWEEPER_LINES).format(station=station_name)
    audio_path = await _render_sweeper_audio(
        sweeper_text,
        config,
        state,
        prefix="recovery_sweeper",
        validate_dry=True,
    )
    try:
        await asyncio.to_thread(validate_segment_audio, audio_path, SegmentType.SWEEPER)
    except (AudioQualityError, AudioToolError):
        audio_path.unlink(missing_ok=True)
        raise
    return Segment(
        type=SegmentType.SWEEPER,
        path=audio_path,
        metadata={
            "type": "sweeper",
            "text": sweeper_text,
            "title": "Recovery sweeper",
            "error_recovery": True,
            "rescue": True,
        },
        ephemeral=True,
    )


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
            background=True,
            playlist=state.playlist,
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
    # Gate on source_revision (a true source switch via switch_playlist), NOT the broad
    # playlist_revision: a benign in-place edit (shuffle/add/move/enrich) bumps
    # playlist_revision but leaves the prewarmed song on the current source, so it must
    # keep the instant-audio pre-roll rather than throw it away.
    generation_source_revision = state.source_revision
    generation_chaos_epoch = state.chaos_cutover_epoch
    generation_continuity_epoch = state.continuity_epoch

    def _prewarm_stale_reason() -> str | None:
        if state.session_stopped:
            return GenerationWasteReason.SESSION_STOPPED
        if state.source_revision != generation_source_revision:
            return GenerationWasteReason.STALE_SOURCE
        if state.chaos_cutover_epoch != generation_chaos_epoch:
            return GenerationWasteReason.STALE_CHAOS
        if state.continuity_epoch != generation_continuity_epoch:
            return GenerationWasteReason.STALE_CONTINUITY
        return None

    try:
        track = _select_accepted_music_track(state, config)
        if track is None:
            return False
        logger.info("Pre-warming first track: %s", track.display)
        rendered = await _render_music_track(
            track,
            config,
            temp_prefix="music",
            context="prewarm",
            playlist=state.playlist,
        )
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
                _record_generated_waste(
                    state,
                    SegmentType.MUSIC,
                    norm_path,
                    GenerationWasteReason.QUALITY_GATE_REJECT,
                    duration_sec=(track.duration_ms or 0) / 1000.0,
                    ephemeral=not rendered.cache_hit,
                )
                # A quality rejection means this normalization is not safe
                # recovery media either. Remove the cache copy as well as the
                # transient render so a later rescue path cannot select it.
                rendered.cache_path.unlink(missing_ok=True)
                if not rendered.cache_hit:
                    norm_path.unlink(missing_ok=True)
                return False
        if generation_source_revision != state.source_revision:
            logger.info("Discarding stale prewarm segment after source switch")
            prewarm_segment = Segment(
                type=SegmentType.MUSIC,
                path=norm_path,
                duration_sec=(track.duration_ms or 0) / 1000.0,
                ephemeral=not rendered.cache_hit,
            )
            state.record_discard(prewarm_segment, reason=GenerationWasteReason.STALE_SOURCE)
            if not rendered.cache_hit:
                norm_path.unlink(missing_ok=True)
            return False
        if generation_chaos_epoch != state.chaos_cutover_epoch:
            logger.info("Discarding stale prewarm segment after chaos cutover")
            prewarm_segment = Segment(
                type=SegmentType.MUSIC,
                path=norm_path,
                duration_sec=(track.duration_ms or 0) / 1000.0,
                ephemeral=not rendered.cache_hit,
            )
            state.record_discard(prewarm_segment, reason=GenerationWasteReason.STALE_CHAOS)
            if not rendered.cache_hit:
                norm_path.unlink(missing_ok=True)
            return False
        if generation_continuity_epoch != state.continuity_epoch:
            logger.info("Discarding stale prewarm segment after a live continuity reservation")
            prewarm_segment = Segment(
                type=SegmentType.MUSIC,
                path=norm_path,
                duration_sec=(track.duration_ms or 0) / 1000.0,
                ephemeral=not rendered.cache_hit,
            )
            state.record_discard(prewarm_segment, reason=GenerationWasteReason.STALE_CONTINUITY)
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
                "heading_id": track.heading_id,
            },
            ephemeral=not rendered.cache_hit,
        )
        segment.duration_sec = await loop.run_in_executor(None, _probe_segment_duration, norm_path)
        # Post-egress stale check: the egress encode (FM broadcast chain) runs inside the
        # funnel before queue.put, and a source switch, chaos cutover, or continuity
        # reservation landing during it
        # would purge the queue first — then this put would land a stale pre-roll. Re-check
        # at the last moment so a switch during egress discards the pre-roll instead (#665).
        if not await _enqueue_with_egress(
            queue,
            state,
            config,
            segment,
            stale_check=_prewarm_stale_reason,
        ):
            return False
        _arm_accepted_heading_announcement(state, track)
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
    directive_source: str = "ha",
) -> bool:
    """Immediately interrupt the stream with bridge audio + pissed banter.

    Uses alert.mp3 or a packaged emergency tone as a bridge clip, drains
    the lookahead queue so no buffered music plays between bridge and banter,
    injects the directive, and fires skip_event to cut the current segment.

    Returns True if the interrupt fired, False if suppressed by the global
    cooldown gate. Per-entity cooldowns are enforced upstream by
    check_reactive_triggers.
    """
    # Retained as a call-site-compatible diagnostic hook; bridge generation no
    # longer writes a temporary file because the fallback is bundled.
    _ = bridge_tmp_dir
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
    if (
        state.interrupt_slot_ephemeral
        and state.interrupt_slot is not None
        and not _is_packaged_asset(state.interrupt_slot)
    ):
        state.interrupt_slot.unlink(missing_ok=True)
    state.interrupt_slot = None
    state.interrupt_slot_ephemeral = False
    # A hard interrupt supersedes every prior continuity reservation. Clear the
    # out-of-band slot before committing the interrupt bridge so playback cannot
    # serve stale control audio between that bridge and the urgent banter.
    state.continuity_slot = None

    # Commit an immediate bridge before touching the ready queue. This contains
    # no await and therefore cannot expose a drained queue to playback. The
    # packaged tone is intentionally used rather than waiting for FFmpeg tone
    # generation on a loaded Home Assistant Green.
    alert_sfx = _SFX_DIR / "alert.mp3"
    emergency_tone = _DEMO_ASSETS_DIR / "recovery" / "emergency_tone.mp3"
    if alert_sfx.exists():
        state.interrupt_slot = alert_sfx
    elif emergency_tone.is_file():
        state.interrupt_slot = emergency_tone
    else:
        # No bridge audio at all: hard-cutting here would drain the queue and
        # fire skip_event with nothing to air, opening dead air until banter
        # renders. Preserve whatever is already queued and abort the interrupt
        # instead of breaking the illusion (INSTANT AUDIO).
        logger.error("Interrupt bridge assets are unavailable; aborting interrupt to preserve current audio")
        return False

    # Drain the lookahead queue so no buffered music leaks between bridge and banter.
    purged = 0
    while not queue.empty():
        try:
            seg = queue.get_nowait()
            state.record_discard(seg, reason=GenerationWasteReason.INTERRUPT, already_counted_in_produced=True)
            if seg.ephemeral and not _is_packaged_asset(seg.path):
                seg.path.unlink(missing_ok=True)
            queue.task_done()
            purged += 1
        except Exception:
            break
    if purged:
        logger.info("Interrupt: purged %d buffered segments", purged)
    state.queued_segments.clear()
    state.continuity_epoch += 1
    # An urgent interrupt is a hard continuity break: the buffered tail is gone and the
    # current segment is cut below. Clear music adjacency so the urgent banter doesn't bed
    # a purged/cut song (the same stale-bleed class as the front-insert tail drop, #641).
    state.last_enqueued_type = None

    # Inject directive + cut the current segment. The bridge was already
    # committed above, so this remains immediate even under render pressure.
    # An interrupt clobbers whatever directive was pending. If that directive
    # carried an elected Moment Receipt (same-poll directive+interrupt election,
    # or a timer-poll interrupt landing on a waiting ritual directive), demote
    # it honestly — its recipe cooldown is already spent and it can never air.
    _mark_moment_dropped(
        state,
        state.ha_pending_directive_moment_id,
        "interrupt_override",
        "interrupt-override",
    )
    state.ha_pending_directive = spec.directive
    # Reactive-trigger interrupts carry no receipt; the ritual caller overwrites
    # this with its row id right after a successful fire.
    state.ha_pending_directive_moment_id = ""
    state.ha_pending_directive_source = directive_source
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT
    state.force_next = SegmentType.BANTER  # safety belt if chaos_pending is raced
    state.chaos_cutover_epoch += 1
    if skip_event is not None:
        skip_event.set()

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


def _observe_home_context_director(state: StationState, config: StationConfig, context: HomeContext) -> None:
    """Refresh the director's strict projection without adding HA polling."""
    director = state.home_context_director
    if director is None:
        return
    observations: list[DirectorObservation] = []
    for entity in context.scored:
        try:
            observation = DirectorObservation.from_home_assistant_state(
                entity.entity_id,
                entity.raw_state,
                score=entity.score,
                area=entity.area,
            )
        except Exception:
            observation = None
        if observation is not None:
            observations.append(observation)
    try:
        # One load: policy_revision/muted/personal_moment_opt_ins are all slices
        # of the same normalized policy dict — three helper calls would re-lock
        # and re-stat the same file per HA refresh.
        policy = load_entity_policy(config.cache_dir)
        muted = policy.get("muted", {})
        opt_ins = policy.get("personal_moment_opt_ins", {})
        # Narrow-mode observations carry the synthetic ambient id, but an operator
        # may mute the real HA source. Expand so a muted real source suppresses its
        # synthetic projection the same way the fetch layer already does.
        muted_ids = expand_muted_with_ambient_sources(
            set(muted) if isinstance(muted, dict) else set(),
            context.ambient_sources,
        )
        director.observe(
            observations,
            policy_revision=int(policy.get("policy_revision", 0) or 0),
            muted_entity_ids=muted_ids,
            personal_moment_opt_ins=set(opt_ins) if isinstance(opt_ins, dict) else set(),
        )
    except Exception:
        logger.debug("Home context director observation failed", exc_info=True)


def _home_context_ready_for_first_moment(ha_cache: HomeContext) -> bool:
    if len(ha_cache.scored) < FIRST_HOME_CONTEXT_MIN_ENTITIES:
        return False
    if not (ha_cache.summary or "").strip():
        return False
    return any(
        any(str(getattr(entity, field, "") or "").strip() for field in ("area", "label_en", "label_it"))
        for entity in ha_cache.scored
    )


def _has_refresh_budget_context(ctx: HomeContext | None) -> bool:
    """True only for a genuinely populated context — not the empty timeout fallback.

    A successful fetch stamps ``timestamp`` (and usually fills ``scored``/``summary``);
    the empty ``HomeContext()`` we air on after a timeout has none of these. Gating
    the cold-vs-warm budget on this prevents the empty fallback from poisoning the
    cache state: without it, one cold timeout would store an empty context, and the
    next refresh would see "a cache" and drop to the tight budget — so a healthy-but-
    slow registry/weather warm-up could time out forever until restart.
    """
    return ctx is not None and (ctx.timestamp > 0 or bool(ctx.scored) or bool((ctx.summary or "").strip()))


def _has_real_home_context(ctx: HomeContext | None) -> bool:
    """Backward-compatible alias for refresh-budget context readiness."""
    return _has_refresh_budget_context(ctx)


class _HAContextRefreshCoordinator:
    """Producer-owned single-flight mailbox for HA prompt context.

    The foreground deadline is deliberately a *wait* deadline rather than a
    request deadline.  When it expires, production continues with the last
    prompt-safe snapshot while the one retained request may finish in the
    background.  Only this coordinator reads its result and mutates producer
    refresh telemetry, so a late task can never update a prompt or event
    baseline in the middle of a render.
    """

    def __init__(self, config: StationConfig, state: StationState) -> None:
        self._config = config
        self._state = state
        # An explicitly injected legacy fetch owns its synthetic snapshot; do
        # not let a previous module cache turn that test/integration response
        # into an accidental stale-gap resynchronization.
        self._context = (
            None
            if _uses_injected_legacy_fetch()
            else get_cached_home_context(config.cache_dir, authorization=state.home_authorization)
        )
        self._task: asyncio.Task[_HomeContextFetchOutcome] | None = None
        self._attempt_baseline_timestamp = 0.0
        self._attempt_started_at = 0.0
        self._attempt_started_monotonic = 0.0
        self._attempt_finished_monotonic = 0.0
        self._attempt_started_after_stale_gap = False
        self._foreground_timed_out = False
        self._home_event_handoffs_allowed = True
        self._next_retry_not_before = 0.0
        self._closed = False
        self._state.ha_context_refresh_stale_after_seconds = self.stale_threshold_seconds
        self._state.ha_context_refresh_configured = bool(
            config.homeassistant.enabled
            and config.homeassistant.context_enabled
            and config.ha_token
            and config.homeassistant.url
        )
        # Status serialization may inspect this private mailbox read-only to
        # distinguish a still-running request from a completed reply awaiting
        # adoption. No completion callback writes StationState.
        self._state.ha_context_refresh_mailbox = self
        self._sync_freshness()

    @property
    def current_context(self) -> HomeContext | None:
        """The most recently adopted source snapshot (diagnostic only)."""
        return self._context

    @property
    def home_event_handoffs_allowed(self) -> bool:
        """Whether the ledger may offer home-event material to a prompt."""
        return self._home_event_handoffs_allowed

    @property
    def in_flight_task(self) -> asyncio.Task[_HomeContextFetchOutcome] | None:
        """Test-visible retained task; production code never exposes this."""
        return self._task

    def read_refresh_mailbox_status(self) -> dict[str, object]:
        """Return read-only terminal detail for the authenticated status view.

        A completed task can be waiting for the next safe producer boundary.
        The admin serializer needs to distinguish an adoptable fresh reply from
        a failed/expired request during that small window, without a completion
        callback mutating ``StationState``.  Calling ``Task.result()`` here is
        safe because the task is already done and the producer will still drain
        the same result at its next preparation boundary.
        """
        task = self._task
        if task is None:
            return {
                "in_flight": False,
                "adoption_pending": False,
                "last_result": None,
                "last_result_duration_ms": None,
                "last_result_used_background": False,
            }
        if not task.done():
            return {
                "in_flight": True,
                "adoption_pending": False,
                "last_result": None,
                "last_result_duration_ms": None,
                "last_result_used_background": False,
            }

        finished_at = self._attempt_finished_monotonic or time.monotonic()
        duration_ms = round(max(0.0, finished_at - self._attempt_started_monotonic) * 1000)
        used_background = self._foreground_timed_out
        try:
            outcome = task.result()
        except asyncio.CancelledError:
            result = "failed"
        except TimeoutError:
            result = "background_timeout"
        except Exception:
            result = "failed"
        else:
            duration_ms = round(max(0.0, outcome.duration_seconds) * 1000)
            if not outcome.is_adoptable_from(self._attempt_baseline_timestamp):
                result = "failed"
            elif self._is_stale(outcome.context):
                # A reply can finish fresh enough for the request but wait in
                # the mailbox until it is too old for prompt use. It is not
                # ready to adopt merely because the task completed.
                result = "stale"
            else:
                return {
                    "in_flight": False,
                    "adoption_pending": True,
                    "last_result": "success",
                    "last_result_duration_ms": duration_ms,
                    "last_result_used_background": used_background,
                }
        return {
            "in_flight": False,
            "adoption_pending": False,
            "last_result": result,
            "last_result_duration_ms": duration_ms,
            "last_result_used_background": used_background,
        }

    @property
    def stale_threshold_seconds(self) -> float:
        return max(2.0 * float(self._config.homeassistant.poll_interval), _HA_CONTEXT_MIN_STALE_SECONDS)

    @property
    def _poll_interval_seconds(self) -> float:
        return max(0.01, float(self._config.homeassistant.poll_interval))

    def _is_stale(self, context: HomeContext | None = None) -> bool:
        snapshot = self._context if context is None else context
        return bool(
            snapshot is not None and snapshot.timestamp > 0 and snapshot.age_seconds > self.stale_threshold_seconds
        )

    def _sync_freshness(self) -> None:
        # No task callback writes state.  This method is called only by the
        # producer at a safe preparation boundary (or explicit shutdown).
        self._state.ha_context_refresh_stale = self._is_stale()

    def _suppress_stale_handoffs(self) -> None:
        """Drop not-yet-rendered HA event material once its source is over-age."""
        if self._state.ha_pending_directive_source == "ha":
            _mark_moment_dropped(
                self._state,
                self._state.ha_pending_directive_moment_id,
                "stale_context",
                "stale-home-directive",
            )
            self._state.ha_pending_directive = ""
            self._state.ha_pending_directive_moment_id = ""
            self._state.ha_pending_directive_source = ""

        # EveningLedger is exclusively home-event material. Do not let an old
        # bucket bypass the blank stale prompt view as a running gag.
        if self._state.ha_running_gag or self._state.ha_running_gag_key or self._state.ha_running_gag_moment_id:
            _mark_moment_dropped(
                self._state,
                self._state.ha_running_gag_moment_id,
                "stale_context",
                "stale-home-gag",
            )
            self._state.ha_running_gag = ""
            self._state.ha_running_gag_key = ""
            self._state.ha_running_gag_moment_id = ""

    def suppress_stale_handoffs(self) -> None:
        """Clear prompt artifacts that cannot safely outlive a stale snapshot."""
        self._suppress_stale_handoffs()

    def _fallback_prompt_context(self) -> HomeContext:
        """Return a safe context without consuming any one-shot handoffs."""
        self._sync_freshness()
        if self._context is None:
            return HomeContext()
        if self._is_stale():
            # Retain the real source timestamp for operator diagnostics, but
            # withhold every ambient detail from future prompt construction.
            self._suppress_stale_handoffs()
            return HomeContext(timestamp=self._context.timestamp)
        # Cache fallback is a repeatable prompt view: it re-applies live mutes
        # and deliberately clears radio/ritual one-shots.
        return apply_entity_mute_policy(self._context, self._config.cache_dir)

    @staticmethod
    def _without_delayed_one_shots(context: HomeContext) -> HomeContext:
        """Keep ambient state after a stale gap, never replay delayed events."""
        return replace(
            context,
            events=deque(maxlen=context.events.maxlen),
            radio_events=[],
            ritual_recipe_matches=[],
            ritual_public_families=[],
            ritual_recipe_audit=[],
            events_summary="",
            events_summary_en="",
            last_event_label_en="",
        )

    def _start_attempt(self) -> None:
        if self._closed or self._task is not None:
            return

        self._attempt_baseline_timestamp = self._context.timestamp if self._context is not None else 0.0
        self._attempt_started_at = time.time()
        self._attempt_started_monotonic = time.monotonic()
        self._attempt_finished_monotonic = 0.0
        self._attempt_started_after_stale_gap = self._is_stale()
        self._foreground_timed_out = False

        async def _bounded_fetch() -> _HomeContextFetchOutcome:
            # This is the sole total-request timeout.  The foreground wait uses
            # shield() below, so it cannot cancel this request at two seconds.
            # Keep the actual request as its own task so expiry and producer
            # shutdown both explicitly cancel *and await* it.  ``wait_for``
            # alone would cancel implicitly, which makes the ownership and
            # cleanup boundary needlessly opaque.
            request = asyncio.create_task(
                _fetch_producer_context_outcome(
                    ha_url=self._config.homeassistant.url,
                    ha_token=self._config.ha_token,
                    poll_interval=self._poll_interval_seconds,
                    cache=self._context,
                    cache_dir=self._config.cache_dir,
                    radio_event_rules=self._config.radio_events,
                    authorization=self._state.home_authorization,
                    observed_entity_ids_callback=self._state.home_entity_ids_observer,
                ),
                name="ha-context-fetch",
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(request),
                    timeout=_HA_CONTEXT_BACKGROUND_TIMEOUT,
                )
            finally:
                # A foreground timeout never reaches here: it awaits the
                # coordinator task through shield(). This finalizer is solely
                # for the total cap or producer shutdown, and leaves no late
                # request able to write after the retained runner is gone.
                if not request.done():
                    request.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await request
                # Private timing only: no task callback writes StationState.
                self._attempt_finished_monotonic = time.monotonic()

        self._task = asyncio.create_task(_bounded_fetch(), name="ha-context-refresh")
        self._state.ha_context_refresh_in_flight = True
        self._state.ha_context_refresh_last_attempt_at = self._attempt_started_at
        self._state.ha_context_refresh_active_foreground_timed_out = False

    def _record_terminal_result(self, result: str, duration_seconds: float, *, used_background: bool) -> None:
        self._state.ha_context_refresh_in_flight = False
        self._state.ha_context_refresh_active_foreground_timed_out = False
        self._state.ha_context_refresh_last_result = result
        self._state.ha_context_refresh_last_result_duration_ms = round(max(0.0, duration_seconds) * 1000)
        self._state.ha_context_refresh_last_result_used_background = used_background
        self._sync_freshness()

    async def _drain_completed_result(self) -> tuple[HomeContext, bool] | None:
        """Adopt a completed fresh result only at this safe producer boundary."""
        task = self._task
        if task is None or not task.done():
            return None

        self._task = None
        used_background = self._foreground_timed_out
        finished_at = self._attempt_finished_monotonic or time.monotonic()
        duration_seconds = max(0.0, finished_at - self._attempt_started_monotonic)
        self._next_retry_not_before = self._attempt_started_at + self._poll_interval_seconds

        try:
            outcome = task.result()
        except asyncio.CancelledError:
            self._record_terminal_result("failed", duration_seconds, used_background=used_background)
            return None
        except TimeoutError:
            self._record_terminal_result("background_timeout", duration_seconds, used_background=used_background)
            logger.warning(
                "HA context refresh exceeded %.1fs total cap — keeping the last safe snapshot",
                _HA_CONTEXT_BACKGROUND_TIMEOUT,
            )
            return None
        except Exception:
            self._record_terminal_result("failed", duration_seconds, used_background=used_background)
            logger.warning("HA context refresh task failed (non-fatal)", exc_info=True)
            return None

        duration_seconds = outcome.duration_seconds
        if not outcome.is_adoptable_from(self._attempt_baseline_timestamp):
            # Cached/failed outcomes and snapshots no newer than the request's
            # starting baseline must not overwrite a safe adopted snapshot.
            self._record_terminal_result("failed", duration_seconds, used_background=used_background)
            return None

        active_mode = (self._state.home_authorization or HomeAuthorization.narrow()).mode.value
        # The injected-legacy fetch seam (tests/embedding) normalizes a mocked
        # context through _legacy_mock_home_context and does not preserve the
        # authorization stamp; it is trusted test input and never active in
        # production, where the real fetch always stamps the requested mode.
        if not _uses_injected_legacy_fetch() and outcome.context.authorization_mode != active_mode:
            # Authorization is install-scoped: a fetch that returns a context
            # stamped for the other mode (a bug or a reused cross-mode cache)
            # must never be adopted. Fail closed to the last safe snapshot.
            logger.error(
                "HA context authorization mismatch (%s != %s); discarding refreshed context",
                outcome.context.authorization_mode,
                active_mode,
            )
            self._record_terminal_result("failed", duration_seconds, used_background=used_background)
            return None

        # A request that *started* while the prior snapshot was safe keeps its
        # legitimate one-shots when it is adopted promptly, even if the prior
        # snapshot crossed the threshold in flight. A reply that itself has
        # aged past the threshold while waiting in the mailbox is different:
        # it must never become prompt input.
        was_stale_gap = self._attempt_started_after_stale_gap
        adopted = revalidate_home_context_mutes(outcome.context, self._config.cache_dir)
        stale_at_adoption = self._is_stale(adopted)
        if was_stale_gap or stale_at_adoption:
            self._suppress_stale_handoffs()
            # The next normal poll re-establishes event continuity. Until then,
            # do not let a pre-gap EveningLedger bucket leak as a new prompt gag.
            self._home_event_handoffs_allowed = False
            adopted = self._without_delayed_one_shots(adopted)
        else:
            self._home_event_handoffs_allowed = True

        # Publish both the accepted snapshot and its event-matcher baselines as
        # one producer-owned handoff.  No background-task callback can do this.
        if not _uses_injected_legacy_fetch():
            _publish_home_context_outcome(replace(outcome, context=adopted))
        self._context = adopted
        self._record_terminal_result(
            "stale" if stale_at_adoption else "success",
            duration_seconds,
            used_background=used_background,
        )
        if stale_at_adoption:
            return self._fallback_prompt_context(), False
        return adopted, not was_stale_gap

    def _refresh_is_due(self) -> bool:
        if self._context is None:
            return True
        return self._context.age_seconds >= self._poll_interval_seconds

    def _foreground_budget_seconds(self) -> float:
        have_context = _has_refresh_budget_context(self._context)
        if have_context:
            return float(self._config.homeassistant.context_refresh_timeout)
        return max(float(self._config.homeassistant.context_refresh_timeout), _HA_CONTEXT_COLD_LOAD_TIMEOUT)

    async def prepare_for_segment(self) -> tuple[HomeContext, bool]:
        """Return prompt context and whether this boundary owns fresh one-shots.

        Call only immediately before prompt construction for BANTER, AD, or
        NEWS_FLASH.  ``True`` means the returned context was freshly adopted
        at this boundary and its event/directive handoffs may be consumed once.
        """
        if self._closed:
            return self._fallback_prompt_context(), False

        adopted = await self._drain_completed_result()
        if adopted is not None:
            return adopted

        now = time.time()
        # A rebound legacy dependency is an explicit injected fetch (used by
        # older embedding/test callers), so honor it even if a prior module
        # cache is still within its poll interval.
        refresh_due = self._refresh_is_due() or _uses_injected_legacy_fetch()
        if self._task is None and refresh_due and now >= self._next_retry_not_before:
            self._start_attempt()

        task = self._task
        if task is None:
            return self._fallback_prompt_context(), False

        # After the foreground wait has already expired, later eligible
        # segments must never each pay another two-second wait. They reuse the
        # last safe view until this same task finishes and is drained above.
        if self._foreground_timed_out:
            if task.done():
                adopted = await self._drain_completed_result()
                if adopted is not None:
                    return adopted
            return self._fallback_prompt_context(), False

        try:
            # Shield is the key recovery seam: the foreground deadline returns
            # audio production to the caller without cancelling the owned task.
            await asyncio.wait_for(asyncio.shield(task), timeout=self._foreground_budget_seconds())
        except TimeoutError:
            self._foreground_timed_out = True
            self._state.ha_context_refresh_active_foreground_timed_out = True
            self._sync_freshness()
            logger.warning(
                "HA context foreground wait exceeded %.1fs — continuing audio while the refresh catches up",
                self._foreground_budget_seconds(),
            )
            return self._fallback_prompt_context(), False

        adopted = await self._drain_completed_result()
        if adopted is not None:
            return adopted
        return self._fallback_prompt_context(), False

    async def close(self) -> None:
        """Explicitly cancel and await the retained request during producer exit."""
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._state.ha_context_refresh_in_flight = False
        self._state.ha_context_refresh_active_foreground_timed_out = False
        if self._state.ha_context_refresh_mailbox is self:
            self._state.ha_context_refresh_mailbox = None


def _apply_radio_event_matches(state: StationState, matches: list[RadioEventMatch]) -> list[HomeEvent]:
    """Apply configured HA radio-event matches to existing producer handoff paths."""
    gag_events: list[HomeEvent] = []
    for match in matches:
        if match.mode == "gag":
            gag_events.append(match.event)
            continue
        if match.mode != "directive" or not match.directive:
            continue
        if state.ha_pending_directive:
            continue
        state.ha_pending_directive = match.directive
        # Radio-event directives have no Moment Receipt in v1 — clear any stale
        # ritual id so it cannot attach to the wrong banter.
        state.ha_pending_directive_moment_id = ""
        state.ha_pending_directive_source = "ha"
        commit_radio_event_directive(match)
    return gag_events


def _record_ritual_moment(
    state: StationState,
    match: RitualRecipeMatch,
    *,
    lane: str,
    status: str = "elected",
    drop_reason: str = "",
) -> str:
    """Best-effort Moment Receipt row for a ritual match. Never raises.

    Returns the row id ("" when the store is absent or the write failed) so
    callers can thread it toward the consuming segment's metadata.
    """
    store = state.moment_store
    if store is None:
        return ""
    try:
        return store.record(
            lane=lane,
            family=match.recipe.family,
            public_label=match.recipe.public_family_label,
            entity_id=match.entity_id,
            confidence=match.confidence,
            status=status,
            drop_reason=drop_reason,
        )
    except Exception as exc:  # pragma: no cover - receipts must never break production
        logger.debug("Moment receipt record failed: %s", exc)
        return ""


def _apply_ritual_recipe_matches(
    state: StationState,
    matches: list[RitualRecipeMatch],
) -> tuple[list[HomeEvent], _PendingRitualInterrupt | None]:
    """Apply bundled ritual recipe matches to existing delivery lanes."""
    gag_events: list[HomeEvent] = []
    interrupt: _PendingRitualInterrupt | None = None
    for match in matches:
        lane = match.recipe.delivery_lane
        if lane == "running_gag":
            gag_events.append(match.to_home_event())
            continue
        if lane == "ambient_context":
            continue
        if lane == "interrupt":
            if (
                interrupt is not None
                or state.chaos_pending is not None
                or state.operator_force_pending is not None
                or state.force_next is not None
            ):
                # Cleared the matcher but lost the slot — visible in the admin
                # Moments panel so "why did nothing happen" has an answer.
                _record_ritual_moment(state, match, lane=lane, status="dropped", drop_reason="interrupt_slot_busy")
                continue
            interrupt = _PendingRitualInterrupt(
                match=match,
                spec=InterruptSpec(
                    directive=match.recipe.directive,
                    urgency=match.recipe.interrupt_urgency,
                    cooldown=match.recipe.cooldown_seconds,
                ),
            )
            continue
        if lane != "directive" or not match.recipe.directive:
            continue
        if state.ha_pending_directive:
            _record_ritual_moment(state, match, lane=lane, status="dropped", drop_reason="directive_slot_busy")
            continue
        state.ha_pending_directive = match.recipe.directive
        # The receipt id travels WITH the directive: the scriptwriter hands it
        # off to the segment build, and confirmed-air flips it to aired.
        state.ha_pending_directive_moment_id = _record_ritual_moment(state, match, lane=lane)
        state.ha_pending_directive_source = "ha"
        commit_ritual_recipe_match(match)
    return gag_events, interrupt


def _maybe_arm_first_home_context_moment(
    state: StationState,
    ha_cache: HomeContext,
    seg_type: SegmentType,
    *,
    can_generate_banter: bool = True,
) -> None:
    if state.ha_first_home_context_moment_fired:
        return
    if not can_generate_banter:
        return
    if state.ha_pending_directive or state.chaos_pending is not None or state.operator_force_pending is not None:
        return
    if state.force_next is not None:
        return
    if not _home_context_ready_for_first_moment(ha_cache):
        return

    state.ha_pending_directive = FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
    state.ha_pending_directive_moment_id = ""  # not a ritual moment — no receipt
    state.ha_pending_directive_source = "ha"
    if seg_type != SegmentType.BANTER:
        state.force_next = SegmentType.BANTER


def _cache_eviction_protected_paths(queue: asyncio.Queue[Segment], state: StationState) -> set[Path]:
    """Paths an LRU cache eviction pass must never remove.

    Both the real playback queue and the capacity-exempt continuity slot hold
    ready audio; evicting either would break delivery mid-stream. The slot is
    absent from the real queue by design, so it is protected explicitly.
    """
    protected = {seg.path for seg in list(getattr(queue, "_queue", ())) if seg.path}
    if state.continuity_slot is not None and state.continuity_slot.path:
        protected.add(state.continuity_slot.path)
    return protected


async def run_producer(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
    skip_event: asyncio.Event | None = None,
) -> None:
    """Run production with explicit ownership of any late HA refresh request."""
    context_coordinator = _HAContextRefreshCoordinator(config, state)
    try:
        await _run_producer_inner(
            queue,
            state,
            config,
            skip_event,
            context_coordinator=context_coordinator,
        )
    finally:
        # This finally covers cancellation anywhere in the producer loop, not
        # just the HA preparation await.  A late task therefore cannot write
        # state after producer shutdown.
        await context_coordinator.close()


async def _run_producer_inner(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
    skip_event: asyncio.Event | None = None,
    *,
    context_coordinator: _HAContextRefreshCoordinator,
) -> None:
    """Keep the lookahead queue filled with rendered segments for live playback."""
    prev_seg_type = _initial_previous_segment_type(queue, state)
    state.last_enqueued_type = _seed_adjacency_type(queue, state, prev_seg_type)
    observed_continuity_epoch = state.continuity_epoch
    logger.info("Producer started. Playlist: %d tracks", len(state.playlist))

    producer_task = asyncio.current_task()
    if producer_task is not None:

        def _close_timing_on_producer_exit(task: asyncio.Task) -> None:
            # Cancellation or an unexpected task exit can land inside any awaited
            # render stage. Close the in-memory diagnostic after task completion
            # so idle time is never charged to a later attempt.
            if not state._render_timing_started:
                return
            reason = "cancelled" if task.cancelled() else "producer_error" if task.exception() else "producer_exit"
            state.end_gen(ok=False)
            state.finish_render_timing("failed", reason=reason)

        producer_task.add_done_callback(_close_timing_on_producer_exit)

    def _home_fact_policy_is_current(segment: Segment) -> bool:
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        fact_id = metadata.get("home_fact_id")
        if not fact_id:
            return True
        revision = metadata.get("home_fact_policy_revision")
        entity_id = metadata.get("home_fact_entity_id")
        # One load: the revision and the mute set both come from the same policy
        # dict, so read them off a single normalized load instead of two.
        policy = load_entity_policy(config.cache_dir)
        muted = policy.get("muted", {})
        muted_ids = set(muted) if isinstance(muted, dict) else set()
        # A narrow break is tagged with the synthetic ambient id; expand the muted
        # set with the synthetic projection of any muted real source (cheap raw
        # module-cache read, no-op in legacy mode where ambient_sources is empty)
        # so muting the real HA source rejects the break at admission too.
        if muted_ids:
            cached = get_cached_home_context(authorization=state.home_authorization)
            ambient_sources = getattr(cached, "ambient_sources", None) if cached is not None else None
            if ambient_sources:
                muted_ids = expand_muted_with_ambient_sources(muted_ids, ambient_sources)
        return (
            isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision == int(policy.get("policy_revision", 0) or 0)
            and isinstance(entity_id, str)
            and entity_id not in muted_ids
        )

    async def _queue_segment(
        segment: Segment,
        *,
        shadow_entry: dict | None = None,
        stale_check: StaleCheck | None = None,
    ) -> bool:
        """Queue a segment unless the operator stopped the session mid-generation."""
        nonlocal prev_seg_type
        if state.session_stopped:
            state.record_discard(segment, reason=GenerationWasteReason.SESSION_STOPPED)
            if segment.ephemeral and not _is_packaged_asset(segment.path):
                segment.path.unlink(missing_ok=True)
            logger.info("Discarding %s because the session is stopped", segment.type.value)
            return False
        if not _home_fact_policy_is_current(segment):
            state.record_discard(segment, reason=GenerationWasteReason.OPERATOR_PURGE)
            _unlink_if_tmp_render(segment, config.tmp_dir)
            return False

        def _home_fact_is_stale(_segment: Segment = segment) -> bool:
            return not _home_fact_policy_is_current(_segment)

        def _combined_stale_check() -> bool:
            # Discard if EITHER the caller's staleness gate (continuity epoch,
            # source/playlist/chaos) OR the home-fact policy check fires.
            if stale_check is not None and stale_check():
                return True
            return _home_fact_is_stale()

        if not await _enqueue_with_egress(
            queue,
            state,
            config,
            segment,
            shadow_entry=shadow_entry,
            stale_check=_combined_stale_check,
        ):
            return False
        prev_seg_type = _adjacency_type_for(segment)
        return True

    # The coordinator owns the adopted snapshot and the one in-flight request.
    # This local only feeds the existing prompt/status projection below.
    ha_cache: HomeContext | None = context_coordinator.current_context

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

    home_authorization = state.home_authorization or HomeAuthorization.narrow()
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
                            artwork_url=config.brand.artwork_url,
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
        if config.homeassistant.timer_interrupts and home_authorization.allows_household_moments:
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
                            muted_ids = muted_entity_ids(config.cache_dir)
                            for muted_id in muted_ids:
                                _timer_old_states.pop(muted_id, None)
                            for eid in _timer_entity_ids:
                                if eid in muted_ids:
                                    continue
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
                            if muted_ids:
                                timer_events = deque(
                                    (event for event in timer_events if event.entity_id not in muted_ids),
                                    maxlen=timer_events.maxlen,
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
                                        directive_source="timer",
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
        if observed_continuity_epoch != state.continuity_epoch:
            # A streamer control rebuilt the queue outside this coroutine. Re-read
            # its final tail before producing again so a removed song cannot lend
            # a talk bed or transition sting to the next speech segment.
            queued = list(getattr(queue, "_queue", ()))
            prev_seg_type = _adjacency_type_for(queued[-1]) if queued else None
            state.last_enqueued_type = prev_seg_type
            observed_continuity_epoch = state.continuity_epoch
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
                            artwork_url=config.brand.artwork_url,
                        )
                    )
                )
            # Deliberately NOT cancelled: .cancel() only detaches the asyncio.Task
            # wrapper, it can't interrupt the in-flight executor ffmpeg (same
            # limitation the relaunch guard below is built around). Cancelling
            # here would flip _prefetch_task.done() to True while the executor
            # thread keeps running, so the relaunch guard would launch a SECOND
            # prefetch on resume — the exact duplicate-background-work race this
            # PR closes elsewhere. Left running, it finishes on its own (or is
            # still in flight, in which case the guard correctly skips a relaunch).
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
                await _queue_continuity_bridge(
                    _queue_segment,
                    state,
                    config,
                    bridge_type="resume",
                    bridge_flag="resume_bridge",
                    canned_title="Resume bridge",
                    music_runway=True,
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
                # idle_bridge marks canned warm-up clips as rescue audio so the
                # fallback classifier does not report them as the primary station.
                await _queue_continuity_bridge(
                    _queue_segment,
                    state,
                    config,
                    bridge_type="idle",
                    bridge_flag="idle_bridge",
                    canned_title="Station warm-up",
                    canned_metadata={"warmup": True, "rescue": True},
                    music_runway=True,
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
            and not _runway_fill_needed(queue)
            and state.force_next is None
            and state.chaos_pending is None
        ):
            # Periodically evict stale cache files while the producer is idle
            now = asyncio.get_running_loop().time()
            if now - _last_cache_eviction >= _cache_eviction_interval:
                _last_cache_eviction = now
                # Protect norm files currently in the playback queue — and the
                # capacity-exempt continuity slot — from eviction. Evicting ready
                # audio would break delivery mid-stream.
                queued_paths = _cache_eviction_protected_paths(queue, state)
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
                # Doorway: a banned song must not slip back in via the mid-session
                # chart refresh either (no restart needed to reintroduce it).
                new_tracks = filter_blocklisted(new_tracks, state.blocklist)
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
        elif _release_campaign_should_force_first_banter(state):
            seg_type = SegmentType.BANTER
            logger.info("Release campaign first airing: forcing a safe banter slot")
        else:
            seg_type = next_segment_type(state, config.pacing)
            if seg_type in _RUNWAY_GOVERNED_TYPES:
                should_defer, buffered = _should_defer_for_runway(queue, config.pacing.lookahead_segments)
                if should_defer:
                    logger.info(
                        "Runway governor: %.0fs buffered < %ds floor; airing music instead of %s",
                        buffered,
                        RUNWAY_FLOOR_SECONDS,
                        seg_type.value,
                    )
                    seg_type = SegmentType.MUSIC
        if seg_type == SegmentType.MUSIC and not state.playlist:
            logger.warning("Rotation pool empty; producing recovery banter until tracks are re-added")
            seg_type = SegmentType.BANTER
            if is_operator_forced:
                state.operator_force_pending = None
                is_operator_forced = False
        segment: Segment | None = None
        generation_revision = state.playlist_revision
        # source_revision bumps ONLY on a true source switch (switch_playlist),
        # while playlist_revision also bumps on benign in-place edits (shuffle/
        # add/move/enrich). Capturing both lets the stale gate tell a source
        # switch (stale_source) apart from a same-source playlist edit
        # (stale_playlist) for honest waste telemetry (#397).
        generation_source_revision = state.source_revision
        # Live controls reserve continuity before their destructive queue change.
        # A completed render from before that change must never refill the queue
        # after the reservation has made its safety promise.
        generation_continuity_epoch = state.continuity_epoch

        def _enqueue_stale_reason(
            captured_revision: int = generation_revision,
            captured_source_revision: int = generation_source_revision,
            captured_chaos_epoch: int = generation_chaos_epoch,
            captured_continuity_epoch: int = generation_continuity_epoch,
        ) -> str | None:
            if state.session_stopped:
                return GenerationWasteReason.SESSION_STOPPED
            if captured_source_revision != state.source_revision:
                return GenerationWasteReason.STALE_SOURCE
            if captured_revision != state.playlist_revision:
                return GenerationWasteReason.STALE_PLAYLIST
            if captured_chaos_epoch != state.chaos_cutover_epoch:
                return GenerationWasteReason.STALE_CHAOS
            if captured_continuity_epoch != state.continuity_epoch:
                return GenerationWasteReason.STALE_CONTINUITY
            return None

        success_callback: Callable[[], None] | None = None
        banter_commit = None
        post_failure_backoff: float | None = None

        async def _sleep_post_failure_backoff(delay: float | None) -> None:
            if delay is not None:
                await asyncio.sleep(delay)
                if asyncio.sleep is not _REAL_ASYNCIO_SLEEP:
                    await _REAL_ASYNCIO_SLEEP(0.02)

        # Per-iteration reset of the cross-domain-callback "landed" flag. The
        # flash/ad branches also reset it before generating, but resetting here
        # too keeps it provably scoped to one segment — a stale True from a
        # flash that failed mid-generation can never reach the next flash/ad.
        state.pending_callback_landed = False
        # Render-latency deep-dive: total wall time to build this segment, logged
        # at INFO on the Queued line below. Per-stage ffmpeg breakdown is at DEBUG
        # in audio/normalizer.py (set LOG_LEVEL=DEBUG for a soak).
        _t_render = time.monotonic()
        state.begin_render_timing(seg_type.value, started=_t_render)

        # Refresh Home Assistant context for banter/ad/news-flash segments.
        # NEWS_FLASH is included so the meteo flash grounds itself in a freshly
        # refreshed forecast (#626) — without it the weather arc was only ever
        # refreshed for banter/ad and a flash could air the startup snapshot.
        # The refresh is poll_interval-gated (cache read in the common case), and
        # the news-flash category (weather vs sports/traffic) isn't known until
        # write_news_flash runs, so the gate necessarily covers every flash, not
        # just weather. Staleness is bounded by the weather cache TTL plus one
        # poll interval, not made real-time — the TTL is deliberately unchanged.
        if (
            config.homeassistant.enabled
            and config.homeassistant.context_enabled
            and config.ha_token
            and seg_type
            in (
                SegmentType.BANTER,
                SegmentType.AD,
                SegmentType.NEWS_FLASH,
            )
        ):
            # A foreground timeout is a wait timeout, not request cancellation.
            # The coordinator keeps exactly one request alive for up to 30s and
            # drains an accepted late result here, immediately before prompt
            # construction.  It never touches already-rendering/queued audio.
            # Authorization (narrow vs legacy) is threaded through the coordinator
            # from state.home_authorization at fetch time.
            ha_cache, fresh_one_shot_handoff = await context_coordinator.prepare_for_segment()
            # Fail-soft: the scene namer is a mood garnish, and this block runs
            # OUTSIDE the segment-render try below — an exception here would
            # kill the producer task itself (INSTANT AUDIO). Same posture as
            # the schedule_label_generation wrap further down.
            if home_authorization.mode is HomeAuthorizationMode.NARROW:
                mood_it, mood_en = "", ""
            else:
                try:
                    mood_it, mood_en = resolve_home_mood(config, state, ha_cache)
                except Exception:
                    logger.warning("HA mood resolution failed (non-fatal)", exc_info=True)
                    mood_it, mood_en = ha_cache.mood, ha_cache.mood_en
            state.ha_context = ha_cache.summary
            state.ha_events_summary = ha_cache.events_summary
            state.ha_home_mood = mood_it
            state.ha_weather_arc = ha_cache.weather_arc
            state.ha_home_mood_en = mood_en
            state.ha_weather_arc_en = ha_cache.weather_arc_en
            state.ha_events_summary_en = ha_cache.events_summary_en
            state.ha_scored_entities = [entity.to_status_dict() for entity in ha_cache.scored]
            state.ha_denylist_hits = dict(ha_cache.denylist_hits)
            state.ha_catalog_hit_rate = ha_cache.catalog_hit_rate
            state.ha_label_stats = dict(getattr(ha_cache, "label_stats", {}) or {})
            state.ha_registry_source = str(getattr(ha_cache, "registry_source", "") or "")
            state.ha_context_entity_count = len(ha_cache.scored)
            state.ha_context_char_count = len(ha_cache.summary or "")
            _observe_home_context_director(state, config, ha_cache)
            ritual_matches = list(getattr(ha_cache, "ritual_recipe_matches", []) or [])
            state.ha_ritual_public_families = list(getattr(ha_cache, "ritual_public_families", []) or [])[:4]
            state.ha_ritual_context = ", ".join(state.ha_ritual_public_families)
            state.ha_ritual_matches = [
                match.to_status_dict() for match in ritual_matches if hasattr(match, "to_status_dict")
            ][:8]
            state.ha_ritual_recipe_audit = list(getattr(ha_cache, "ritual_recipe_audit", []) or [])[:16]
            raw_states = getattr(ha_cache, "raw_states", {})
            if isinstance(raw_states, dict) and home_authorization.mode is not HomeAuthorizationMode.NARROW:
                # Fail-soft: scheduling does synchronous preflight work before
                # creating the background task; an exception here must never
                # stop segment production (INSTANT AUDIO).
                try:
                    schedule_label_generation(
                        raw_states,
                        cache_dir=config.cache_dir,
                        config=config,
                        score_by_entity={entity.entity_id: entity.score for entity in ha_cache.scored},
                    )
                except Exception:
                    logger.warning("HA label generation scheduling failed (non-fatal)", exc_info=True)
            timestamp = getattr(ha_cache, "timestamp", 0.0)
            state.ha_context_last_updated = timestamp if isinstance(timestamp, int | float) else 0.0
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
            # Phase 4: reactive triggers — interrupt takes priority over ambient
            # directives.  A cached prompt view can retain recent events for
            # display, so only a just-adopted fresh handoff may consume them.
            if fresh_one_shot_handoff and not state.ha_pending_directive:
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
                    state.ha_pending_directive_moment_id = ""  # not a ritual moment
                    state.ha_pending_directive_source = "ha"
            radio_gag_events: list[HomeEvent] = []
            ritual_gag_events: list[HomeEvent] = []
            ritual_interrupt: _PendingRitualInterrupt | None = None
            if fresh_one_shot_handoff:
                radio_gag_events = _apply_radio_event_matches(state, list(getattr(ha_cache, "radio_events", []) or []))
                ritual_gag_events, ritual_interrupt = _apply_ritual_recipe_matches(state, ritual_matches)
            if ritual_interrupt is not None:
                fired = await _fire_interrupt(
                    state,
                    ritual_interrupt.spec,
                    queue,
                    skip_event,
                    enforce_global_cooldown=True,
                    bridge_tmp_dir=config.tmp_dir,
                )
                if fired:
                    commit_ritual_recipe_match(ritual_interrupt.match)
                    # _fire_interrupt planted the directive (and blanked the
                    # receipt id); attach this moment's row so the urgent
                    # banter that consumes it carries the receipt to air.
                    state.ha_pending_directive_moment_id = _record_ritual_moment(
                        state, ritual_interrupt.match, lane="interrupt"
                    )
                else:
                    _record_ritual_moment(
                        state,
                        ritual_interrupt.match,
                        lane="interrupt",
                        status="dropped",
                        drop_reason="interrupt_cooldown",
                    )
            if home_authorization.mode is not HomeAuthorizationMode.NARROW:
                _maybe_arm_first_home_context_moment(
                    state,
                    ha_cache,
                    seg_type,
                    can_generate_banter=_sw.has_script_llm(config),
                )

            # Impossible Moments v2 (A): fold new events into the evening ledger
            # (watermark-deduped) and, for banter only, surface one eligible
            # running-gag. Ads stay gag-free in v0. The ledger persists across
            # the addon's frequent restarts.
            if (
                state.evening_ledger is not None
                and home_authorization.mode is not HomeAuthorizationMode.NARROW
                and not state.ha_context_refresh_stale
                and context_coordinator.home_event_handoffs_allowed
            ):
                _now = time.time()
                state.evening_ledger.observe([*ha_cache.events, *radio_gag_events, *ritual_gag_events], now=_now)
                if seg_type == SegmentType.BANTER:
                    # Offer (don't spend) — the cooldown is marked in the banter
                    # success callback only if generated banter actually airs, so
                    # an LLM failure that falls back to a canned clip does not burn
                    # the callback.
                    offered = state.evening_ledger.offer_gag(now=_now)
                    if offered is not None:
                        state.ha_running_gag_key, state.ha_running_gag = offered
                        # Moment Receipt for ritual-sourced gags only: the
                        # bucket's ritual_family provenance (threaded in via
                        # HomeEvent) is what makes a receipt legitimate — a
                        # plain home-event gag has no ritual moment in v1.
                        state.ha_running_gag_moment_id = ""
                        if state.moment_store is not None:
                            try:
                                _bucket = state.evening_ledger.buckets.get(state.ha_running_gag_key)
                                if _bucket is not None and _bucket.ritual_family:
                                    state.ha_running_gag_moment_id = state.moment_store.record(
                                        lane="running_gag",
                                        family=_bucket.ritual_family,
                                        public_label=_bucket.label,
                                        entity_id=_bucket.entity_id,
                                        count=_bucket.count,
                                        now=_now,
                                    )
                            except Exception as exc:  # pragma: no cover - never break production
                                logger.debug("Moment receipt gag record failed: %s", exc)
                    else:
                        state.ha_running_gag = ""
                        state.ha_running_gag_key = ""
                        state.ha_running_gag_moment_id = ""
                else:
                    state.ha_running_gag = ""
                    state.ha_running_gag_key = ""
                    state.ha_running_gag_moment_id = ""
                state.evening_ledger.save_if_dirty(config.cache_dir)
            elif home_authorization.mode is HomeAuthorizationMode.NARROW:
                # A copied/restored cache can contain buckets elected by an
                # older install. Narrow mode may retain that file for explicit
                # future recovery, but it never offers or airs those callbacks.
                state.ha_running_gag = ""
                state.ha_running_gag_key = ""
                state.ha_running_gag_moment_id = ""
            elif state.evening_ledger is not None:
                # A stale/resync prompt must not bypass its blank context via a
                # previously observed home-event running gag.
                context_coordinator.suppress_stale_handoffs()
        # Flush Moment Receipts once per cycle at loop level, NOT inside the HA
        # block: streamer-side finalizes (airing → true outcome) set the dirty
        # flag from the playback loop, and must still reach disk when HA context
        # is disabled or its refresh stalls. Dirty-gated — a clean store is a
        # no-op, so this costs nothing on quiet cycles.
        if state.moment_store is not None:
            try:
                state.moment_store.save_if_dirty(config.cache_dir)
            except Exception:
                logger.debug("Moment receipt store save failed", exc_info=True)

        if generation_chaos_epoch != state.chaos_cutover_epoch:
            logger.info("Restarting producer cycle after interrupt cutover")
            state.finish_render_timing("discarded", reason=GenerationWasteReason.STALE_CHAOS)
            continue

        try:
            if seg_type == SegmentType.MUSIC:
                track = _select_accepted_music_track(state, config)
                playlist_idx: int = -1
                if track is None:
                    # All recent candidates denylisted — yield to event loop and retry.
                    state.finish_render_timing("discarded", reason=GenerationWasteReason.BLOCKLIST_GATE)
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
                    rendered = await _render_music_track(
                        track,
                        config,
                        temp_prefix="music",
                        context="music",
                        playlist=state.playlist,
                        timing_state=state,
                    )
                    _gen_ok = rendered is not None
                finally:
                    state.end_gen(ok=_gen_ok)
                if rendered is None:
                    state.finish_render_timing("failed", reason="render_unavailable")
                    continue
                norm_path = rendered.path
                norm_cached = rendered.cache_path
                norm_is_cached = rendered.cache_hit
                audio_source = "download"

                # Quality gate: reject truncated/silent downloads before queueing.
                # Circuit breaker: after MUSIC_QUALITY_GATE_REJECTION_LIMIT consecutive rejections, either serve a
                # packaged recovery clip (when the rejection is due to silence — i.e. all
                # available audio is silent and playing it would cause dead air) or
                # let the track through as-is (when rejected for other reasons such as being
                # short — silence is still worse than a slightly-short real track).
                if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
                    _music_loop = asyncio.get_running_loop()
                    try:
                        with _timed_render_stage(state, "quality"):
                            await _music_loop.run_in_executor(
                                None, validate_segment_audio, norm_path, SegmentType.MUSIC
                            )
                        _music_qg_rejections = 0
                    except AudioToolError as exc:
                        logger.warning("Audio tool unavailable, skipping music quality check: %s", exc)
                    except AudioQualityError as exc:
                        _music_qg_rejections += 1
                        if _music_qg_rejections >= MUSIC_QUALITY_GATE_REJECTION_LIMIT:
                            _music_qg_rejections = 0
                            if "silence" in str(exc).lower():
                                # All available tracks are silent. Playing
                                # them would break the illusion with dead air.  Insert a
                                # packaged recovery clip instead so the stream stays alive.
                                # The rejected normalization is not safe recovery media
                                # either. Remove both its durable cache copy and its
                                # transient render before selecting a fallback.
                                norm_cached.unlink(missing_ok=True)
                                if not norm_is_cached:
                                    norm_path.unlink(missing_ok=True)
                                fallback = _pick_recovery_clip(state)
                                if fallback:
                                    logger.warning(
                                        "Quality gate circuit breaker: %d consecutive silence rejections — "
                                        "inserting packaged recovery clip to prevent dead air (%s: %s)",
                                        MUSIC_QUALITY_GATE_REJECTION_LIMIT,
                                        norm_path.name,
                                        exc,
                                    )
                                    await _queue_segment(
                                        Segment(
                                            type=SegmentType.BANTER,
                                            path=fallback,
                                            metadata={
                                                "type": "banter",
                                                "canned": True,
                                                "silence_fallback": True,
                                                "rescue": True,
                                                "title": "Station continuity",
                                            },
                                            ephemeral=False,
                                        )
                                    )
                                    state.finish_render_timing(
                                        "discarded", reason=GenerationWasteReason.QUALITY_GATE_REJECT
                                    )
                                    continue
                                # No packaged recovery clips — recycle the last known-good music
                                # norm rather than letting a silent file through.
                                last_good = _get_last_music_file(state)
                                if last_good and last_good != norm_cached:
                                    logger.warning(
                                        "Quality gate circuit breaker: silence with no banter fallback — "
                                        "recycling last-known-good music (%s: %s)",
                                        norm_path.name,
                                        exc,
                                    )
                                    await _queue_segment(
                                        Segment(
                                            type=SegmentType.MUSIC,
                                            path=last_good,
                                            metadata={
                                                "type": "music",
                                                "recycled": True,
                                                "silence_fallback": True,
                                                "rescue": True,
                                                "title": last_good.name,
                                            },
                                            ephemeral=False,
                                        )
                                    )
                                    state.finish_render_timing(
                                        "discarded", reason=GenerationWasteReason.QUALITY_GATE_REJECT
                                    )
                                    continue
                                # No recovery clip and no distinct last-known-good file.
                                # Drop this track and let the streamer's rescue path handle
                                # the gap — queueing a rejected silent file would break the
                                # illusion.
                                logger.error(
                                    "Quality gate circuit breaker: silence, no banter, "
                                    "no distinct last-known-good music — dropping track (%s: %s)",
                                    norm_path.name,
                                    exc,
                                )
                                state.finish_render_timing(
                                    "discarded", reason=GenerationWasteReason.QUALITY_GATE_REJECT
                                )
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
                            _record_generated_waste(
                                state,
                                SegmentType.MUSIC,
                                norm_path,
                                GenerationWasteReason.QUALITY_GATE_REJECT,
                                duration_sec=(track.duration_ms or 0) / 1000.0,
                                ephemeral=not norm_is_cached,
                            )
                            if not norm_is_cached:
                                norm_path.unlink(missing_ok=True)
                            state.finish_render_timing("discarded", reason=GenerationWasteReason.QUALITY_GATE_REJECT)
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
                            with _timed_render_stage(state, "mix"):
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
                    duration_sec=(track.duration_ms or 0) / 1000.0,
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
                        "heading_id": track.heading_id,
                    },
                    ephemeral=not norm_is_cached,
                )
                _bound_track = track
                _bound_rendered = rendered

                def _music_callback(_t=_bound_track, _r=_bound_rendered) -> None:
                    _arm_accepted_heading_announcement(state, _t)
                    state.after_music(_t)
                    _remember_rendered_music(_r, state)

                success_callback = _music_callback

            elif seg_type == SegmentType.BANTER:
                logger.info("Producing BANTER")

                # Reset the pending verbal gag — write_banter sets it ONLY on the
                # LLM success path, so a canned/failed banter never leaves a stale
                # gag for the success callback to commit (B-i).
                state.pending_verbal_gag = None
                # Reset the Moment Receipt handoff the same way: a previous cycle
                # that consumed a directive but then died before the build (TTS
                # failure, quality-gate reject, chaos discard) must not leave its
                # id behind for an unrelated banter to wear on air. Its consumed
                # directive is gone, so the elected row is demoted honestly
                # instead of reading "waiting for its break" until retention.
                if (
                    state.last_banter_ritual_moment_id
                    and state.last_banter_ritual_moment_id != state.ha_pending_directive_moment_id
                ):
                    _mark_moment_dropped(
                        state,
                        state.last_banter_ritual_moment_id,
                        "generation_failed",
                        "stale-handoff",
                    )
                state.last_banter_ritual_moment_id = ""
                state.last_banter_home_fact = None

                def _drop_unqueued_banter_receipts(reason: str, context: str) -> None:
                    ritual_id = state.last_banter_ritual_moment_id
                    if ritual_id and ritual_id != state.ha_pending_directive_moment_id:
                        _mark_moment_dropped(state, ritual_id, reason, f"{context}:ritual")
                    state.last_banter_ritual_moment_id = ""
                    _mark_moment_dropped(state, state.ha_running_gag_moment_id, reason, f"{context}:gag")
                    state.ha_running_gag_moment_id = ""

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
                trans_track_ref: str | None = None
                loop = asyncio.get_running_loop()
                first_home_context_moment_pending = state.ha_pending_directive == FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
                home_context_director = state.home_context_director
                # A pending reactive/first-moment directive carries its own home
                # payload (and the FIRST CONNECTED HOME MOMENT directive asks the
                # host to cite concrete home details), so it must keep the legacy
                # ha_context/events/weather sections. Only director-owned casual
                # breaks suppress them, and those are the only breaks that select a
                # prompt_fact — so the flag and the selection share one condition.
                use_directed_home_context = (
                    chaos_subtype is None and home_context_director is not None and not state.ha_pending_directive
                )
                prompt_fact: PromptFact | None = None
                # Only select when real banter will actually be generated: the
                # canned/impossible no-LLM branch below never consumes a fact, and
                # selecting there would advance rotation/counters for a cue that
                # never airs.
                if use_directed_home_context and home_context_director is not None and _sw.has_script_llm(config):
                    try:
                        prompt_fact = home_context_director.select(lane="casual")
                    except Exception:
                        logger.debug("Home context director selection failed", exc_info=True)

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
                            audio_path = await _synthesize_impossible_moment(
                                line, config, state, _adjacent_music_source(state)
                            )
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
                                audio_path = await _synthesize_impossible_moment(
                                    line, config, state, _adjacent_music_source(state)
                                )
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
                            with _timed_render_stage(state, "tts"):
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
                                    prompt_fact=prompt_fact,
                                    use_directed_home_context=use_directed_home_context,
                                )
                                (
                                    (trans_host, trans_text, trans_track_ref),
                                    (
                                        lines,
                                        listener_request_commit,
                                    ),
                                ) = await asyncio.gather(transition_task, banter_task)
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
                                _music_src=_adjacent_music_source(state),
                            ):
                                with _timed_render_stage(state, "tts"):
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
                                with _timed_render_stage(state, "mix"):
                                    result = await _try_crossfade(_path, config, xfade_out, _music_src)
                                return result, result == xfade_out

                            async def _do_dialogue(_lines=lines, _tmp_dir=config.tmp_dir) -> Path:
                                with _timed_render_stage(state, "tts"):
                                    return await synthesize_dialogue(_lines, _tmp_dir, state=state)

                            banter_path: Path
                            (trans_voice_path, has_music_tail), banter_path = await asyncio.gather(
                                _do_transition(),
                                _do_dialogue(),
                            )

                            # Concat: transition + banter (both pre-normalized)
                            audio_path = config.tmp_dir / f"banter_full_{uuid4().hex[:8]}.mp3"
                            loop = asyncio.get_running_loop()
                            try:
                                with _timed_render_stage(state, "mix"):
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
                                # This canned clip never carries the directive/gag on
                                # air (attach_moment_ids below will be False) — demote
                                # both receipts now, not just on a successful queue.
                                # Otherwise a segment that later hits the stale/chaos-
                                # cutover discard gate (which reads segment.metadata,
                                # already stripped of these ids for a canned clip)
                                # leaves the row "elected" until restart/7-day prune.
                                _drop_unqueued_banter_receipts("canned_fallback", "chaos-canned-fallback")
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
                                state.finish_render_timing("failed", reason="render_failure")
                                continue
                        else:
                            logger.warning("Banter TTS failed, skipping segment: %s", exc)
                            _abandon_release_beat_commit(state, listener_request_commit)
                            _drop_unqueued_banter_receipts("generation_failed", "tts-failure")
                            # Commit-free net: on a transition+banter gather failure
                            # the tuple never unpacks, so listener_request_commit is
                            # None and the abandon above is a no-op. Restore any
                            # begun-but-unqueued beat by ledger status.
                            _release_campaign_abandon_in_flight(state)
                            state.finish_render_timing("failed", reason="render_failure")
                            continue

                if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
                    try:
                        expected_min_duration_sec = None if canned else banter_expected_min_duration_sec
                        expected_line_count = None if canned else banter_expected_line_count
                        with _timed_render_stage(state, "quality"):
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
                            _abandon_release_beat_commit(state, listener_request_commit)
                            _record_generated_waste(
                                state,
                                SegmentType.BANTER,
                                audio_path,
                                GenerationWasteReason.QUALITY_GATE_REJECT,
                                # Probe the real rendered length so speech waste is
                                # counted like music waste (which passes a duration).
                                # Without this, banter rejects record 0.0s and the
                                # duration-based "discarding often" gate never sees
                                # them. Best-effort helper: returns 0.0, never raises.
                                duration_sec=await loop.run_in_executor(None, _probe_segment_duration, audio_path),
                            )
                            audio_path.unlink(missing_ok=True)
                        fallback_canned = _pick_canned_clip("banter", state=state)
                        if fallback_canned:
                            try:
                                with _timed_render_stage(state, "quality"):
                                    await loop.run_in_executor(
                                        None, validate_segment_audio, fallback_canned, SegmentType.BANTER
                                    )
                                logger.info(
                                    "Using canned banter fallback after quality reject: %s", fallback_canned.name
                                )
                                audio_path = fallback_canned
                                canned = fallback_canned
                                trans_track_ref = None
                                # Same as the chaos-exception fallback above: this canned
                                # clip carries neither receipt on air, so demote both now
                                # rather than leaving the gag id to leak into a later
                                # discard/enqueue-failure that can't recover it from
                                # segment.metadata (already stripped for a canned clip).
                                _drop_unqueued_banter_receipts("canned_fallback", "quality-canned-fallback")
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
                                _drop_unqueued_banter_receipts("generation_failed", "fallback-quality-reject")
                                state.finish_render_timing(
                                    "discarded", reason=GenerationWasteReason.QUALITY_GATE_REJECT
                                )
                                continue
                        else:
                            _drop_unqueued_banter_receipts("generation_failed", "quality-reject")
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
                            state.finish_render_timing("discarded", reason=GenerationWasteReason.QUALITY_GATE_REJECT)
                            continue

                if canned is None:
                    try:
                        audio_path = await _apply_talk_bed(
                            audio_path,
                            config,
                            state,
                            prefix="banter",
                            source_track=_adjacent_music_source(state),
                        )
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
                                with _timed_render_stage(state, "mix"):
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

                banter_commit = listener_request_commit
                release_beat_metadata = {}
                memory_extraction_metadata = {}
                if canned is None and not impossible_tts:
                    release_beat_metadata = _release_beat_metadata_from_commit(banter_commit)
                    memory_extraction_metadata = _memory_extraction_metadata_from_commit(
                        banter_commit,
                        state.last_banter_script,
                    )
                ritual_moment_id = state.last_banter_ritual_moment_id or ""
                gag_moment_id = state.ha_running_gag_moment_id or ""
                attach_moment_ids = canned is None and not impossible_tts
                home_fact = state.last_banter_home_fact if attach_moment_ids else None
                home_fact_metadata: dict[str, str | int] = home_fact.segment_metadata() if home_fact is not None else {}
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
                        "transition_track_ref": trans_track_ref,
                        "ledger_segment_id": _banter_attempt_id or None,
                        # Moment Receipt ids (opaque; safe to cross public payload
                        # boundaries). Generated banter only — canned fallbacks and
                        # pre-rendered impossible-moment clips never carried the
                        # directive or the gag on air. ONLY the scriptwriter's
                        # handoff slot is read (both lanes set it at directive
                        # inclusion; the stock-copy except path clears it) — never
                        # live state, which survives a stock-copy fallback and
                        # would mint a false aired receipt.
                        "ritual_moment_id": ritual_moment_id if attach_moment_ids and ritual_moment_id else None,
                        "gag_moment_id": gag_moment_id if attach_moment_ids and gag_moment_id else None,
                        **home_fact_metadata,
                        **release_beat_metadata,
                        **memory_extraction_metadata,
                    },
                    ephemeral=canned is None,
                )
                # The handoff slot is single-use: consumed into this segment's
                # metadata (or intentionally not, for canned fallbacks — the
                # elected row then simply never airs and ages out honestly).
                state.last_banter_ritual_moment_id = ""
                state.last_banter_home_fact = None

                def _banter_callback(
                    *,
                    _is_new_listener=_is_new_listener,
                    _new_listener_count=_new_listener_count,
                    _listener_request_commit=listener_request_commit,
                    _used_generated_banter=(canned is None and not impossible_tts),
                    _first_home_context_moment_pending=first_home_context_moment_pending,
                    _gag_key=state.ha_running_gag_key,
                    _ritual_moment_id=ritual_moment_id,
                    _ritual_moment_attached=bool(attach_moment_ids and ritual_moment_id),
                    _gag_moment_id=gag_moment_id,
                    _gag_moment_attached=bool(attach_moment_ids and gag_moment_id),
                    _ledger=state.evening_ledger,
                    _cache_dir=config.cache_dir,
                    _pending_gag=state.pending_verbal_gag,
                    _vledger=state.verbal_gag_ledger,
                    _segment=segment,
                ) -> None:
                    state.after_banter()
                    if _is_new_listener:
                        state.new_listeners_pending = max(0, state.new_listeners_pending - _new_listener_count)
                    if _used_generated_banter and _listener_request_commit is not None:
                        _listener_request_commit.apply(
                            state,
                            config,
                            queue_id=str(_segment.metadata.get("queue_id") or ""),
                        )
                    if (
                        _used_generated_banter
                        and _first_home_context_moment_pending
                        and state.ha_pending_directive != FIRST_HOME_CONTEXT_MOMENT_DIRECTIVE
                    ):
                        state.ha_first_home_context_moment_fired = True
                    # Spend the running-gag cooldown only when generated banter
                    # (which carried the gag) actually airs — not on canned or
                    # failed-LLM fallbacks. Honors EveningLedger.offer_gag's contract.
                    if _used_generated_banter and _ledger is not None and _gag_key:
                        _ledger.mark_spoken(_gag_key, now=time.time())
                        _ledger.save_if_dirty(_cache_dir)
                    elif _gag_moment_id and not _gag_moment_attached:
                        # The gag never rode this banter (canned clip or
                        # pre-rendered moment aired instead) — demote its
                        # receipt honestly; offer_gag can re-elect it later.
                        _mark_moment_dropped(state, _gag_moment_id, "canned_fallback", "gag-canned-fallback")
                    if _ritual_moment_id and not _ritual_moment_attached:
                        _mark_moment_dropped(
                            state,
                            _ritual_moment_id,
                            "generation_failed",
                            "ritual-canned-fallback",
                        )
                    state.ha_running_gag_key = ""
                    state.ha_running_gag_moment_id = ""
                    # Commit the banter-seeded verbal gag to the cross-domain
                    # ledger ONLY now that the banter actually queued (B-i). A
                    # discarded banter never reaches this callback, so it never
                    # plants a travelable gag whose setup the listener never heard.
                    if _pending_gag and _vledger is not None:
                        _vledger.add_gag(
                            _pending_gag.get("text", ""),
                            punch=_pending_gag.get("punch"),
                            now=time.time(),
                        )
                    state.pending_verbal_gag = None

                success_callback = _banter_callback

            elif seg_type == SegmentType.NEWS_FLASH:
                logger.info("Producing NEWS FLASH")

                try:
                    state.set_gen("writing", "news_flash", "Writing a news flash")
                    # Callback Director: offer at most one cross-domain verbal gag
                    # (best-effort, never raises into audio). contrasting_to is the
                    # segment domain — a host-seeded gag is eligible here.
                    state.pending_callback_landed = False
                    _cb_gag = None
                    if state.verbal_gag_ledger is not None:
                        try:
                            _cb_gag = state.verbal_gag_ledger.offer(now=time.time(), contrasting_to="news_flash")
                        except Exception:
                            _cb_gag = None
                    _gen_ok = False
                    try:
                        host, text, category = await _sw.write_news_flash(
                            state, config, callback_gag=(_cb_gag[1].text if _cb_gag else None)
                        )
                        _gen_ok = True
                    finally:
                        state.end_gen(ok=_gen_ok)
                    flash_path = config.tmp_dir / f"flash_{uuid4().hex[:8]}.mp3"

                    # Keep news flashes intelligible; only traffic gets a small urgency nudge.
                    flash_rate: str | None = None
                    if category == "traffic":
                        flash_rate = "+10%"

                    with _timed_render_stage(state, "tts"):
                        await synthesize(
                            text,
                            host.voice,
                            flash_path,
                            rate=flash_rate,
                            engine=host.engine,
                            edge_fallback_voice=host.edge_fallback_voice,
                            state=state,
                        )

                    # Overlay on the tail of the last music segment — but only when a
                    # song aired immediately before this flash (else it bleeds stale).
                    flash_music = _adjacent_music_source(state)
                    crossfade_out = config.tmp_dir / f"flash_transition_{uuid4().hex[:8]}.mp3"
                    with _timed_render_stage(state, "mix"):
                        audio_path = await _try_crossfade(
                            flash_path, config, crossfade_out, flash_music, tail_seconds=6.0
                        )
                    has_music_tail = audio_path == crossfade_out
                    if audio_path is flash_path:
                        # No adjacent song / crossfade failed — bundled or synthetic bed,
                        # never the stale track.
                        try:
                            audio_path = await _apply_talk_bed(
                                audio_path, config, state, prefix="news", source_track=flash_music
                            )
                        except Exception as exc:
                            logger.warning("Talk bed generation failed, using dry news flash: %s", exc)

                    state.last_banter_script = [{"host": host.name, "text": text, "type": "news_flash"}]
                except Exception as exc:
                    logger.warning("News flash TTS failed, skipping: %s", exc)
                    state.finish_render_timing("failed", reason="render_failure")
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

                def _news_callback(_c=_bound_cat, _gag=_cb_gag, _vledger=state.verbal_gag_ledger) -> None:
                    state.after_news_flash(_c)
                    # Retire the verbal gag ONLY if the model reported it landed
                    # (queue-time != used) — a discarded segment never reaches here,
                    # and an ignored callback must not burn the gag.
                    if _gag is not None and _vledger is not None and state.pending_callback_landed:
                        _vledger.mark_spoken(_gag[0], now=time.time())

                success_callback = _news_callback

            elif seg_type == SegmentType.STATION_ID:
                logger.info("Producing STATION ID")
                sb = config.sonic_brand
                # Use full ident text, or fall back to station name
                ident_text = sb.full_ident or config.display_station_name

                try:
                    # Generate voice tag + musical sting in parallel
                    voice_path = config.tmp_dir / f"stid_voice_{uuid4().hex[:8]}.mp3"
                    sting_path = config.tmp_dir / f"stid_sting_{uuid4().hex[:8]}.mp3"

                    # Use configured sweeper voice, or a random host
                    sweeper_voice = sb.sweeper_voice
                    sweeper_engine = sb.sweeper_engine
                    sweeper_fallback = sb.sweeper_edge_fallback_voice
                    if not sweeper_voice:
                        sweeper_host = random.choice(_sw._regular_hosts(config))
                        sweeper_voice = sweeper_host.voice
                        sweeper_engine = sweeper_host.engine
                        sweeper_fallback = sweeper_host.edge_fallback_voice
                    loop = asyncio.get_running_loop()

                    async def _build_station_voice(
                        _text=ident_text,
                        _voice=sweeper_voice,
                        _path=voice_path,
                        _engine=sweeper_engine,
                        _fallback=sweeper_fallback,
                    ) -> None:
                        with _timed_render_stage(state, "tts"):
                            await synthesize(
                                _text,
                                _voice,
                                _path,
                                engine=_engine,
                                edge_fallback_voice=_fallback,
                                state=state,
                            )

                    async def _build_station_sting(
                        _loop=loop,
                        _path=sting_path,
                        _notes=sb.motif_notes,
                    ) -> None:
                        with _timed_render_stage(state, "mix"):
                            await _loop.run_in_executor(
                                None,
                                generate_station_id_bed,
                                _path,
                                3.0,
                                _notes,
                            )

                    await asyncio.gather(_build_station_voice(), _build_station_sting())

                    # Mix voice over sting
                    audio_path = config.tmp_dir / f"stid_{uuid4().hex[:8]}.mp3"
                    with _timed_render_stage(state, "mix"):
                        await loop.run_in_executor(None, mix_voice_with_sting, voice_path, sting_path, audio_path)
                    voice_path.unlink(missing_ok=True)
                    sting_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("Station ID generation failed: %s", exc)
                    state.finish_render_timing("failed", reason="render_failure")
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
                    sweeper_text = random.choice(sb.sweepers) if sb.sweepers else config.display_station_name
                    audio_path = await _render_sweeper_audio(sweeper_text, config, state, prefix="sweeper")
                except Exception as exc:
                    logger.warning("Sweeper generation failed: %s", exc)
                    state.finish_render_timing("failed", reason="render_failure")
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
                station_name = config.display_station_name
                # Italian grammar: "È l'una" for 1:00/13:00, "Sono le N" otherwise
                hour_str = "È l'una" if hour in (1, 13) else f"Sono le {hour}"
                if minute == 0:
                    time_text = f"{hour_str} su {station_name}."
                else:
                    time_text = f"{hour_str} e {minute} su {station_name}."

                try:
                    voice_path = config.tmp_dir / f"time_voice_{uuid4().hex[:8]}.mp3"
                    chime_path = config.tmp_dir / f"time_chime_{uuid4().hex[:8]}.mp3"
                    host = random.choice(_sw._regular_hosts(config))
                    loop = asyncio.get_running_loop()

                    # Voice + chime run in parallel, but retain their distinct
                    # diagnostic ownership: provider voice vs local imaging.
                    async def _build_time_voice(
                        _text=time_text,
                        _host=host,
                        _path=voice_path,
                    ) -> None:
                        with _timed_render_stage(state, "tts"):
                            await synthesize(
                                _text,
                                _host.voice,
                                _path,
                                engine=_host.engine,
                                edge_fallback_voice=_host.edge_fallback_voice,
                                state=state,
                            )

                    async def _build_time_chime(_loop=loop, _path=chime_path) -> None:
                        with _timed_render_stage(state, "mix"):
                            await _loop.run_in_executor(None, generate_tone, _path, 1047, 0.3)

                    await asyncio.gather(_build_time_voice(), _build_time_chime())
                    audio_path = config.tmp_dir / f"time_{uuid4().hex[:8]}.mp3"
                    with _timed_render_stage(state, "mix"):
                        await loop.run_in_executor(None, concat_files, [chime_path, voice_path], audio_path, 200, False)
                    chime_path.unlink(missing_ok=True)
                    voice_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("Time check generation failed: %s", exc)
                    state.finish_render_timing("failed", reason="render_failure")
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
                    state.finish_render_timing("discarded", reason="no_ad_brands")
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
                    voice_map = _cast_voices(brand, config.ads.voices, _sw._regular_hosts(config), roles_needed)
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

                async def _build_intro(_music_src=_adjacent_music_source(state)):
                    """Intro: transition LLM → TTS → crossfade + promo tag."""
                    parts = []
                    try:
                        with _timed_render_stage(state, "script"):
                            ihost, itext, itrack_ref = await _sw.write_transition(state, config, next_segment="ad")
                    except Exception:
                        ihost = random.choice(_sw._regular_hosts(config))
                        itext = random.choice(_sw.AD_BREAK_INTROS)
                        itrack_ref = None
                    ipath = config.tmp_dir / f"ad_intro_{uuid4().hex[:8]}.mp3"
                    with _timed_render_stage(state, "tts"):
                        await synthesize(
                            itext,
                            ihost.voice,
                            ipath,
                            engine=ihost.engine,
                            edge_fallback_voice=ihost.edge_fallback_voice,
                            state=state,
                        )
                    xout = config.tmp_dir / f"ad_trans_{uuid4().hex[:8]}.mp3"
                    with _timed_render_stage(state, "mix"):
                        ipath = await _try_crossfade(ipath, config, xout, _music_src)
                    has_music_tail = ipath == xout
                    parts.append(ipath)
                    # Promo compliance tag
                    try:
                        ppath = config.tmp_dir / f"promo_tag_{uuid4().hex[:8]}.mp3"
                        if config.ads.voices:
                            promo_voice = config.ads.voices[0]
                            pvoice = promo_voice.voice
                            pengine = promo_voice.engine
                            pfallback = promo_voice.edge_fallback_voice
                        else:
                            pvoice = ihost.voice
                            pengine = ihost.engine
                            pfallback = ihost.edge_fallback_voice
                        with _timed_render_stage(state, "tts"):
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
                    return parts, itext, has_music_tail, itrack_ref

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
                    with _timed_render_stage(state, "mix"):
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
                # Use the brand NAME, never the AdBrand object — f-stringing the
                # object leaks its repr ("AdBrand(name='Gelato Infinito', ...)")
                # into the admin In-Produzione feed (machine words on a human screen).
                _ad_brand = spot_params[0][0].name if spot_params else ""
                state.set_gen(
                    "writing",
                    "ad",
                    f"Writing the {_ad_brand} spot" if _ad_brand else "Writing an ad break",
                    track_timing=False,
                )
                # Callback Director: offer one cross-domain verbal gag for the
                # break, handed to the FIRST spot only (at most one callback per
                # break). Best-effort; never raises into audio.
                state.pending_callback_landed = False
                _cb_gag = None
                if state.verbal_gag_ledger is not None:
                    try:
                        _cb_gag = state.verbal_gag_ledger.offer(now=time.time(), contrasting_to="ad")
                    except Exception:
                        _cb_gag = None
                _cb_gag_text = _cb_gag[1].text if _cb_gag else None

                async def _write_ad_scripts(
                    _spot_params=tuple(spot_params),
                    _callback_gag_text=_cb_gag_text,
                ):
                    with _timed_render_stage(state, "script"):
                        return await asyncio.gather(
                            *(
                                _sw.write_ad(
                                    brand,
                                    vm,
                                    state,
                                    config,
                                    ad_format=af,
                                    sonic=sn,
                                    spot_index=i,
                                    callback_gag=(_callback_gag_text if i == 0 else None),
                                )
                                for i, (brand, af, sn, vm) in enumerate(_spot_params)
                            )
                        )

                _gen_ok = False
                try:
                    (
                        (intro_parts, intro_text, intro_has_music_tail, intro_track_ref),
                        scripts,
                        (bumper_in, mid_bumpers),
                    ) = await asyncio.gather(
                        _build_intro(),
                        _write_ad_scripts(),
                        _build_bumpers(),
                    )
                    _gen_ok = True
                finally:
                    reset_collector(_ad_prov_tok)
                    state.end_gen(ok=_gen_ok)

                # ── PHASE 2: Fan out all ad TTS synthesis in parallel ──
                with _timed_render_stage(state, "tts"):
                    ad_paths = await asyncio.gather(
                        *(
                            synthesize_ad(
                                script,
                                vm,
                                config.tmp_dir,
                                sfx_dir,
                                state=state,
                                cache_dir=config.cache_dir,
                            )
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
                outro_host = random.choice(_sw._regular_hosts(config))
                outro_path = config.tmp_dir / f"ad_outro_{uuid4().hex[:8]}.mp3"
                outro_text = random.choice(_sw.AD_BREAK_OUTROS)

                async def _build_closing_bumper(_loop=loop, _path=bumper_out) -> None:
                    with _timed_render_stage(state, "mix"):
                        await _loop.run_in_executor(None, generate_bumper_jingle, _path)

                async def _build_outro_voice(
                    _text=outro_text,
                    _host=outro_host,
                    _path=outro_path,
                ) -> None:
                    with _timed_render_stage(state, "tts"):
                        await synthesize(
                            _text,
                            _host.voice,
                            _path,
                            engine=_host.engine,
                            edge_fallback_voice=_host.edge_fallback_voice,
                            state=state,
                        )

                await asyncio.gather(_build_closing_bumper(), _build_outro_voice())
                break_parts.append(bumper_out)
                break_parts.append(outro_path)

                # ── PHASE 5: Final concat (skip loudnorm — all parts pre-normalized) ──
                if len(break_parts) == 1:
                    ad_break_path = break_parts[0]
                else:
                    ad_break_path = config.tmp_dir / f"adbreak_{uuid4().hex[:8]}.mp3"
                    try:
                        with _timed_render_stage(state, "mix"):
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
                        with _timed_render_stage(state, "quality"):
                            await loop.run_in_executor(None, validate_segment_audio, ad_break_path, SegmentType.AD)
                    except AudioToolError as exc:
                        logger.warning("Audio tool unavailable, skipping ad quality check: %s", exc)
                    except AudioQualityError as exc:
                        logger.warning("Quality gate rejected ad break (%s): %s", ad_break_path.name, exc)
                        _record_generated_waste(
                            state,
                            SegmentType.AD,
                            ad_break_path,
                            GenerationWasteReason.QUALITY_GATE_REJECT,
                            # Probe the real rendered length so ad-break waste is
                            # counted like music waste; best-effort, returns 0.0.
                            duration_sec=await loop.run_in_executor(None, _probe_segment_duration, ad_break_path),
                        )
                        ad_break_path.unlink(missing_ok=True)
                        # Prevent scheduler lock on AD if we reject a full break.
                        state.songs_since_ad = 0
                        state.finish_render_timing("discarded", reason=GenerationWasteReason.QUALITY_GATE_REJECT)
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
                        "transition_track_ref": intro_track_ref,
                        "ledger_segment_id": _ad_attempt_id,
                    },
                )
                _bound_brands = break_brands

                def _ad_callback(_b=_bound_brands, _gag=_cb_gag, _vledger=state.verbal_gag_ledger) -> None:
                    state.after_ad(brands=_b)
                    # Retire the verbal gag ONLY if the model reported it landed
                    # (queue-time != used) — a discarded break never reaches here.
                    if _gag is not None and _vledger is not None and state.pending_callback_landed:
                        _vledger.mark_spoken(_gag[0], now=time.time())

                success_callback = _ad_callback

        except Exception as e:
            # Recoverable: network/ffmpeg/disk/httpx errors — use non-silent continuity audio.
            logger.error("Failed to produce %s segment: %s", seg_type.value, e)
            state.finish_render_timing("failed", reason="render_failure")
            # Commit-free: banter_commit may still be None here (e.g. a sibling
            # task raised inside the transition+banter gather before the tuple
            # unpacked), so restore any begun-but-unqueued beat by ledger status.
            _release_campaign_abandon_in_flight(state)
            state.failed_segments += 1
            # Backoff on persistent failures to avoid CPU-burning tight loop
            consecutive = state.failed_segments
            if consecutive > 1:
                post_failure_backoff = min(30.0, 2.0 ** min(consecutive, 5))
                logger.warning(
                    "Consecutive failures: %d — backing off %.0fs after recovery audio queues",
                    consecutive,
                    post_failure_backoff,
                )
            segment = await _producer_error_recovery_segment(state, config)
            if segment is None:
                await asyncio.sleep(0.5)
                await _sleep_post_failure_backoff(post_failure_backoff)
                continue
            # Do NOT advance state counters — failed segment doesn't count

        if segment:
            actual_seg_type = _adjacency_type_for(segment)
            if (
                prev_seg_type is not None
                and actual_seg_type is not None
                and _crosses_music_speech_boundary(prev_seg_type, actual_seg_type)
                and not segment.metadata.get("has_music_tail")
                and not segment.metadata.get("rescue")
            ):
                try:
                    loop = asyncio.get_running_loop()
                    sting_path = config.tmp_dir / f"transition_{uuid4().hex[:8]}.mp3"
                    imaging_lib = _make_imaging_lib(config)
                    with _timed_render_stage(state, "mix"):
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
                    if pre_sting_ephemeral and not _is_packaged_asset(pre_sting_path):
                        pre_sting_path.unlink(missing_ok=True)
                    segment = replace(segment, path=merged_path, ephemeral=True)
                except Exception as exc:
                    logger.warning("Transition sting generation failed, using clean cut: %s", exc)
            segment.duration_sec = await asyncio.to_thread(_probe_segment_duration, segment.path)
            if generation_revision != state.playlist_revision:
                if generation_source_revision != state.source_revision:
                    logger.info("Discarding stale %s segment after playlist source switch", seg_type.value)
                    stale_reason = GenerationWasteReason.STALE_SOURCE
                else:
                    logger.info("Discarding stale %s segment after same-source playlist edit", seg_type.value)
                    stale_reason = GenerationWasteReason.STALE_PLAYLIST
                state.record_discard(segment, reason=stale_reason)
                _drop_segment_moment_receipts(state, segment, str(stale_reason), "stale-discard")
                _abandon_release_beat_commit(state, banter_commit)
                _unlink_if_tmp_render(segment, config.tmp_dir)
                state.finish_render_timing("discarded", reason=stale_reason)
                if is_operator_forced:
                    state.operator_force_pending = None  # render abandoned — let the operator retry
                await _sleep_post_failure_backoff(post_failure_backoff)
                continue
            if generation_chaos_epoch != state.chaos_cutover_epoch:
                logger.info("Discarding stale %s segment after chaos cutover", seg_type.value)
                state.record_discard(segment, reason=GenerationWasteReason.STALE_CHAOS)
                _drop_segment_moment_receipts(state, segment, GenerationWasteReason.STALE_CHAOS, "chaos-discard")
                _abandon_release_beat_commit(state, banter_commit)
                _unlink_if_tmp_render(segment, config.tmp_dir)
                state.finish_render_timing("discarded", reason=GenerationWasteReason.STALE_CHAOS)
                if is_operator_forced:
                    state.operator_force_pending = None  # render abandoned — let the operator retry
                await _sleep_post_failure_backoff(post_failure_backoff)
                continue
            if generation_continuity_epoch != state.continuity_epoch:
                logger.info("Discarding stale %s segment after a live continuity reservation", seg_type.value)
                state.record_discard(segment, reason=GenerationWasteReason.STALE_CONTINUITY)
                _drop_segment_moment_receipts(
                    state, segment, GenerationWasteReason.STALE_CONTINUITY, "continuity-discard"
                )
                _abandon_release_beat_commit(state, banter_commit)
                _unlink_if_tmp_render(segment, config.tmp_dir)
                state.finish_render_timing("discarded", reason=GenerationWasteReason.STALE_CONTINUITY)
                if is_operator_forced:
                    state.operator_force_pending = None
                await _sleep_post_failure_backoff(post_failure_backoff)
                continue
            if not _home_fact_policy_is_current(segment):
                logger.info("Discarding stale banter after Home Context policy change")
                state.record_discard(segment, reason=GenerationWasteReason.OPERATOR_PURGE)
                _drop_segment_moment_receipts(state, segment, GenerationWasteReason.OPERATOR_PURGE, "home-fact-policy")
                _abandon_release_beat_commit(state, banter_commit)
                _unlink_if_tmp_render(segment, config.tmp_dir)
                await _sleep_post_failure_backoff(post_failure_backoff)
                continue
            # Stable per-segment id: the shared queue publication helper stamps
            # this on both the audio and its Scaletta row before admission.
            shadow_entry = _queue_shadow_entry(segment)
            # Reserve the ambient home fact (if any) BEFORE admission. A rejected
            # reservation means the topic is already queued ahead or resting on
            # cooldown, so airing this segment would double the same cue on-air —
            # drop it here instead of after it is already in the queue. This reads
            # only segment metadata, so it is a safe no-op on non-home segments.
            _seg_metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
            _home_fact_id = str(_seg_metadata.get("home_fact_id") or "")
            _home_fact_director = state.home_context_director
            _home_fact_queue_id = str(_seg_metadata.get("queue_id") or "")
            if (
                _home_fact_id
                and _home_fact_director is not None
                and not _home_fact_director.reserve_by_id(_home_fact_queue_id, _home_fact_id)
            ):
                logger.info("Discarding banter: home fact reservation rejected (topic already queued or resting)")
                state.record_discard(segment, reason=GenerationWasteReason.OPERATOR_PURGE)
                _drop_segment_moment_receipts(
                    state, segment, GenerationWasteReason.OPERATOR_PURGE, "home-fact-reserve-rejected"
                )
                _abandon_release_beat_commit(state, banter_commit)
                _unlink_if_tmp_render(segment, config.tmp_dir)
                await _sleep_post_failure_backoff(post_failure_backoff)
                continue
            if is_operator_forced:
                # Air-next: front-insert past the buffered lookahead so the operator
                # hears their pick at the next boundary, never minutes later.
                def _front_insert_stale_check(_segment: Segment = segment) -> bool:
                    # Stale if the continuity/source/playlist/chaos gate fires OR
                    # the home-fact policy changed for this segment.
                    if bool(_enqueue_stale_reason()):
                        return True
                    return not _home_fact_policy_is_current(_segment)

                if not await _enqueue_with_egress(
                    queue,
                    state,
                    config,
                    segment,
                    front_insert=True,
                    shadow_entry=shadow_entry,
                    stale_check=_front_insert_stale_check,
                ):
                    if _home_fact_id and _home_fact_director is not None:
                        _home_fact_director.release(_home_fact_queue_id, fact_id=_home_fact_id or None)
                    _drop_segment_moment_receipts(state, segment, "generation_failed", "front-insert-failed")
                    _abandon_release_beat_commit(state, banter_commit)
                    state.operator_force_pending = None
                    state.finish_render_timing(
                        "discarded",
                        reason=_enqueue_rejection_reason(state, segment, _enqueue_stale_reason)
                        or GenerationWasteReason.AIR_NEXT_OVERFLOW,
                    )
                    await _sleep_post_failure_backoff(post_failure_backoff)
                    continue
                # Queue-tail adjacency lives in _remember_enqueued; this head-order value drives stings.
                prev_seg_type = _adjacency_type_for(segment)
            else:
                if not await _queue_segment(
                    segment,
                    shadow_entry=shadow_entry,
                    stale_check=_enqueue_stale_reason,
                ):
                    if _home_fact_id and _home_fact_director is not None:
                        _home_fact_director.release(_home_fact_queue_id, fact_id=_home_fact_id or None)
                    _drop_segment_moment_receipts(state, segment, "generation_failed", "enqueue-failed")
                    _abandon_release_beat_commit(state, banter_commit)
                    state.finish_render_timing(
                        "discarded",
                        reason=_enqueue_rejection_reason(state, segment, _enqueue_stale_reason)
                        or GenerationWasteReason.EGRESS_STALE,
                    )
                    await _sleep_post_failure_backoff(post_failure_backoff)
                    continue
            if chaos_subtype is not None and state.chaos_pending == chaos_subtype:
                state.chaos_pending = None
            if chaos_subtype == ChaosSubtype.URGENT_INTERRUPT:
                # An interrupt banter that fell back to stock copy queued WITHOUT
                # its receipt id (the scriptwriter's except path cleared the
                # handoff) — and the directive is consumed right here, so there
                # is no retry. Demote the elected row honestly instead of leaving
                # it "waiting for its break" until retention ages it out.
                if (
                    state.ha_pending_directive_moment_id
                    and not segment.metadata.get("ritual_moment_id")
                    and state.moment_store is not None
                ):
                    _mark_moment_dropped(
                        state,
                        state.ha_pending_directive_moment_id,
                        "generation_failed",
                        "interrupt-stock-copy",
                    )
                state.ha_pending_directive = ""
                state.ha_pending_directive_moment_id = ""
                state.ha_pending_directive_source = ""
                # The safety-belt force_next was set when the interrupt fired.
                # chaos_pending already produced the banter; clearing here
                # prevents the producer from queueing an extra banter next cycle.
                if state.force_next == SegmentType.BANTER:
                    state.force_next = None
            _segments_produced += 1
            # Queue appended → up_next changed → integration consumers polling
            # ``changed_at`` need to see this even without a segment transition.
            state.last_state_change_at = time.time()
            if "error" not in segment.metadata and not segment.metadata.get("rescue"):
                if success_callback:
                    success_callback()
                state.failed_segments = 0  # Reset backoff on success
                _drain_guard_queued = False  # Real segment landed — allow drain guard to fire again if needed
                # #144/#146: Launch background normalization of the predicted next music track.
                # By the time the current track finishes playing (~3-4 min), the next norm
                # is already cached — avoids the 75-second Pi stall when the queue drains.
                # Let a running prefetch finish instead of cancel-and-replace:
                # cancelling can't stop its in-flight executor ffmpeg, which keeps
                # holding the background admission slot — a replacement would only
                # park another shared executor thread behind it. The next music
                # segment retries with a fresh candidate.
                if (
                    segment.type == SegmentType.MUSIC
                    and state.force_next is None
                    and state.playlist
                    and (_prefetch_task is None or _prefetch_task.done())
                ):
                    _prefetch_task = asyncio.create_task(
                        _prefetch_next(state, config, _prefetch_failed_keys),
                        name="prefetch-norm",
                    )
            logger.info(
                "Queued %s in %.1fs (queue size: %d)",
                segment.type.value,
                time.monotonic() - _t_render,
                queue.qsize(),
            )
            state.finish_render_timing("produced")
            await _sleep_post_failure_backoff(post_failure_backoff)
