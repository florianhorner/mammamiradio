"""Segment production pipeline for music, banter, and ad breaks."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import random
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from mammamiradio.audio_quality import AudioQualityError, AudioToolError, validate_segment_audio
from mammamiradio.config import StationConfig
from mammamiradio.context_cues import generate_impossible_line
from mammamiradio.downloader import download_track, evict_cache_lru
from mammamiradio.ha_context import HomeContext, fetch_home_context
from mammamiradio.models import (
    AdBrand,
    AdFormat,
    AdHistoryEntry,
    AdVoice,
    Segment,
    SegmentType,
    SonicWorld,
    StationState,
)
from mammamiradio.normalizer import (
    concat_files,
    crossfade_voice_over_music,
    generate_bumper_jingle,
    generate_silence,
    generate_station_id_bed,
    generate_tone,
    mix_voice_with_sting,
    normalize,
)
from mammamiradio.scheduler import next_segment_type
from mammamiradio.scriptwriter import (
    AD_BREAK_INTROS,
    AD_BREAK_OUTROS,
    _has_script_llm,
    write_ad,
    write_banter,
    write_news_flash,
    write_transition,
)
from mammamiradio.track_rationale import classify_track_crate, generate_track_rationale
from mammamiradio.tts import synthesize, synthesize_ad, synthesize_dialogue

logger = logging.getLogger(__name__)


_background_tasks: set[asyncio.Task] = set()


async def _record_motif(state: StationState, track) -> None:
    """Record a played track as a motif in the listener persona (fire-and-forget)."""
    persona_store = getattr(state, "persona_store", None)
    if not persona_store:
        return
    try:
        await persona_store.record_motif(track.artist, track.title)
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
_DEMO_ASSETS_DIR = Path(__file__).parent / "demo_assets"


# Tracks the most recent music file to avoid repeated glob scans on every banter.
_last_music_file: Path | None = None


def _set_last_music_file(path: Path) -> None:
    """Update the cached last music file (called after each music segment)."""
    global _last_music_file
    _last_music_file = path


def _latest_music_file(tmp_dir: Path) -> Path | None:
    """Return the most recently written music_*.mp3, using cached path when available."""
    if _last_music_file and _last_music_file.exists():
        return _last_music_file
    # Fallback: scan directory (only on first call or after cache invalidation)
    files = list(tmp_dir.glob("music_*.mp3"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


async def _try_crossfade(
    voice_path: Path,
    config: StationConfig,
    output_path: Path,
    tail_seconds: float = 8.0,
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
        )
        voice_path.unlink(missing_ok=True)
        logger.info("Crossfade over %s", last_music.name)
        return output_path
    except Exception as exc:
        logger.warning("Crossfade failed, using standalone: %s", exc)
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
    """Pick a pre-bundled clip from demo_assets/{subdir}/, avoiding recent repeats.

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


def _pick_brand(brands: list[AdBrand], ad_history: list) -> AdBrand:
    """Pick a brand, avoiding the last 3 aired and weighting recurring brands higher."""
    recent_names = {e.brand for e in list(ad_history)[-3:]}
    eligible = [b for b in brands if b.name not in recent_names]
    if not eligible:
        eligible = list(brands)  # allow repeats if pool exhausted
    weights = [3 if b.recurring else 1 for b in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]


# Default sonic palettes by brand category. Each category gets multiple variants so
# ads can shift texture between breaks instead of sounding like one recycled bed.
_CATEGORY_SONIC: dict[str, list[SonicWorld]] = {
    "tech": [
        SonicWorld(environment="shopping_channel", music_bed="discount_techno", transition_motif="startup_synth"),
        SonicWorld(environment="showroom", music_bed="upbeat", transition_motif="whoosh"),
    ],
    "food": [
        SonicWorld(environment="cafe", music_bed="tarantella_pop", transition_motif="register_hit"),
        SonicWorld(environment="shopping_channel", music_bed="cheap_synth_romance", transition_motif="ice_clink"),
        SonicWorld(environment="cafe", music_bed="upbeat", transition_motif="mandolin_sting"),
    ],
    "fashion": [
        SonicWorld(environment="showroom", music_bed="suspicious_jazz", transition_motif="whoosh"),
        SonicWorld(environment="showroom", music_bed="discount_techno", transition_motif="tape_stop"),
    ],
    "beauty": [
        SonicWorld(environment="luxury_spa", music_bed="cheap_synth_romance", transition_motif="mandolin_sting"),
        SonicWorld(environment="showroom", music_bed="lounge", transition_motif="ice_clink"),
    ],
    "services": [
        SonicWorld(environment="motorway", music_bed="lounge", transition_motif="chime"),
        SonicWorld(environment="shopping_channel", music_bed="discount_techno", transition_motif="register_hit"),
    ],
    "finance": [
        SonicWorld(environment="", music_bed="suspicious_jazz", transition_motif="hotline_beep"),
        SonicWorld(environment="showroom", music_bed="lounge", transition_motif="ding"),
    ],
    "health": [
        SonicWorld(environment="", music_bed="lounge", transition_motif="ding"),
        SonicWorld(environment="luxury_spa", music_bed="cheap_synth_romance", transition_motif="chime"),
    ],
    "fitness": [
        SonicWorld(environment="stadium", music_bed="upbeat", transition_motif="whoosh"),
        SonicWorld(environment="motorway", music_bed="discount_techno", transition_motif="startup_synth"),
    ],
    "tourism": [
        SonicWorld(environment="beach", music_bed="tarantella_pop", transition_motif="mandolin_sting"),
        SonicWorld(environment="shopping_channel", music_bed="overblown_epic", transition_motif="whoosh"),
    ],
}

# Default roles needed per format
_FORMAT_ROLES: dict[str, list[str]] = {
    AdFormat.CLASSIC_PITCH: ["hammer"],
    AdFormat.TESTIMONIAL: ["witness", "hammer"],
    AdFormat.DUO_SCENE: ["hammer", "maniac"],
    AdFormat.LIVE_REMOTE: ["hammer"],
    AdFormat.LATE_NIGHT_WHISPER: ["seductress"],
    AdFormat.INSTITUTIONAL_PSA: ["bureaucrat"],
}

ALL_FORMATS = [f.value for f in AdFormat]


def _select_ad_creative(
    brand: AdBrand,
    state: StationState,
    config: StationConfig,
) -> tuple[str, SonicWorld, list[str]]:
    """Pick the ad format, sonic world, and needed speaker roles for this spot.

    Voice-count guard: if fewer than 2 distinct voices are available, multi-voice
    formats (duo_scene, testimonial) are excluded from candidates.
    """
    # Determine available distinct voices
    num_voices = len(config.ads.voices) if config.ads.voices else 1

    # Pick format
    if brand.campaign and brand.campaign.format_pool:
        candidates = list(brand.campaign.format_pool)
    else:
        candidates = list(ALL_FORMATS)

    # Voice-count guard: exclude multi-voice formats if < 2 voices
    if num_voices < 2:
        candidates = [f for f in candidates if AdFormat(f).voice_count < 2]
        if not candidates:
            candidates = [AdFormat.CLASSIC_PITCH]

    # Avoid last-used format for this brand
    brand_history = [e for e in state.ad_history if e.brand == brand.name]
    if brand_history:
        last_format = brand_history[-1].format
        if last_format and len(candidates) > 1:
            candidates = [f for f in candidates if f != last_format] or candidates

    ad_format = random.choice(candidates)

    # Pick sonic world
    sonic_variants = _CATEGORY_SONIC.get(brand.category, [SonicWorld()])
    if brand_history and len(sonic_variants) > 1:
        last_sonic = brand_history[-1]
        sonic_variants = [
            variant
            for variant in sonic_variants
            if not (
                variant.environment == last_sonic.environment
                and variant.music_bed == last_sonic.music_bed
                and variant.transition_motif == last_sonic.transition_motif
            )
        ] or sonic_variants
    cat_sonic = replace(random.choice(sonic_variants))

    if brand.campaign and brand.campaign.sonic_signature:
        sonic = SonicWorld(
            environment=cat_sonic.environment,
            music_bed=cat_sonic.music_bed,
            transition_motif=brand.campaign.sonic_signature.split("+")[0],
            sonic_signature=brand.campaign.sonic_signature,
        )
    else:
        sonic = cat_sonic

    # Determine needed roles
    if brand.campaign and brand.campaign.spokesperson:
        primary_role = brand.campaign.spokesperson
        default_roles = _FORMAT_ROLES.get(ad_format, ["hammer"])
        if AdFormat(ad_format).voice_count >= 2:
            # Primary is the spokesperson, secondary is the other role
            secondary = [r for r in default_roles if r != primary_role]
            roles = [primary_role] + (secondary if secondary else [default_roles[-1]])
        else:
            roles = [primary_role]
    else:
        roles = _FORMAT_ROLES.get(ad_format, ["hammer"])

    return ad_format, sonic, roles


def _cast_voices(
    brand: AdBrand,
    config: StationConfig,
    roles_needed: list[str],
) -> dict[str, AdVoice]:
    """Map needed speaker roles to actual AdVoice instances.

    Falls back to random voice from pool if no voice matches a needed role.
    """
    voices = config.ads.voices
    if not voices:
        # No voices configured, use a host as fallback
        host = random.choice(config.hosts)
        fallback = AdVoice(name=host.name, voice=host.voice, style=host.style)
        return {roles_needed[0] if roles_needed else "default": fallback}

    # Build role->voice index
    role_index: dict[str, AdVoice] = {}
    for v in voices:
        if v.role:
            role_index[v.role] = v

    result: dict[str, AdVoice] = {}
    used_voices: set[str] = set()

    for role in roles_needed:
        if role in role_index:
            result[role] = role_index[role]
            used_voices.add(role_index[role].name)
        else:
            # Fallback: pick a random voice not already used
            available = [v for v in voices if v.name not in used_voices]
            if not available:
                available = list(voices)
            pick = random.choice(available)
            result[role] = pick
            used_voices.add(pick.name)

    return result


async def run_producer(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
    skip_event: asyncio.Event | None = None,
) -> None:
    """Keep the lookahead queue filled with rendered segments for live playback."""
    logger.info("Producer started. Playlist: %d tracks", len(state.playlist))

    async def _queue_segment(segment: Segment) -> bool:
        """Queue a segment unless the operator stopped the session mid-generation."""
        if state.session_stopped:
            if segment.ephemeral:
                segment.path.unlink(missing_ok=True)
            logger.info("Discarding %s because the session is stopped", segment.type.value)
            return False
        await queue.put(segment)
        return True

    # Home Assistant context cache
    ha_cache: HomeContext | None = None

    _music_qg_rejections = 0  # consecutive music quality gate rejections (circuit breaker)
    _last_cache_eviction = 0.0  # epoch time of last eviction check
    _cache_eviction_interval = 3600  # run eviction at most once per hour

    while True:
        if state.session_stopped:
            await asyncio.sleep(1)
            continue

        if queue.qsize() >= config.pacing.lookahead_segments:
            # Periodically evict stale cache files while the producer is idle
            now = asyncio.get_running_loop().time()
            if now - _last_cache_eviction >= _cache_eviction_interval:
                _last_cache_eviction = now
                await asyncio.to_thread(evict_cache_lru, config.cache_dir, config.max_cache_size_mb)
            await asyncio.sleep(0.5)
            continue

        # Check for forced trigger first, otherwise use scheduler
        if state.force_next is not None:
            seg_type = state.force_next
            state.force_next = None
            logger.info("Forced trigger: %s", seg_type.value)
        else:
            seg_type = next_segment_type(state, config.pacing)
        segment: Segment | None = None
        generation_revision = state.playlist_revision
        success_callback: Callable[[], None] | None = None

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

        try:
            if seg_type == SegmentType.MUSIC:
                track = state.select_next_track()
                logger.info("Producing MUSIC: %s", track.display)

                norm_path = config.tmp_dir / f"music_{uuid4().hex[:8]}.mp3"

                audio_path = await download_track(track, config.cache_dir, music_dir=Path("music"))
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, normalize, audio_path, norm_path)
                audio_source = "download"

                # Quality gate: reject truncated/silent downloads before queueing.
                # Circuit breaker: after 3 consecutive rejections, let the next track
                # through so the stream doesn't starve (silence > dead air).
                if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
                    _music_loop = asyncio.get_running_loop()
                    try:
                        await _music_loop.run_in_executor(None, validate_segment_audio, norm_path, SegmentType.MUSIC)
                        _music_qg_rejections = 0
                    except AudioToolError as exc:
                        logger.warning("Audio tool unavailable, skipping music quality check: %s", exc)
                    except AudioQualityError as exc:
                        _music_qg_rejections += 1
                        if _music_qg_rejections >= 3:
                            logger.warning(
                                "Quality gate circuit breaker: %d consecutive rejections, "
                                "allowing track through to prevent stream starvation (%s: %s)",
                                _music_qg_rejections,
                                norm_path.name,
                                exc,
                            )
                            _music_qg_rejections = 0
                        else:
                            logger.warning("Quality gate rejected music track (%s): %s", norm_path.name, exc)
                            norm_path.unlink(missing_ok=True)
                            continue

                # Generate "Why this track?" rationale for listener UI
                rationale = generate_track_rationale(
                    track,
                    source=state.playlist_source,
                    listener=state.listener,
                )
                crate = classify_track_crate(track, state.playlist_source)

                segment = Segment(
                    type=SegmentType.MUSIC,
                    path=norm_path,
                    metadata={
                        "title": track.display,
                        "spotify_id": track.spotify_id,
                        "album_art": track.album_art,
                        "rationale": rationale,
                        "crate": crate,
                        "audio_source": audio_source,
                    },
                )
                _bound_track = track
                _set_last_music_file(norm_path)

                # Record track as a motif in listener persona (async, non-blocking)
                task = asyncio.create_task(_record_motif(state, track))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

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

                if not _has_script_llm(config):
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

                if canned:
                    logger.info("Using pre-bundled banter clip: %s", canned.name)
                    audio_path = canned
                    state.last_banter_script = [{"host": "Radio", "text": "(pre-recorded banter)"}]
                elif not impossible_tts:
                    try:
                        # Generate transition voice + banter in parallel
                        transition_task = write_transition(state, config, next_segment="banter")
                        banter_task = write_banter(
                            state,
                            config,
                            is_new_listener=_is_new_listener,
                            is_first_listener=_is_first_listener,
                        )
                        (trans_host, trans_text), lines = await asyncio.gather(transition_task, banter_task)

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
                            )
                            xfade_out = config.tmp_dir / f"banter_trans_{uuid4().hex[:8]}.mp3"
                            return await _try_crossfade(_path, config, xfade_out)

                        banter_path: Path
                        trans_voice_path, banter_path = await asyncio.gather(
                            _do_transition(),
                            synthesize_dialogue(lines, config.tmp_dir),
                        )

                        # Concat: transition + banter (both pre-normalized)
                        audio_path = config.tmp_dir / f"banter_full_{uuid4().hex[:8]}.mp3"
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None, concat_files, [trans_voice_path, banter_path], audio_path, 200, False
                        )
                        trans_voice_path.unlink(missing_ok=True)
                        banter_path.unlink(missing_ok=True)

                        state.recent_transition_texts.append(trans_text)
                        state.last_banter_script = [
                            {"host": trans_host.name, "text": trans_text, "type": "transition"},
                        ] + [{"host": h.name, "text": t} for h, t in lines]
                    except Exception as exc:
                        logger.warning("Banter TTS failed, skipping segment: %s", exc)
                        continue

                if not os.environ.get("MAMMAMIRADIO_SKIP_QUALITY_GATE"):
                    try:
                        await loop.run_in_executor(None, validate_segment_audio, audio_path, SegmentType.BANTER)
                    except AudioToolError as exc:
                        logger.warning("Audio tool unavailable, skipping banter quality check: %s", exc)
                    except AudioQualityError as exc:
                        logger.warning("Quality gate rejected banter (%s): %s", audio_path.name, exc)
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
                                state.last_banter_script = [{"host": "Radio", "text": "(pre-recorded banter)"}]
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
                                continue
                        else:
                            continue

                # Clear new-listener flag only after banter was successfully produced
                if _is_new_listener:
                    state.new_listeners_pending = max(0, state.new_listeners_pending - _new_listener_count)

                segment = Segment(
                    type=SegmentType.BANTER,
                    path=audio_path,
                    metadata={"type": "banter", "lines": state.last_banter_script, "canned": canned is not None},
                    ephemeral=canned is None,
                )
                success_callback = state.after_banter

            elif seg_type == SegmentType.NEWS_FLASH:
                logger.info("Producing NEWS FLASH")

                try:
                    host, text, category = await write_news_flash(state, config)
                    flash_path = config.tmp_dir / f"flash_{uuid4().hex[:8]}.mp3"

                    # Synthesize with extra energy for sports
                    flash_prosody: dict[str, str] = {}
                    if category == "sports":
                        flash_prosody = {"rate": "+25%", "pitch": "+12Hz"}
                    elif category == "traffic":
                        flash_prosody = {"rate": "+10%"}

                    await synthesize(
                        text,
                        host.voice,
                        flash_path,
                        **flash_prosody,
                        engine=host.engine,
                        edge_fallback_voice=host.edge_fallback_voice,
                    )

                    # Try to overlay on the tail of the last music segment
                    crossfade_out = config.tmp_dir / f"flash_transition_{uuid4().hex[:8]}.mp3"
                    audio_path = await _try_crossfade(flash_path, config, crossfade_out, tail_seconds=6.0)

                    state.last_banter_script = [{"host": host.name, "text": text, "type": "news_flash"}]
                except Exception as exc:
                    logger.warning("News flash TTS failed, skipping: %s", exc)
                    continue

                segment = Segment(
                    type=SegmentType.NEWS_FLASH,
                    path=audio_path,
                    metadata={"type": "news_flash", "category": category, "host": host.name},
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
                    if not sweeper_voice:
                        sweeper_voice = random.choice(config.hosts).voice
                    loop = asyncio.get_running_loop()

                    voice_task = synthesize(ident_text, sweeper_voice, voice_path)
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
                    metadata={"type": "station_id", "text": ident_text},
                )
                success_callback = state.after_station_id

            elif seg_type == SegmentType.SWEEPER:
                logger.info("Producing SWEEPER")
                sb = config.sonic_brand

                try:
                    sweeper_text = random.choice(sb.sweepers) if sb.sweepers else config.station.name

                    sweeper_voice = sb.sweeper_voice
                    sweeper_engine = "edge"
                    sweeper_fallback = ""
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
                    )
                except Exception as exc:
                    logger.warning("Sweeper generation failed: %s", exc)
                    continue

                segment = Segment(
                    type=SegmentType.SWEEPER,
                    path=audio_path,
                    metadata={"type": "sweeper", "text": sweeper_text},
                )
                success_callback = state.after_sweeper

            elif seg_type == SegmentType.TIME_CHECK:
                logger.info("Producing TIME CHECK")
                now = datetime.datetime.now()
                hour = now.hour
                minute = now.minute
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
                        synthesize(time_text, host.voice, voice_path),
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
                    metadata={"type": "time_check", "time": time_text},
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

                loop = asyncio.get_running_loop()
                sfx_dir = Path(config.ads.sfx_dir) if config.ads.sfx_dir else None

                # ── Pre-compute brand selections (pure sync, no I/O) ──
                used_brands_this_break: list[str] = []
                break_formats: list[str] = []
                spot_params = []
                for spot_idx in range(num_spots):
                    brand = _pick_brand(
                        config.ads.brands,
                        list(state.ad_history)
                        + [AdHistoryEntry(brand=b, summary="", timestamp=0) for b in used_brands_this_break],
                    )
                    used_brands_this_break.append(brand.name)
                    ad_format, sonic, roles_needed = _select_ad_creative(brand, state, config)
                    voice_map = _cast_voices(brand, config, roles_needed)
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
                        ihost, itext = await write_transition(state, config, next_segment="ad")
                    except Exception:
                        ihost = random.choice(config.hosts)
                        itext = random.choice(AD_BREAK_INTROS)
                    ipath = config.tmp_dir / f"ad_intro_{uuid4().hex[:8]}.mp3"
                    await synthesize(
                        itext,
                        ihost.voice,
                        ipath,
                        engine=ihost.engine,
                        edge_fallback_voice=ihost.edge_fallback_voice,
                    )
                    xout = config.tmp_dir / f"ad_trans_{uuid4().hex[:8]}.mp3"
                    ipath = await _try_crossfade(ipath, config, xout)
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
                        )
                        parts.append(ppath)
                    except Exception:
                        pass
                    return parts, itext

                async def _build_bumpers(_num_spots=num_spots, _loop=loop):
                    """Opening bumper + all mid-spot bumpers in parallel."""
                    bumper_in = config.tmp_dir / f"bumper_in_{uuid4().hex[:8]}.mp3"
                    mid_bumpers = [
                        config.tmp_dir / f"bumper_mid_{uuid4().hex[:8]}.mp3" for _ in range(max(0, _num_spots - 1))
                    ]
                    tasks = [_loop.run_in_executor(None, generate_bumper_jingle, bumper_in)]
                    for mb in mid_bumpers:
                        tasks.append(_loop.run_in_executor(None, generate_bumper_jingle, mb, 0.8))
                    await asyncio.gather(*tasks)
                    return bumper_in, mid_bumpers

                # Fan out: intro + LLM scripts + bumpers all in parallel
                (intro_parts, intro_text), scripts, (bumper_in, mid_bumpers) = await asyncio.gather(
                    _build_intro(),
                    asyncio.gather(
                        *(
                            write_ad(brand, vm, state, config, ad_format=af, sonic=sn)
                            for brand, af, sn, vm in spot_params
                        )
                    ),
                    _build_bumpers(),
                )

                # ── PHASE 2: Fan out all ad TTS synthesis in parallel ──
                ad_paths = await asyncio.gather(
                    *(
                        synthesize_ad(script, vm, config.tmp_dir, sfx_dir)
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

                # ── PHASE 4: Closing bumper + outro in parallel ──
                bumper_out = config.tmp_dir / f"bumper_out_{uuid4().hex[:8]}.mp3"
                outro_host = random.choice(config.hosts)
                outro_path = config.tmp_dir / f"ad_outro_{uuid4().hex[:8]}.mp3"
                outro_text = random.choice(AD_BREAK_OUTROS)
                await asyncio.gather(
                    loop.run_in_executor(None, generate_bumper_jingle, bumper_out),
                    synthesize(outro_text, outro_host.voice, outro_path),
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
                }
                segment = Segment(
                    type=SegmentType.AD,
                    path=ad_break_path,
                    metadata={
                        "type": "ad_break",
                        "brands": break_brands,
                        "spots": num_spots,
                    },
                )
                _bound_brands = break_brands

                def _ad_callback(_b=_bound_brands) -> None:
                    state.after_ad(brands=_b)

                success_callback = _ad_callback

        except Exception as e:
            # Recoverable: network/ffmpeg/disk/httpx errors — insert silence, retry next loop
            logger.error("Failed to produce %s segment: %s", seg_type.value, e)
            state.failed_segments += 1
            # Backoff on persistent failures to avoid CPU-burning tight loop
            consecutive = state.failed_segments
            if consecutive > 1:
                backoff = min(30.0, 2.0 ** min(consecutive, 5))
                logger.warning("Consecutive failures: %d — backing off %.0fs", consecutive, backoff)
                await asyncio.sleep(backoff)
            silence_path = config.tmp_dir / f"silence_{uuid4().hex[:8]}.mp3"
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, generate_silence, silence_path, 5.0)
            except Exception as silence_err:
                logger.error("Cannot generate silence (ffmpeg broken?): %s", silence_err)
                # Retry quickly so a transient ffmpeg failure does not stall the stream.
                await asyncio.sleep(0.5)
                continue
            segment = Segment(
                type=seg_type,
                path=silence_path,
                metadata={"error": str(e)},
            )
            # Do NOT advance state counters — failed segment doesn't count

        if segment:
            if generation_revision != state.playlist_revision:
                logger.info("Discarding stale %s segment after playlist source switch", seg_type.value)
                segment.path.unlink(missing_ok=True)
                continue
            if not await _queue_segment(segment):
                continue
            state.queued_segments.append(
                {
                    "type": seg_type.value,
                    "label": segment.metadata.get("title", seg_type.value),
                    "spotify_id": segment.metadata.get("spotify_id", ""),
                    "reason": segment.metadata.get("queue_reason", "Rendered and queued for playback."),
                }
            )
            if "error" not in segment.metadata:
                if success_callback:
                    success_callback()
                state.failed_segments = 0  # Reset backoff on success
            logger.info("Queued %s (queue size: %d)", seg_type.value, queue.qsize())
