"""Focused producer attribution tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import HostPersonality, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling.producer import RenderedMusicTrack, run_producer

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"


def _make_config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    return config


async def _run_until_status_queued(
    queue: asyncio.Queue[Segment],
    state: StationState,
    config,
    timeout: float = 5.0,
) -> None:
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while not state.queued_segments:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("Producer did not queue a status segment in time")
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _passthrough_talk_bed(audio_path, *_args, **_kwargs):
    """No-op talk bed: these tests assert segment attribution, not imaging."""
    return audio_path


@pytest.fixture(autouse=True)
def _mock_audio_validation():
    with (
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=180.0),
        patch(f"{PRODUCER_MODULE}._apply_talk_bed", _passthrough_talk_bed),
    ):
        yield


@pytest.mark.asyncio
async def test_queued_segment_includes_playlist_index_for_music(tmp_path):
    """Music segments must carry playlist_index >= 0 and source_kind."""
    tracks = [
        Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1", source="classic"),
        Track(title="Canzone Due", artist="Artista", duration_ms=180_000, spotify_id="demo2", source="jamendo"),
    ]
    state = StationState(playlist=tracks, listeners_active=1)
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"fake audio")

    async def fake_render(track: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=track, path=music_path, cache_path=music_path, cache_hit=True)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=fake_render),
        patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="Because it fits."),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
    ):
        await _run_until_status_queued(queue, state, config)

    queued = state.queued_segments[-1]
    assert queued["playlist_index"] >= 0
    assert "source_kind" in queued
    assert queued["source_kind"] == state.playlist[queued["playlist_index"]].source


@pytest.mark.asyncio
async def test_produced_segment_routes_through_egress_funnel(tmp_path):
    """Integration: with the broadcast chain active, a real segment produced by
    run_producer actually flows through the egress funnel — proving the enqueue
    refactor wires the FX pass in, not just the isolated unit tests."""
    track = Track(title="Canzone", artist="Artista", duration_ms=200_000, spotify_id="d1", source="classic")
    state = StationState(playlist=[track], listeners_active=1)
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"fake audio")

    async def fake_render(t: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=t, path=music_path, cache_path=music_path, cache_hit=True)

    def spy_colour(in_path, out_path):
        out_path.write_bytes(b"FM")
        return True

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=fake_render),
        patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="Because it fits."),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
        # Chain active (its version drives the cache-bake path for this cache-hit source).
        patch(f"{PRODUCER_MODULE}.broadcast_chain_version", return_value="testv"),
        patch(f"{PRODUCER_MODULE}.apply_broadcast_chain", side_effect=spy_colour) as m_chain,
    ):
        await _run_until_status_queued(queue, state, config)

    m_chain.assert_called()  # the produced segment passed through the transmitter
    assert m_chain.call_args[0][0] == music_path  # colouring the real produced audio


@pytest.mark.asyncio
async def test_queued_segment_uses_selected_track_identity_for_duplicate_cache_keys(tmp_path):
    """Duplicate cache keys must still report the exact selected pool index."""
    tracks = [
        Track(title="Canzone", artist="Artista", duration_ms=200_000, spotify_id="demo1", source="classic"),
        Track(title="Canzone", artist="Artista", duration_ms=200_000, spotify_id="demo2", source="classic"),
    ]
    state = StationState(playlist=tracks, listeners_active=1, pinned_track=tracks[1])
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"fake audio")

    async def fake_render(track: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=track, path=music_path, cache_path=music_path, cache_hit=True)

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=fake_render),
        patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="Because it fits."),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
    ):
        await _run_until_status_queued(queue, state, config)

    queued = state.queued_segments[-1]
    assert queued["playlist_index"] == 1
    assert queued["source_kind"] == "classic"


@pytest.mark.asyncio
async def test_empty_fallback_keeps_attribution_defaults(tmp_path):
    """Empty fallback audio delivery must not leak stale music attribution or queue silence."""
    from mammamiradio.scheduling import producer

    track = Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1", source="classic")
    state = StationState(playlist=[track], listeners_active=1)
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    recovery_path = tmp_path / "recovery.mp3"
    recovery_path.write_bytes(b"recovery")
    recovery = Segment(
        type=SegmentType.SWEEPER,
        path=recovery_path,
        metadata={"type": "sweeper", "rescue": True, "error_recovery": True, "title": "Recovery sweeper"},
    )

    orig_last_music = producer._last_music_file
    producer._last_music_file = None
    try:
        with (
            patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
            patch(
                f"{PRODUCER_MODULE}._render_music_track",
                new_callable=AsyncMock,
                side_effect=RuntimeError("no audio"),
            ),
            patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
            patch(f"{PRODUCER_MODULE}.select_norm_cache_rescue", return_value=None),
            patch(f"{PRODUCER_MODULE}._build_recovery_sweeper_segment", new_callable=AsyncMock, return_value=recovery),
            patch(
                f"{PRODUCER_MODULE}.generate_silence",
                side_effect=AssertionError("silence should never be a producer recovery fallback"),
                create=True,
            ) as mock_silence,
            patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        ):
            await _run_until_status_queued(queue, state, config)
    finally:
        producer._last_music_file = orig_last_music

    queued = state.queued_segments[-1]
    assert queued["playlist_index"] == -1
    assert queued["source_kind"] == ""
    mock_silence.assert_not_called()
    assert queued["label"] == "Recovery sweeper"


@pytest.mark.asyncio
async def test_post_restart_resume_keeps_music_attribution(tmp_path):
    """After a stopped session resumes, produced music keeps source attribution."""
    track = Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1", source="classic")
    state = StationState(playlist=[track], listeners_active=1, session_stopped=True)
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"fake audio")

    async def fake_render(track: Track, *_args, **_kwargs) -> RenderedMusicTrack:
        return RenderedMusicTrack(track=track, path=music_path, cache_path=music_path, cache_hit=True)

    def fake_tone(path: Path, *_args, **_kwargs):
        path.write_bytes(b"tone")
        return path

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.MUSIC),
        patch(f"{PRODUCER_MODULE}._pick_canned_clip", return_value=None),
        patch(f"{PRODUCER_MODULE}.generate_tone", side_effect=fake_tone),
        patch(f"{PRODUCER_MODULE}._render_music_track", new_callable=AsyncMock, side_effect=fake_render),
        patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.generate_track_rationale", return_value="Because it fits."),
        patch(f"{PRODUCER_MODULE}.classify_track_crate", return_value="test"),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            assert state.queued_segments == []
            state.session_stopped = False
            state.resume_event.set()
            deadline = asyncio.get_event_loop().time() + 5.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Producer did not queue resume bridge")
                await asyncio.sleep(0.05)
            bridge = queue.get_nowait()
            assert bridge.metadata.get("resume_bridge") is True
            assert bridge.metadata.get("audio_source") == "emergency_tone"
            while not state.queued_segments:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Producer did not queue after resume")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    queued = state.queued_segments[-1]
    assert queued["playlist_index"] == 0
    assert queued["source_kind"] == "classic"


@pytest.mark.asyncio
async def test_ha_stop_transition_pushes_idle_state(tmp_path):
    """When a running session stops, producer publishes an idle HA state."""
    state = StationState(listeners_active=0, session_stopped=False)
    config = _make_config(tmp_path)
    config.homeassistant.enabled = True
    config.homeassistant.url = "http://ha.local:8123"
    config.ha_token = "test-token"
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)

    with patch(f"{PRODUCER_MODULE}.push_state_to_ha", new_callable=AsyncMock) as push_state:
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            await asyncio.sleep(0.05)
            state.session_stopped = True
            deadline = asyncio.get_event_loop().time() + 2.0
            while not push_state.await_args_list:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Producer did not publish HA stop transition")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    push_state.assert_awaited()
    stop_call = push_state.await_args
    assert stop_call.kwargs["ha_url"] == "http://ha.local:8123"
    assert stop_call.kwargs["ha_token"] == "test-token"
    assert stop_call.kwargs["now_streaming"] == {}
    assert stop_call.kwargs["current_track"] is None
    assert stop_call.kwargs["listeners_active"] == 0
    assert stop_call.kwargs["session_stopped"] is True
    # The call site must forward the canonical station name (config.display_station_name).
    assert stop_call.kwargs["station_name"] == "Mamma Mi Radio"


@pytest.mark.asyncio
async def test_queued_segment_playlist_index_minus_one_for_nonmusic(tmp_path):
    """Non-music segments must have playlist_index == -1."""
    state = StationState(
        playlist=[Track(title="Canzone Uno", artist="Artista", duration_ms=200_000, spotify_id="demo1")],
        listeners_active=1,
    )
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0] if config.hosts else HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...", None)
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Che bella giornata!")], None),
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.synthesize_dialogue", new_callable=AsyncMock, return_value=tmp_path / "banter.mp3"),
        patch(f"{PRODUCER_MODULE}.concat_files", return_value=None),
    ):
        await _run_until_status_queued(queue, state, config)

    assert state.queued_segments[-1]["playlist_index"] == -1
    assert state.queued_segments[-1]["source_kind"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_render_music_cache_hit_reconciles_loudness(tmp_path, background):
    """A normalization cache hit must still run the loudness reconcile pass on the
    cached file — otherwise a norm file produced before reconciliation aired at its
    old, quieter level ("some songs are just quieter"). Guards producer.py wiring."""
    from mammamiradio.scheduling.producer import _normalized_cache_path, _render_music_track

    track = Track(title="Bye Bye Bye", artist="NSYNC", duration_ms=200_000, spotify_id="bb1", source="classic")
    config = _make_config(tmp_path)
    norm_cached = _normalized_cache_path(track, config)
    norm_cached.write_bytes(b"pre-reconcile norm audio")  # the cache hit

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "dl.mp3"),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "")),
        patch(f"{PRODUCER_MODULE}.reconcile_cached_music") as m_reconcile,
    ):
        result = await _render_music_track(track, config, temp_prefix="t", context="music", background=background)

    assert result is not None and result.cache_hit is True
    assert result.path == norm_cached
    m_reconcile.assert_called_once_with(norm_cached, background=background)


@pytest.mark.asyncio
async def test_render_music_cache_hit_refreshes_actual_youtube_duration_without_clearing_reconcile(tmp_path):
    from mammamiradio.scheduling.producer import _normalized_cache_path, _render_music_track

    track = Track(title="Metadata Says Long", artist="Artist", duration_ms=7_200_000, youtube_id="dQw4w9WgXcQ")
    sibling = Track(title="Normal", artist="Artist", duration_ms=200_000, youtube_id="normal00001")
    config = _make_config(tmp_path)
    raw_path = tmp_path / f"{track.cache_key}.mp3"
    raw_path.write_bytes(b"downloaded audio")
    norm_cached = _normalized_cache_path(track, config)
    norm_cached.write_bytes(b"pre-reconcile norm audio")
    norm_cached.with_name(f"{norm_cached.name}.json").write_text(
        json.dumps(
            {
                "title": "Old title",
                "artist": "Old artist",
                "duration_ms": 7_200_000,
                "reconciled_lufs": -16.0,
            }
        )
    )

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=raw_path),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "")),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=180.0),
        patch(f"{PRODUCER_MODULE}.reconcile_cached_music") as m_reconcile,
    ):
        result = await _render_music_track(
            track,
            config,
            temp_prefix="t",
            context="music",
            playlist=[sibling, track],
        )

    assert result is not None and result.cache_hit is True
    sidecar = json.loads(norm_cached.with_name(f"{norm_cached.name}.json").read_text())
    assert sidecar["title"] == "Metadata Says Long"
    assert sidecar["artist"] == "Artist"
    assert sidecar["duration_ms"] == 180_000
    assert sidecar["reconciled_lufs"] == -16.0
    m_reconcile.assert_called_once_with(norm_cached, background=False)


@pytest.mark.asyncio
async def test_render_music_track_writes_duration_to_norm_sidecar(tmp_path):
    from mammamiradio.scheduling.producer import _normalized_cache_path, _render_music_track

    track = Track(title="Miss Understanding", artist="Sam Brown", duration_ms=204_192, spotify_id="jamendo_1131121")
    config = _make_config(tmp_path)
    norm_cached = _normalized_cache_path(track, config)

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=tmp_path / "download.mp3"),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "")),
        patch(
            f"{PRODUCER_MODULE}.normalize", side_effect=lambda _src, dst, *_args, **_kwargs: dst.write_bytes(b"norm")
        ),
        patch(
            f"{PRODUCER_MODULE}.shutil.copy2",
            side_effect=lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes()),
        ),
    ):
        result = await _render_music_track(track, config, temp_prefix="music", context="music")

    assert result is not None and result.cache_hit is False
    sidecar = json.loads(norm_cached.with_name(f"{norm_cached.name}.json").read_text())
    assert sidecar["title"] == "Miss Understanding"
    assert sidecar["artist"] == "Sam Brown"
    assert sidecar["duration_ms"] == 204_192


@pytest.mark.asyncio
async def test_render_music_track_holds_lied_longform_before_normalize(tmp_path):
    from mammamiradio.scheduling.producer import _render_music_track

    track = Track(title="Looks Short", artist="Artist", duration_ms=180_000, youtube_id="dQw4w9WgXcQ")
    sibling = Track(title="Normal", artist="Artist", duration_ms=200_000, youtube_id="normal00001")
    config = _make_config(tmp_path)
    raw_path = tmp_path / f"{track.cache_key}.mp3"
    raw_path.write_bytes(b"downloaded audio")

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=raw_path),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "")),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=7_200.0),
        patch(f"{PRODUCER_MODULE}.normalize") as mock_normalize,
    ):
        result = await _render_music_track(
            track,
            config,
            temp_prefix="music",
            context="music",
            playlist=[sibling, track],
        )

    assert result is None
    assert raw_path.exists() is False
    mock_normalize.assert_not_called()


@pytest.mark.asyncio
async def test_render_music_track_uses_actual_duration_for_accepted_youtube_sidecar(tmp_path):
    from mammamiradio.scheduling.producer import _normalized_cache_path, _render_music_track

    track = Track(title="Metadata Says Long", artist="Artist", duration_ms=7_200_000, youtube_id="dQw4w9WgXcQ")
    sibling = Track(title="Normal", artist="Artist", duration_ms=200_000, youtube_id="normal00001")
    config = _make_config(tmp_path)
    raw_path = tmp_path / f"{track.cache_key}.mp3"
    raw_path.write_bytes(b"downloaded audio")
    norm_cached = _normalized_cache_path(track, config)

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=raw_path),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "")),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=180.0),
        patch(
            f"{PRODUCER_MODULE}.normalize", side_effect=lambda _src, dst, *_args, **_kwargs: dst.write_bytes(b"norm")
        ),
        patch(
            f"{PRODUCER_MODULE}.shutil.copy2",
            side_effect=lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes()),
        ),
    ):
        result = await _render_music_track(
            track,
            config,
            temp_prefix="music",
            context="music",
            playlist=[sibling, track],
        )

    assert result is not None
    assert track.duration_ms == 180_000
    sidecar = json.loads(norm_cached.with_name(f"{norm_cached.name}.json").read_text())
    assert sidecar["duration_ms"] == 180_000


@pytest.mark.asyncio
async def test_render_music_track_uses_metadata_when_probe_fails(tmp_path):
    from mammamiradio.scheduling.producer import _render_music_track

    track = Track(title="Metadata Longform", artist="Artist", duration_ms=7_200_000, youtube_id="dQw4w9WgXcQ")
    sibling = Track(title="Normal", artist="Artist", duration_ms=200_000, youtube_id="normal00001")
    config = _make_config(tmp_path)
    raw_path = tmp_path / f"{track.cache_key}.mp3"
    raw_path.write_bytes(b"downloaded audio")

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=raw_path),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "")),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=0.0),
        patch(f"{PRODUCER_MODULE}.normalize") as mock_normalize,
    ):
        result = await _render_music_track(
            track,
            config,
            temp_prefix="music",
            context="music",
            playlist=[sibling, track],
        )

    assert result is None
    assert raw_path.exists() is False
    mock_normalize.assert_not_called()


@pytest.mark.asyncio
async def test_render_music_track_holds_chart_youtube_without_exact_id(tmp_path):
    from mammamiradio.scheduling.producer import _render_music_track

    track = Track(
        title="Looks Short",
        artist="Artist",
        duration_ms=180_000,
        spotify_id="chart_looks_short",
        youtube_id="",
        source="youtube",
    )
    sibling = Track(title="Normal", artist="Artist", duration_ms=200_000, youtube_id="normal00001")
    config = _make_config(tmp_path)
    raw_path = tmp_path / f"{track.cache_key}.mp3"
    raw_path.write_bytes(b"downloaded audio")

    with (
        patch(f"{PRODUCER_MODULE}.download_track", new_callable=AsyncMock, return_value=raw_path),
        patch(f"{PRODUCER_MODULE}.validate_download", return_value=(True, "")),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=7_200.0),
        patch(f"{PRODUCER_MODULE}.normalize") as mock_normalize,
    ):
        result = await _render_music_track(
            track,
            config,
            temp_prefix="music",
            context="music",
            playlist=[sibling, track],
        )

    assert result is None
    assert raw_path.exists() is False
    mock_normalize.assert_not_called()


def test_norm_cache_bridge_scrubs_foreign_artist():
    """P1 regression: the producer bridges (queue_drain_recovery / resume_bridge /
    idle_bridge) read the SAME norm-cache sidecar as the streamer rescue. A poisoned
    'Radio X' artist must be scrubbed at this source so it never reaches
    now_streaming.metadata -> the listener now-playing line and Music Assistant."""
    from mammamiradio.scheduling.producer import _norm_cache_bridge_payload

    poisoned = {"title": "Be Without U", "artist": "Radio Sabrina Sensatione"}
    with patch(f"{PRODUCER_MODULE}.load_track_metadata", return_value=poisoned):
        metadata, log_label = _norm_cache_bridge_payload(
            Path("norm_abc_128k.mp3"), "queue_drain_recovery", "Mamma Mi Radio"
        )

    assert "Radio Sabrina Sensatione" not in metadata["artist"]
    assert "Radio Sabrina Sensatione" not in log_label
    assert metadata["artist"] == ""  # whole foreign name stripped (artist field)
    assert metadata["title"] == "Be Without U"  # real title preserved
    assert metadata["queue_drain_recovery"] is True
    assert metadata["audio_source"] == "norm_cache"


def test_norm_cache_bridge_payload_uses_sidecar_duration(tmp_path):
    from mammamiradio.audio.normalizer import save_track_metadata
    from mammamiradio.scheduling.producer import _norm_cache_bridge_payload

    norm = tmp_path / "norm_jamendo_jamendo_1131121_192k.mp3"
    norm.write_bytes(b"x" * 24_000)
    save_track_metadata(norm, title="Miss Understanding", artist="Sam Brown", duration_ms=204_192)

    metadata, _ = _norm_cache_bridge_payload(norm, "idle_bridge", "Mamma Mi Radio", bitrate_kbps=192)

    assert metadata["duration_ms"] == 204_192
    assert metadata["idle_bridge"] is True


def test_norm_cache_bridge_scrubs_foreign_title_prefix():
    """Sibling of the artist scrub: a sidecar title carrying a 'Radio X - Song'
    rescue prefix must be prefix-stripped here too, so the bridge payload never
    leaks a foreign station name on the title while the artist alone was cleaned.
    A song genuinely titled 'Radio Ga Ga' (no separator) survives."""
    from mammamiradio.scheduling.producer import _norm_cache_bridge_payload

    poisoned = {"title": "Radio Sabrina Sensatione – Be Without U", "artist": "Be Without U"}
    with patch(f"{PRODUCER_MODULE}.load_track_metadata", return_value=poisoned):
        metadata, _ = _norm_cache_bridge_payload(Path("norm_abc_128k.mp3"), "queue_drain_recovery", "Mamma Mi Radio")

    assert "Radio Sabrina Sensatione" not in metadata["title"]
    assert metadata["title"] == "Be Without U"

    # A real "Radio X" song title (no separator) is preserved, not blanked.
    real = {"title": "Radio Ga Ga", "artist": "Queen"}
    with patch(f"{PRODUCER_MODULE}.load_track_metadata", return_value=real):
        metadata, _ = _norm_cache_bridge_payload(Path("norm_xyz_128k.mp3"), "idle_bridge", "Mamma Mi Radio")
    assert metadata["title"] == "Radio Ga Ga"
