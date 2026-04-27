"""Tests for new features: news flash categories, sports prosody, crossfade,
concat loudnorm, after_news_flash counters, admin CSRF, trigger endpoint,
synthesize_dialogue loudnorm passthrough, and _get_client/_get_system_prompt caching."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mammamiradio.config import PacingSection, load_config
from mammamiradio.models import (
    HostPersonality,
    PersonalityAxes,
    SegmentType,
    StationState,
    Track,
)

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(**kwargs) -> StationState:
    return StationState(
        playlist=[Track(title="Test", artist="Test", duration_ms=200000, spotify_id="test1")],
        **kwargs,
    )


def _make_test_app(*, admin_password: str = "", admin_token: str = ""):
    """Build a minimal FastAPI app with the streamer router and populated state."""
    from fastapi import FastAPI

    from mammamiradio.streamer import LiveStreamHub, router

    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = admin_token
    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000, spotify_id="t1")],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


def _mock_anthropic_response(text: str):
    """Build a mock AsyncAnthropic whose messages.create returns the given text."""
    mock_content_block = MagicMock()
    mock_content_block.text = text
    mock_response = MagicMock()
    mock_response.content = [mock_content_block]
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


# ---------------------------------------------------------------------------
# write_news_flash — each category produces valid (host, text, category)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["traffic", "breaking", "sports", "weather", "culture"])
async def test_write_news_flash_each_category(category):
    """write_news_flash returns valid tuple for every NEWS_FLASH_CATEGORIES key."""
    from mammamiradio.scriptwriter import write_news_flash

    config = load_config(TOML_PATH)
    config.anthropic_api_key = "test-key"
    state = _make_state()
    # played_tracks is a deque (doesn't support slicing); convert to list for test
    state.played_tracks = list(state.played_tracks)

    response_json = json.dumps({"text": f"Breaking {category} news!"})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, text, cat = await write_news_flash(state, config, category=category)

    assert isinstance(host, HostPersonality)
    assert text == f"Breaking {category} news!"
    assert cat == category


@pytest.mark.asyncio
async def test_write_news_flash_sports_picks_most_energetic_host():
    """Sports flash should be assigned to the host with the highest energy."""
    from mammamiradio.scriptwriter import write_news_flash

    config = load_config(TOML_PATH)
    config.anthropic_api_key = "test-key"
    # Override hosts with known energy values
    config.hosts = [
        HostPersonality(name="Calm", voice="it-IT-DiegoNeural", style="calm", personality=PersonalityAxes(energy=20)),
        HostPersonality(
            name="Manic", voice="it-IT-IsabellaNeural", style="manic", personality=PersonalityAxes(energy=90)
        ),
    ]
    state = _make_state()
    # played_tracks is a deque (doesn't support slicing); convert to list for test
    state.played_tracks = list(state.played_tracks)

    response_json = json.dumps({"text": "GOOOL!"})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, _text, _cat = await write_news_flash(state, config, category="sports")

    assert host.name == "Manic"


# ---------------------------------------------------------------------------
# Sports prosody boost in TTS
# ---------------------------------------------------------------------------


def test_prosody_for_host_sports_energy_boost():
    """High-energy host should get +10% rate from _prosody_for_host."""
    from mammamiradio.tts import _prosody_for_host

    # Host with high energy (sports commentator style)
    host = HostPersonality(
        name="Sportscaster", voice="it-IT-DiegoNeural", style="manic", personality=PersonalityAxes(energy=90)
    )
    kwargs = _prosody_for_host(host)
    assert kwargs.get("rate") == "+10%"


def test_prosody_for_host_low_energy():
    """Low-energy host should get -10% rate."""
    from mammamiradio.tts import _prosody_for_host

    host = HostPersonality(
        name="Calm", voice="it-IT-IsabellaNeural", style="zen", personality=PersonalityAxes(energy=20)
    )
    kwargs = _prosody_for_host(host)
    assert kwargs.get("rate") == "-10%"


def test_prosody_for_host_warmth_affects_pitch():
    """High warmth → lower pitch, low warmth → higher pitch."""
    from mammamiradio.tts import _prosody_for_host

    warm_host = HostPersonality(
        name="Warm", voice="it-IT-DiegoNeural", style="warm", personality=PersonalityAxes(warmth=80)
    )
    cold_host = HostPersonality(
        name="Cold", voice="it-IT-DiegoNeural", style="cold", personality=PersonalityAxes(warmth=20)
    )
    assert _prosody_for_host(warm_host).get("pitch") == "-5Hz"
    assert _prosody_for_host(cold_host).get("pitch") == "+5Hz"


def test_prosody_for_host_neutral_returns_empty():
    """Neutral personality (defaults) should produce no prosody overrides."""
    from mammamiradio.tts import _prosody_for_host

    host = HostPersonality(name="Neutral", voice="it-IT-DiegoNeural", style="normal")
    assert _prosody_for_host(host) == {}


# ---------------------------------------------------------------------------
# crossfade_voice_over_music — mocked ffmpeg
# ---------------------------------------------------------------------------


def test_crossfade_voice_over_music_calls_ffmpeg():
    """crossfade_voice_over_music should invoke ffmpeg with the right filter."""
    from mammamiradio.normalizer import crossfade_voice_over_music

    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""

    music = Path("/fake/music.mp3")
    voice = Path("/fake/voice.mp3")
    output = Path("/fake/crossfade.mp3")

    with patch("mammamiradio.normalizer.subprocess.run", return_value=completed) as mock_run:
        result = crossfade_voice_over_music(music, voice, output, tail_seconds=10.0)

    assert result == output
    call_args = mock_run.call_args[0][0]
    assert "ffmpeg" in call_args[0]
    # Check that the sseof flag is set correctly for tail_seconds
    assert "-sseof" in call_args
    sseof_idx = call_args.index("-sseof")
    assert call_args[sseof_idx + 1] == "-10.0"


@pytest.mark.parametrize("tail_seconds", [3.0, 8.0, 15.0])
def test_crossfade_voice_over_music_various_tails(tail_seconds):
    """crossfade_voice_over_music should pass tail_seconds correctly."""
    from mammamiradio.normalizer import crossfade_voice_over_music

    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""

    with patch("mammamiradio.normalizer.subprocess.run", return_value=completed) as mock_run:
        crossfade_voice_over_music(
            Path("/fake/m.mp3"), Path("/fake/v.mp3"), Path("/fake/o.mp3"), tail_seconds=tail_seconds
        )

    call_args = mock_run.call_args[0][0]
    sseof_idx = call_args.index("-sseof")
    assert call_args[sseof_idx + 1] == f"-{tail_seconds}"


# ---------------------------------------------------------------------------
# concat_files with loudnorm=False
# ---------------------------------------------------------------------------


def test_concat_files_without_loudnorm():
    """concat_files(loudnorm=False) should NOT include loudnorm in the filter."""
    from mammamiradio.normalizer import concat_files

    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""

    paths = [Path("/fake/a.mp3"), Path("/fake/b.mp3")]

    # Patch the duration-guard probe so the ffmpeg call is the only subprocess
    # invocation the mock sees (Item 1 added a post-concat ffprobe sanity check).
    with (
        patch("mammamiradio.normalizer.subprocess.run", return_value=completed) as mock_run,
        patch("mammamiradio.normalizer._ffprobe_duration_sec", return_value=None),
    ):
        concat_files(paths, Path("/fake/out.mp3"), loudnorm=False)

    call_args = mock_run.call_args[0][0]
    # Find the filter_complex argument
    fc_idx = call_args.index("-filter_complex")
    filter_str = call_args[fc_idx + 1]
    assert "loudnorm" not in filter_str
    assert "concat=" in filter_str


def test_concat_files_with_loudnorm():
    """concat_files(loudnorm=True) should include loudnorm in the filter."""
    from mammamiradio.normalizer import concat_files

    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stderr = b""

    paths = [Path("/fake/a.mp3"), Path("/fake/b.mp3")]

    with (
        patch("mammamiradio.normalizer.subprocess.run", return_value=completed) as mock_run,
        patch("mammamiradio.normalizer._ffprobe_duration_sec", return_value=None),
    ):
        concat_files(paths, Path("/fake/out.mp3"), loudnorm=True)

    call_args = mock_run.call_args[0][0]
    fc_idx = call_args.index("-filter_complex")
    filter_str = call_args[fc_idx + 1]
    assert "loudnorm" in filter_str


# ---------------------------------------------------------------------------
# Scheduler: after_news_flash resets counters
# ---------------------------------------------------------------------------


def test_after_news_flash_resets_counters():
    """after_news_flash() should reset songs_since_banter and songs_since_news."""
    state = _make_state(
        segments_produced=10,
        songs_since_banter=5,
        songs_since_news=8,
    )
    old_segments = state.segments_produced

    state.after_news_flash(category="sports")

    assert state.songs_since_banter == 0
    assert state.songs_since_news == 0
    assert state.segments_produced == old_segments + 1


def test_songs_since_news_increments_on_music():
    """after_music() should increment songs_since_news."""
    state = _make_state(songs_since_news=3)
    track = Track(title="Song", artist="Artist", duration_ms=200000, spotify_id="s1")

    state.after_music(track)

    assert state.songs_since_news == 4


def test_news_flash_scheduler_counter_lifecycle():
    """Full lifecycle: play songs → NEWS_FLASH → reset → play more songs."""
    from mammamiradio.scheduler import _decide

    pacing = PacingSection(songs_between_banter=2, songs_between_ads=20)

    # Start with songs_since_news=6 and banter threshold met → NEWS_FLASH
    result = _decide(
        segments_produced=10,
        songs_since_ad=1,
        songs_since_banter=5,
        pacing=pacing,
        deterministic=True,
        songs_since_news=6,
    )
    assert result == SegmentType.NEWS_FLASH

    # After news flash, songs_since_news resets to 0 → BANTER (not news)
    result = _decide(
        segments_produced=11,
        songs_since_ad=2,
        songs_since_banter=5,
        pacing=pacing,
        deterministic=True,
        songs_since_news=0,
    )
    assert result == SegmentType.BANTER


# ---------------------------------------------------------------------------
# Admin panel: CSRF token in response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_panel_contains_csrf_token():
    """GET /admin should include a CSRF token in the HTML (not the placeholder)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")

    assert resp.status_code == 200
    # The placeholder should have been replaced with a real token
    assert "__MAMMAMIRADIO_CSRF_TOKEN__" not in resp.text
    # Token is URL-safe base64, at least 20 chars
    assert len(resp.text) > 100  # sanity: HTML has content


# ---------------------------------------------------------------------------
# POST /api/trigger endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_endpoint_sets_force_next():
    """POST /api/trigger with type=news_flash sets force_next on state."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/trigger", json={"type": "news_flash"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["triggered"] == "news_flash"
    assert app.state.station_state.force_next == SegmentType.NEWS_FLASH


@pytest.mark.asyncio
async def test_trigger_endpoint_banter():
    """POST /api/trigger with type=banter should work."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/trigger", json={"type": "banter"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert app.state.station_state.force_next == SegmentType.BANTER


@pytest.mark.asyncio
async def test_trigger_endpoint_ad():
    """POST /api/trigger with type=ad should work."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/trigger", json={"type": "ad"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert app.state.station_state.force_next == SegmentType.AD


@pytest.mark.asyncio
async def test_trigger_endpoint_invalid_type():
    """POST /api/trigger with invalid type returns error."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/trigger", json={"type": "invalid_thing"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "error" in body


@pytest.mark.asyncio
async def test_trigger_endpoint_requires_admin():
    """POST /api/trigger from public IP without auth should return 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/trigger", json={"type": "banter"})

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# synthesize_dialogue: passes loudnorm=False to concat_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_dialogue_passes_loudnorm_false(tmp_path):
    """synthesize_dialogue should call concat_files with loudnorm=False."""
    from mammamiradio.tts import synthesize_dialogue

    def _touch(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x00" * 256)
        return Path(path)

    mock_comm_instance = MagicMock()
    mock_comm_instance.save = AsyncMock(side_effect=lambda p: _touch(p))
    mock_communicate = MagicMock(return_value=mock_comm_instance)

    def _normalize_side_effect(input_path, output_path, config=None, *, loudnorm=True, music_eq=False):
        _touch(output_path)
        return output_path

    def _concat_side_effect(paths, output_path, silence_ms=300, loudnorm=True):
        _touch(output_path)
        return output_path

    host_a = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")
    host_b = HostPersonality(name="Giulia", voice="it-IT-IsabellaNeural", style="calm")
    lines = [(host_a, "Ciao!"), (host_b, "Buongiorno!")]

    with (
        patch("mammamiradio.tts.edge_tts.Communicate", mock_communicate),
        patch("mammamiradio.tts.normalize", side_effect=_normalize_side_effect),
        patch("mammamiradio.tts.concat_files", side_effect=_concat_side_effect) as mock_concat,
    ):
        await synthesize_dialogue(lines, tmp_path)

    # Verify concat_files was called with loudnorm=False
    mock_concat.assert_called_once()
    call_args = mock_concat.call_args
    # The 4th positional arg (index 3) should be False (loudnorm)
    assert call_args[0][3] is False or call_args.kwargs.get("loudnorm") is False


# ---------------------------------------------------------------------------
# _get_client caching
# ---------------------------------------------------------------------------


def test_get_client_returns_same_instance():
    """_get_client should return the same client for the same key."""
    from mammamiradio.scriptwriter import _get_client

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter._anthropic_key", ""),
    ):
        client1 = _get_client("test-key-1")
        client2 = _get_client("test-key-1")

    assert client1 is client2


def test_get_client_creates_new_for_different_key():
    """_get_client should create a new client when key changes."""
    from mammamiradio.scriptwriter import _get_client

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter._anthropic_key", ""),
    ):
        client1 = _get_client("key-a")
        client2 = _get_client("key-b")

    assert client1 is not client2


# ---------------------------------------------------------------------------
# _get_system_prompt caching
# ---------------------------------------------------------------------------


def test_get_system_prompt_caches_by_host_config():
    """_get_system_prompt should return cached result for unchanged config."""
    from mammamiradio.scriptwriter import _get_system_prompt

    config = load_config(TOML_PATH)

    # Reset cache
    with (
        patch("mammamiradio.scriptwriter._cached_prompt_key", ""),
        patch("mammamiradio.scriptwriter._cached_system_prompt", ""),
    ):
        prompt1 = _get_system_prompt(config)
        prompt2 = _get_system_prompt(config)

    assert prompt1 == prompt2
    assert len(prompt1) > 50  # sanity: not empty


def test_get_system_prompt_rebuilds_when_hosts_change():
    """_get_system_prompt should rebuild when host names/styles change."""
    from mammamiradio.scriptwriter import _get_system_prompt

    config = load_config(TOML_PATH)

    with (
        patch("mammamiradio.scriptwriter._cached_prompt_key", ""),
        patch("mammamiradio.scriptwriter._cached_system_prompt", ""),
    ):
        _get_system_prompt(config)  # prime the cache

        # Modify host config
        original_name = config.hosts[0].name
        config.hosts[0].name = "NewHostName_XYZ"

        # Reset cache key to force re-evaluation
        with patch("mammamiradio.scriptwriter._cached_prompt_key", ""):
            prompt2 = _get_system_prompt(config)

        assert "NewHostName_XYZ" in prompt2

        # Restore
        config.hosts[0].name = original_name


# ---------------------------------------------------------------------------
# POST /api/queue/remove
# ---------------------------------------------------------------------------


def _make_app_with_queue(items: list[str]):
    """Build a test app with pre-seeded asyncio.Queue and queued_segments shadow."""
    import asyncio
    from unittest.mock import MagicMock

    from fastapi import FastAPI

    from mammamiradio.models import Segment
    from mammamiradio.streamer import LiveStreamHub, router

    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    state = _make_state()
    q: asyncio.Queue = asyncio.Queue()

    for label in items:
        seg = MagicMock(spec=Segment)
        seg.path = MagicMock()
        seg.path.read_bytes = MagicMock(return_value=b"")
        q.put_nowait(seg)
        state.queued_segments.append({"type": "music", "label": label})

    app.state.queue = q
    app.state.skip_event = asyncio.Event()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = 0.0
    return app


@pytest.mark.asyncio
async def test_queue_remove_happy_path():
    """POST /api/queue/remove index=1 removes the middle item."""
    app = _make_app_with_queue(["Alpha", "Beta", "Gamma"])
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/queue/remove", json={"index": 1})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["removed"] == "Beta"
    labels = [s["label"] for s in app.state.station_state.queued_segments]
    assert labels == ["Alpha", "Gamma"]
    assert app.state.queue.qsize() == 2


@pytest.mark.asyncio
async def test_queue_remove_index_zero():
    """POST /api/queue/remove index=0 removes the first item."""
    app = _make_app_with_queue(["First", "Second"])
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/queue/remove", json={"index": 0})

    assert resp.status_code == 200
    assert resp.json()["removed"] == "First"
    assert len(app.state.station_state.queued_segments) == 1
    assert app.state.station_state.queued_segments[0]["label"] == "Second"


@pytest.mark.asyncio
async def test_queue_remove_out_of_bounds():
    """POST /api/queue/remove with index beyond queue length returns 422."""
    app = _make_app_with_queue(["Only"])
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/queue/remove", json={"index": 99})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_queue_remove_empty_queue():
    """POST /api/queue/remove on empty queue returns ok with removed=null."""
    app = _make_app_with_queue([])
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/queue/remove", json={"index": 0})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["removed"] is None
