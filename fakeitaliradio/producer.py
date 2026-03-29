from __future__ import annotations

import asyncio
import itertools
import logging
import random
import subprocess
from pathlib import Path
from uuid import uuid4

from fakeitaliradio.config import StationConfig
from fakeitaliradio.downloader import download_track
from fakeitaliradio.ha_context import HomeContext, fetch_home_context
from fakeitaliradio.models import AdBrand, AdHistoryEntry, Segment, SegmentType, StationState
from fakeitaliradio.normalizer import normalize, generate_silence
from fakeitaliradio.scheduler import next_segment_type
from fakeitaliradio.normalizer import concat_files, generate_bumper_jingle
from fakeitaliradio.scriptwriter import (
    AD_BREAK_INTROS, AD_BREAK_OUTROS,
    write_ad, write_banter,
)
from fakeitaliradio.spotify_player import SpotifyPlayer, download_track_spotify
from fakeitaliradio.tts import synthesize, synthesize_ad, synthesize_dialogue

logger = logging.getLogger(__name__)


def _pick_brand(brands: list[AdBrand], ad_history: list) -> AdBrand:
    """Pick a brand, avoiding the last 3 aired and weighting recurring brands higher."""
    recent_names = {e.brand for e in ad_history[-3:]}
    eligible = [b for b in brands if b.name not in recent_names]
    if not eligible:
        eligible = list(brands)  # allow repeats if pool exhausted
    weights = [3 if b.recurring else 1 for b in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]



async def run_producer(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
    spotify_player: SpotifyPlayer | None = None,
) -> None:
    track_iter = itertools.cycle(state.playlist)
    logger.info("Producer started. Playlist: %d tracks", len(state.playlist))

    # Home Assistant context cache
    ha_cache: HomeContext | None = None

    # Don't block on auth — start producing banter immediately,
    # check Spotify connection each time we need to play music
    if spotify_player:
        logger.info("go-librespot running. Select 'fakeitaliradio' in Spotify to enable real music.")

    while True:
        # Always check Spotify auth (cheap HTTP call)
        if spotify_player:
            await spotify_player.check_auth()
            state.spotify_connected = spotify_player._authenticated

        if queue.qsize() >= config.pacing.lookahead_segments:
            await asyncio.sleep(0.5)
            continue

        seg_type = next_segment_type(state, config.pacing)
        segment: Segment | None = None

        # Refresh Home Assistant context for banter/ad segments
        if config.homeassistant.enabled and config.ha_token and seg_type in (
            SegmentType.BANTER, SegmentType.AD,
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
                track = next(track_iter)
                logger.info("Producing MUSIC: %s", track.display)

                norm_path = config.tmp_dir / f"music_{uuid4().hex[:8]}.mp3"

                use_spotify = (
                    spotify_player
                    and spotify_player._authenticated
                    and track.spotify_id
                    and not track.spotify_id.startswith("demo")
                )

                if use_spotify:
                    audio_path = await download_track_spotify(
                        spotify_player, track, norm_path
                    )
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
                state.after_music(track)

            elif seg_type == SegmentType.BANTER:
                logger.info("Producing BANTER")
                lines = await write_banter(state, config)
                audio_path = await synthesize_dialogue(lines, config.tmp_dir)

                state.last_banter_script = [
                    {"host": h.name, "text": t} for h, t in lines
                ]
                segment = Segment(
                    type=SegmentType.BANTER,
                    path=audio_path,
                    metadata={"type": "banter", "lines": state.last_banter_script},
                )
                state.after_banter()

            elif seg_type == SegmentType.AD:
                if not config.ads.brands:
                    logger.warning("No brands configured — skipping ad segment")
                    state.after_ad(brand="")
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

                # 3. Individual ad spots
                used_brands_this_break: list[str] = []
                for spot_idx in range(num_spots):
                    # Avoid brands used in this same break
                    brand = _pick_brand(
                        config.ads.brands,
                        state.ad_history + [
                            AdHistoryEntry(brand=b, summary="", timestamp=0)
                            for b in used_brands_this_break
                        ],
                    )
                    used_brands_this_break.append(brand.name)

                    voice = random.choice(config.ads.voices) if config.ads.voices else None
                    logger.info("  Spot %d/%d: %s (voice: %s)",
                                spot_idx + 1, num_spots, brand.name,
                                voice.name if voice else "host")

                    if voice:
                        script = await write_ad(brand, voice, state, config)
                        sfx_dir = Path(config.ads.sfx_dir) if config.ads.sfx_dir else None
                        ad_path = await synthesize_ad(script, voice, config.tmp_dir, sfx_dir)
                    else:
                        from fakeitaliradio.models import AdVoice as _AV
                        host = random.choice(config.hosts)
                        fallback_voice = _AV(name=host.name, voice=host.voice, style=host.style)
                        script = await write_ad(brand, fallback_voice, state, config)
                        sfx_dir = Path(config.ads.sfx_dir) if config.ads.sfx_dir else None
                        ad_path = await synthesize_ad(script, fallback_voice, config.tmp_dir, sfx_dir)

                    break_parts.append(ad_path)
                    break_brands.append(brand.name)
                    break_summaries.append(script.summary)
                    full_text = " ".join(
                        p.text for p in script.parts if p.type == "voice" and p.text
                    )
                    break_texts.append(full_text)

                    # Record each ad in history immediately so next spot sees it
                    state.after_ad(brand=brand.name, summary=script.summary)

                    # Bumper jingle between spots (not after last one)
                    if spot_idx < num_spots - 1:
                        between_bumper = config.tmp_dir / f"bumper_mid_{uuid4().hex[:8]}.mp3"
                        await loop.run_in_executor(
                            None, generate_bumper_jingle, between_bumper, 0.8,
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
                    await loop.run_in_executor(
                        None, concat_files, break_parts, ad_break_path,
                    )
                    for p in break_parts:
                        p.unlink(missing_ok=True)

                # Dashboard display: show all brands in the break
                state.last_ad_script = {
                    "brands": break_brands,
                    "texts": break_texts,
                    "summaries": break_summaries,
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

        except Exception as e:
            # Recoverable: network/ffmpeg/disk/httpx errors — insert silence, retry next loop
            logger.error("Failed to produce %s segment: %s", seg_type.value, e)
            state.failed_segments += 1
            silence_path = config.tmp_dir / f"silence_{uuid4().hex[:8]}.mp3"
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, generate_silence, silence_path, 5.0)
            segment = Segment(
                type=seg_type,
                path=silence_path,
                metadata={"error": str(e)},
            )
            # Do NOT advance state counters — failed segment doesn't count

        if segment:
            await queue.put(segment)
            logger.info(
                "Queued %s (queue size: %d)", seg_type.value, queue.qsize()
            )
