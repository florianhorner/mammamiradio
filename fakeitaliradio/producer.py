from __future__ import annotations

import asyncio
import itertools
import logging
import random
from pathlib import Path
from uuid import uuid4

from fakeitaliradio.config import StationConfig
from fakeitaliradio.downloader import download_track
from fakeitaliradio.models import Segment, SegmentType, StationState
from fakeitaliradio.normalizer import normalize, generate_silence
from fakeitaliradio.scheduler import next_segment_type
from fakeitaliradio.models import AdBrand
from fakeitaliradio.scriptwriter import write_ad, write_banter
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


def _update_upcoming(state: StationState, current_track: Track) -> None:
    """Update the upcoming tracks preview based on current position in playlist."""
    try:
        playlist = state.playlist
        if not playlist:
            return
        # Find current track index
        idx = next(
            (i for i, t in enumerate(playlist) if t.spotify_id == current_track.spotify_id),
            0,
        )
        upcoming = []
        for j in range(1, 6):
            upcoming.append(playlist[(idx + j) % len(playlist)])
        state.upcoming_tracks = upcoming
    except Exception:
        pass


async def run_producer(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config: StationConfig,
    spotify_player: SpotifyPlayer | None = None,
) -> None:
    track_iter = itertools.cycle(state.playlist)
    logger.info("Producer started. Playlist: %d tracks", len(state.playlist))

    # Don't block on auth — start producing banter immediately,
    # check Spotify connection each time we need to play music
    if spotify_player:
        logger.info("go-librespot running. Select 'fakeitaliradio' in Spotify to enable real music.")

    while True:
        if queue.qsize() >= config.pacing.lookahead_segments:
            await asyncio.sleep(0.5)
            continue

        seg_type = next_segment_type(state, config.pacing)
        segment: Segment | None = None

        try:
            if seg_type == SegmentType.MUSIC:
                track = next(track_iter)
                logger.info("Producing MUSIC: %s", track.display)

                # Update upcoming preview (peek next 5 tracks)
                upcoming = []
                temp_iter = itertools.tee(track_iter, 1)[0]
                # Can't easily peek a cycle, so use playlist index
                _update_upcoming(state, track)

                norm_path = config.tmp_dir / f"music_{uuid4().hex[:8]}.mp3"

                # Check Spotify connection (quick, non-blocking)
                if spotify_player:
                    await spotify_player.check_auth()
                    state.spotify_connected = spotify_player._authenticated

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

                brand = _pick_brand(config.ads.brands, state.ad_history)
                voice = random.choice(config.ads.voices) if config.ads.voices else None
                logger.info("Producing AD: %s (voice: %s)", brand.name, voice.name if voice else "host")

                if voice:
                    # Use dedicated ad voice
                    script = await write_ad(brand, voice, state, config)
                    sfx_dir = Path(config.ads.sfx_dir) if config.ads.sfx_dir else None
                    ad_path = await synthesize_ad(script, voice, config.tmp_dir, sfx_dir)
                else:
                    # Fallback: use a host voice (old behavior)
                    from fakeitaliradio.models import AdVoice as _AV
                    host = random.choice(config.hosts)
                    fallback_voice = _AV(name=host.name, voice=host.voice, style=host.style)
                    script = await write_ad(brand, fallback_voice, state, config)
                    sfx_dir = Path(config.ads.sfx_dir) if config.ads.sfx_dir else None
                    ad_path = await synthesize_ad(script, fallback_voice, config.tmp_dir, sfx_dir)

                # Collect all voice text for dashboard display
                voice_name = voice.name if voice else host.name
                full_text = " ".join(p.text for p in script.parts if p.type == "voice" and p.text)
                state.last_ad_script = {
                    "brand": brand.name, "voice": voice_name,
                    "text": full_text, "summary": script.summary,
                }
                segment = Segment(
                    type=SegmentType.AD,
                    path=ad_path,
                    metadata={
                        "type": "ad", "brand": brand.name,
                        "text": full_text, "voice": voice_name,
                    },
                )
                state.after_ad(brand=brand.name, summary=script.summary)

        except Exception as e:
            logger.error("Failed to produce %s segment: %s", seg_type.value, e)
            # Insert silence so the stream doesn't stall
            silence_path = config.tmp_dir / f"silence_{uuid4().hex[:8]}.mp3"
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, generate_silence, silence_path, 5.0)
            segment = Segment(
                type=seg_type,
                path=silence_path,
                metadata={"error": str(e)},
            )
            if seg_type == SegmentType.MUSIC:
                state.after_music(next(track_iter))
            elif seg_type == SegmentType.BANTER:
                state.after_banter()
            elif seg_type == SegmentType.AD:
                state.after_ad(brand="")

        if segment:
            await queue.put(segment)
            logger.info(
                "Queued %s (queue size: %d)", seg_type.value, queue.qsize()
            )
