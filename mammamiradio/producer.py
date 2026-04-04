"""Segment production pipeline for music, banter, and ad breaks."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from mammamiradio.config import StationConfig
from mammamiradio.downloader import download_track
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
    generate_bumper_jingle,
    generate_silence,
    normalize,
)
from mammamiradio.scheduler import next_segment_type
from mammamiradio.scriptwriter import (
    AD_BREAK_INTROS,
    AD_BREAK_OUTROS,
    write_ad,
    write_banter,
)
from mammamiradio.spotify_player import SpotifyPlayer, download_track_spotify
from mammamiradio.tts import synthesize, synthesize_ad, synthesize_dialogue

logger = logging.getLogger(__name__)

# Directory for pre-bundled banter and ad clips that ship with the package.
# These provide station personality on day 1 without an Anthropic API key.
_DEMO_ASSETS_DIR = Path(__file__).parent / "demo_assets"


_recently_played_clips: list[str] = []


def _pick_canned_clip(subdir: str) -> Path | None:
    """Pick a pre-bundled clip from demo_assets/{subdir}/, avoiding recent repeats."""
    clip_dir = _DEMO_ASSETS_DIR / subdir
    if not clip_dir.is_dir():
        return None
    clips = list(clip_dir.glob("*.mp3"))
    if not clips:
        return None
    # Avoid recently played clips
    eligible = [c for c in clips if c.name not in _recently_played_clips]
    if not eligible:
        _recently_played_clips.clear()
        eligible = clips
    pick = random.choice(eligible)
    _recently_played_clips.append(pick.name)
    if len(_recently_played_clips) > len(clips):
        _recently_played_clips.pop(0)
    return pick


def _pick_brand(brands: list[AdBrand], ad_history: list) -> AdBrand:
    """Pick a brand, avoiding the last 3 aired and weighting recurring brands higher."""
    recent_names = {e.brand for e in ad_history[-3:]}
    eligible = [b for b in brands if b.name not in recent_names]
    if not eligible:
        eligible = list(brands)  # allow repeats if pool exhausted
    weights = [3 if b.recurring else 1 for b in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]


# Default sonic palettes by brand category
_CATEGORY_SONIC: dict[str, SonicWorld] = {
    "tech": SonicWorld(environment="shopping_channel", music_bed="discount_techno", transition_motif="startup_synth"),
    "food": SonicWorld(environment="cafe", music_bed="tarantella_pop", transition_motif="register_hit"),
    "fashion": SonicWorld(environment="showroom", music_bed="suspicious_jazz", transition_motif="whoosh"),
    "beauty": SonicWorld(environment="luxury_spa", music_bed="cheap_synth_romance", transition_motif="mandolin_sting"),
    "services": SonicWorld(environment="motorway", music_bed="lounge", transition_motif="chime"),
    "finance": SonicWorld(environment="", music_bed="suspicious_jazz", transition_motif="hotline_beep"),
    "health": SonicWorld(environment="", music_bed="lounge", transition_motif="ding"),
    "fitness": SonicWorld(environment="stadium", music_bed="upbeat", transition_motif="whoosh"),
    "tourism": SonicWorld(environment="beach", music_bed="tarantella_pop", transition_motif="mandolin_sting"),
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
    if brand.campaign and brand.campaign.sonic_signature:
        cat_sonic = _CATEGORY_SONIC.get(brand.category, SonicWorld())
        sonic = SonicWorld(
            environment=cat_sonic.environment,
            music_bed=cat_sonic.music_bed,
            transition_motif=brand.campaign.sonic_signature.split("+")[0],
            sonic_signature=brand.campaign.sonic_signature,
        )
    elif brand.category in _CATEGORY_SONIC:
        sonic = replace(_CATEGORY_SONIC[brand.category])
    else:
        sonic = SonicWorld()

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
    spotify_player: SpotifyPlayer | None = None,
    skip_event: asyncio.Event | None = None,
) -> None:
    """Keep the lookahead queue filled with rendered segments for live playback."""
    logger.info("Producer started. Playlist: %d tracks", len(state.playlist))

    # Home Assistant context cache
    ha_cache: HomeContext | None = None

    # Don't block on auth — start producing banter immediately,
    # check Spotify connection each time we need to play music
    if spotify_player:
        logger.info("go-librespot running. Select 'mammamiradio' in Spotify to enable real music.")

    was_spotify_connected = False

    while True:
        # Always check Spotify auth (cheap HTTP call)
        if spotify_player:
            await spotify_player.check_auth()
            state.spotify_connected = spotify_player._authenticated

        # Autoplay: when Spotify just connected, capture the current track
        # and generate personalized banter about it IN PARALLEL (the WTF moment)
        if spotify_player and state.spotify_connected and not was_spotify_connected:
            was_spotify_connected = True
            current = await spotify_player.get_current_track()
            if current:
                logger.info("Autoplay: capturing %s + generating banter in parallel", current.display)
                # Skip whatever is currently streaming and purge queued demo segments
                if skip_event:
                    skip_event.set()
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except Exception:
                        break

                norm_path = config.tmp_dir / f"music_{uuid4().hex[:8]}.mp3"

                # Generate banter about THIS specific song while capturing audio
                async def _generate_welcome_banter(track=current):
                    try:
                        canned = _pick_canned_clip("banter") if not config.anthropic_api_key else None
                        if canned:
                            state.last_banter_script = [{"host": "Radio", "text": "(pre-recorded banter)"}]
                            return canned
                        lines = await write_banter(state, config)
                        audio = await synthesize_dialogue(lines, config.tmp_dir)
                        state.last_banter_script = [{"host": h.name, "text": t} for h, t in lines]
                        logger.info("Welcome banter ready (references %s)", track.display)
                        return audio
                    except Exception as exc:
                        logger.warning("Welcome banter generation failed: %s", exc)
                        return None

                try:
                    # Run both in parallel — capture takes song duration,
                    # banter generation takes ~5-10s. Banter finishes first.
                    capture_task = spotify_player.capture_current_audio(current, norm_path)
                    banter_task = _generate_welcome_banter()
                    audio_path, banter_path = await asyncio.gather(capture_task, banter_task)

                    # Queue: user's song first
                    music_seg = Segment(type=SegmentType.MUSIC, path=audio_path, metadata={"title": current.display})
                    await queue.put(music_seg)
                    state.after_music(current)
                    logger.info("Autoplay queued: %s", current.display)

                    # Then the personalized banter (the WTF moment)
                    if banter_path:
                        banter_seg = Segment(
                            type=SegmentType.BANTER,
                            path=banter_path,
                            metadata={"type": "banter", "lines": state.last_banter_script},
                        )
                        await queue.put(banter_seg)
                        state.after_banter()
                        logger.info("Welcome banter queued after %s", current.display)
                except Exception as exc:
                    logger.warning("Autoplay failed: %s", exc)
        if not state.spotify_connected:
            was_spotify_connected = False

        if queue.qsize() >= config.pacing.lookahead_segments:
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
                track = state.reserve_next_track()
                logger.info("Producing MUSIC: %s", track.display)

                norm_path = config.tmp_dir / f"music_{uuid4().hex[:8]}.mp3"

                use_spotify = (
                    spotify_player
                    and spotify_player._authenticated
                    and track.spotify_id
                    and not track.spotify_id.startswith("demo")
                )

                if use_spotify:
                    try:
                        audio_path = await download_track_spotify(spotify_player, track, norm_path)  # type: ignore[arg-type]
                    except Exception as exc:
                        logger.warning("Spotify capture failed, falling back to local: %s", exc)
                        audio_path = await download_track(track, config.cache_dir, music_dir=Path("music"))
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, normalize, audio_path, norm_path)
                else:
                    # Fallback: local files / yt-dlp / placeholder
                    audio_path = await download_track(track, config.cache_dir, music_dir=Path("music"))
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, normalize, audio_path, norm_path)

                segment = Segment(
                    type=SegmentType.MUSIC,
                    path=norm_path,
                    metadata={"title": track.display},
                )
                _bound_track = track

                def _music_callback(_t=_bound_track) -> None:
                    state.after_music(_t)

                success_callback = _music_callback

            elif seg_type == SegmentType.BANTER:
                logger.info("Producing BANTER")

                canned = None
                if not config.anthropic_api_key:
                    canned = _pick_canned_clip("banter")

                if canned:
                    logger.info("Using pre-bundled banter clip: %s", canned.name)
                    audio_path = canned
                    state.last_banter_script = [{"host": "Radio", "text": "(pre-recorded banter)"}]
                else:
                    try:
                        lines = await write_banter(state, config)
                        audio_path = await synthesize_dialogue(lines, config.tmp_dir)
                        state.last_banter_script = [{"host": h.name, "text": t} for h, t in lines]
                    except Exception as exc:
                        logger.warning("Banter TTS failed, skipping segment: %s", exc)
                        continue

                segment = Segment(
                    type=SegmentType.BANTER,
                    path=audio_path,
                    metadata={"type": "banter", "lines": state.last_banter_script},
                )
                success_callback = state.after_banter

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

                # 1. Host tease intro
                intro_text = random.choice(AD_BREAK_INTROS)
                intro_host = random.choice(config.hosts)
                intro_path = config.tmp_dir / f"ad_intro_{uuid4().hex[:8]}.mp3"
                await synthesize(intro_text, intro_host.voice, intro_path)
                break_parts.append(intro_path)

                # 2. Opening bumper jingle
                bumper_in = config.tmp_dir / f"bumper_in_{uuid4().hex[:8]}.mp3"
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, generate_bumper_jingle, bumper_in)
                break_parts.append(bumper_in)

                # Ad spot creative pipeline:
                #   _pick_brand -> _select_ad_creative -> _cast_voices -> write_ad -> synthesize_ad

                # 3. Individual ad spots
                used_brands_this_break: list[str] = []
                break_formats: list[str] = []
                for spot_idx in range(num_spots):
                    # Avoid brands used in this same break
                    brand = _pick_brand(
                        config.ads.brands,
                        state.ad_history
                        + [AdHistoryEntry(brand=b, summary="", timestamp=0) for b in used_brands_this_break],
                    )
                    used_brands_this_break.append(brand.name)

                    # Creative selection and voice casting
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

                    sfx_dir = Path(config.ads.sfx_dir) if config.ads.sfx_dir else None

                    script = await write_ad(
                        brand,
                        voice_map,
                        state,
                        config,
                        ad_format=ad_format,
                        sonic=sonic,
                    )
                    ad_path = await synthesize_ad(script, voice_map, config.tmp_dir, sfx_dir)

                    break_parts.append(ad_path)
                    break_brands.append(brand.name)
                    break_summaries.append(script.summary)
                    break_formats.append(script.format)
                    full_text = " ".join(p.text for p in script.parts if p.type == "voice" and p.text)
                    break_texts.append(full_text)

                    # Record each spot in history with format and sonic info
                    state.record_ad_spot(
                        brand=brand.name,
                        summary=script.summary,
                        format=script.format,
                        sonic_signature=brand.campaign.sonic_signature if brand.campaign else "",
                    )

                    # Bumper jingle between spots (not after last one)
                    if spot_idx < num_spots - 1:
                        between_bumper = config.tmp_dir / f"bumper_mid_{uuid4().hex[:8]}.mp3"
                        await loop.run_in_executor(
                            None,
                            generate_bumper_jingle,
                            between_bumper,
                            0.8,
                        )
                        break_parts.append(between_bumper)

                # 4. Closing bumper jingle
                bumper_out = config.tmp_dir / f"bumper_out_{uuid4().hex[:8]}.mp3"
                await loop.run_in_executor(None, generate_bumper_jingle, bumper_out)
                break_parts.append(bumper_out)

                # 5. Host tease outro
                outro_text = random.choice(AD_BREAK_OUTROS)
                outro_host = random.choice(config.hosts)
                outro_path = config.tmp_dir / f"ad_outro_{uuid4().hex[:8]}.mp3"
                await synthesize(outro_text, outro_host.voice, outro_path)
                break_parts.append(outro_path)

                # 6. Concat everything into one ad break segment
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
                        )
                    finally:
                        for p in break_parts:
                            p.unlink(missing_ok=True)

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
            if "error" not in segment.metadata:
                if success_callback:
                    success_callback()
                state.failed_segments = 0  # Reset backoff on success
            await queue.put(segment)
            logger.info("Queued %s (queue size: %d)", seg_type.value, queue.qsize())
