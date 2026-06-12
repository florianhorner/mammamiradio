from __future__ import annotations

from unittest.mock import patch

from mammamiradio.audio.norm_cache import select_norm_cache_rescue
from mammamiradio.audio.normalizer import save_track_metadata
from mammamiradio.core.models import SegmentLogEntry, SegmentType, StationState, Track


def _write_norm(tmp_path, name: str, *, title: str | None = None, artist: str | None = None):
    path = tmp_path / name
    path.write_bytes(b"audio")
    if title is not None and artist is not None:
        save_track_metadata(path, title=title, artist=artist)
    return path


def test_select_norm_cache_rescue_returns_none_without_cache(tmp_path):
    assert select_norm_cache_rescue(tmp_path, StationState()) is None


def test_select_norm_cache_rescue_avoids_current_song(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Alex Warren - Ordinary",
        "metadata": {"title": "Ordinary", "artist": "Alex Warren"},
    }

    current = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    alternative = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="A far l amore", artist="Raffaella Carra")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]) as choice:
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == alternative
    choice.assert_called_once_with([alternative])
    assert rescue != current


def test_select_norm_cache_rescue_avoids_recent_stream_log_music(tmp_path):
    state = StationState()
    state.stream_log.append(
        SegmentLogEntry(
            type=SegmentType.MUSIC.value,
            label="Alex Warren - Ordinary",
            metadata={"title": "Ordinary", "artist": "Alex Warren"},
        )
    )
    state.stream_log.append(
        SegmentLogEntry(type=SegmentType.BANTER.value, label="Hosts", metadata={"title": "Ordinary"})
    )

    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    alternative = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="Musica Leggera", artist="Colapesce")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]):
        assert select_norm_cache_rescue(tmp_path, state) == alternative


def test_select_norm_cache_rescue_falls_back_when_every_cache_file_is_recent(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Alex Warren - Ordinary",
        "metadata": {"title": "Ordinary", "artist": "Alex Warren"},
    }
    state.stream_log.append(
        SegmentLogEntry(
            type=SegmentType.MUSIC.value,
            label="Raffaella Carra - A far l amore",
            metadata={"title": "A far l amore", "artist": "Raffaella Carra"},
        )
    )

    first = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    second = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="A far l amore", artist="Raffaella Carra")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[-1]) as choice:
        assert select_norm_cache_rescue(tmp_path, state) == second

    choice.assert_called_once_with([first, second])


def test_select_norm_cache_rescue_allows_only_cache_file_when_recent(tmp_path):
    state = StationState(
        current_track=Track(title="Ordinary", artist="Alex Warren", duration_ms=180_000, spotify_id="ordinary")
    )
    only = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]):
        assert select_norm_cache_rescue(tmp_path, state) == only


def test_select_norm_cache_rescue_ignores_malformed_sidecar(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Alex Warren - Ordinary",
        "metadata": {"title": "Ordinary", "artist": "Alex Warren"},
    }

    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    malformed = tmp_path / "norm_broken_sidecar.mp3"
    malformed.write_bytes(b"audio")
    (tmp_path / "norm_broken_sidecar.mp3.json").write_text("{not valid json")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]):
        assert select_norm_cache_rescue(tmp_path, state) == malformed
