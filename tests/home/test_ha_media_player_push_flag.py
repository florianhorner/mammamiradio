"""Tests for the MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH flag and the ghost purge.

When an operator installs the HACS ``mammamiradio`` integration they turn the
add-on's ``ha_media_player_push`` option off; the registered MediaPlayerEntity
then owns ``media_player.mammamiradio``. The push must (1) stop POSTing that id
(last-writer-wins would clobber the real entity and flap the card), (2) keep
pushing the three sensors, and (3) delete the stale ghost once so the
integration claims a free id with no dead ghost left behind.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mammamiradio.home.ha_context as ha


def _mock_client() -> AsyncMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = ""
    client = AsyncMock()
    client.post.return_value = resp
    client.delete.return_value = resp
    return client


def _posted_eids(client: AsyncMock) -> list[str]:
    return [call.args[0].rsplit("/api/states/", 1)[-1] for call in client.post.call_args_list]


@pytest.fixture(autouse=True)
def _reset_push_globals():
    """The 2s debounce and the one-shot purge flag are module globals."""
    ha._last_ha_push = 0.0
    ha._last_ha_stop_push = 0.0
    ha._media_player_ghost_purged = False
    ha._ha_entity_payload_fingerprints.clear()
    ha._ha_entity_last_push_at.clear()
    yield


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ("true", True),
        ("1", True),
        ("anything", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("FALSE", False),
    ],
)
def test_media_player_push_enabled(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", raising=False)
    else:
        monkeypatch.setenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", value)
    assert ha._media_player_push_enabled() is expected


@pytest.mark.asyncio
async def test_push_includes_media_player_when_flag_on(monkeypatch):
    monkeypatch.delenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", raising=False)
    client = _mock_client()
    with patch.object(ha, "_get_ha_client", return_value=client):
        await ha.push_state_to_ha(
            "http://ha:8123", "tok", {"type": "music", "metadata": {"title": "X"}}, None, 1, False
        )
    eids = _posted_eids(client)
    assert "media_player.mammamiradio" in eids
    assert "sensor.mammamiradio_segment_type" in eids
    assert client.delete.call_count == 0


@pytest.mark.asyncio
async def test_push_skips_media_player_and_purges_when_flag_off(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", "false")
    client = _mock_client()
    with patch.object(ha, "_get_ha_client", return_value=client):
        await ha.push_state_to_ha(
            "http://ha:8123", "tok", {"type": "music", "metadata": {"title": "X"}}, None, 1, False
        )
    eids = _posted_eids(client)
    # The registered entity owns the id now — never clobber it.
    assert "media_player.mammamiradio" not in eids
    # The sensors have no registered backing; they keep flowing.
    assert "sensor.mammamiradio_segment_type" in eids
    assert "sensor.mammamiradio_listeners" in eids
    assert "binary_sensor.mammamiradio_on_air" in eids
    # The stale ghost is deleted exactly once.
    assert client.delete.call_count == 1
    assert client.delete.call_args.args[0].endswith("/api/states/media_player.mammamiradio")


@pytest.mark.asyncio
async def test_ghost_purge_is_once_per_process(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", "false")
    client = _mock_client()
    with patch.object(ha, "_get_ha_client", return_value=client):
        await ha.push_state_to_ha("http://ha:8123", "tok", {"type": "music"}, None, 1, False)
        ha._last_ha_push = 0.0  # bypass the 2s debounce for the second push
        await ha.push_state_to_ha("http://ha:8123", "tok", {"type": "music"}, None, 1, False)
    # Deleted once, not on every push.
    assert client.delete.call_count == 1


@pytest.mark.asyncio
async def test_flag_off_filters_media_player_when_stopped(monkeypatch):
    """The skip-media_player filter applies on the session-stopped push path too."""
    monkeypatch.setenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", "false")
    client = _mock_client()
    with patch.object(ha, "_get_ha_client", return_value=client):
        await ha.push_state_to_ha("http://ha:8123", "tok", None, None, 0, True)
    eids = _posted_eids(client)
    assert "media_player.mammamiradio" not in eids
    assert "binary_sensor.mammamiradio_on_air" in eids


@pytest.mark.asyncio
async def test_failed_purge_retries_next_push(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH", "false")
    client = _mock_client()
    client.delete.side_effect = [RuntimeError("boom"), client.delete.return_value]
    with patch.object(ha, "_get_ha_client", return_value=client):
        await ha.push_state_to_ha("http://ha:8123", "tok", {"type": "music"}, None, 1, False)
        ha._last_ha_push = 0.0
        await ha.push_state_to_ha("http://ha:8123", "tok", {"type": "music"}, None, 1, False)
    # First delete failed -> the purge flag was reset -> retried on the next push.
    assert client.delete.call_count == 2
