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


def _choose_first(items, **_kwargs):
    return items[0]


def _choose_last(items, **_kwargs):
    return items[-1]


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

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
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

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
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

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_last) as choice:
        assert select_norm_cache_rescue(tmp_path, state) == second

    choice.assert_called_once_with([first, second])


def test_select_norm_cache_rescue_allows_only_cache_file_when_recent(tmp_path):
    state = StationState(
        current_track=Track(title="Ordinary", artist="Alex Warren", duration_ms=180_000, spotify_id="ordinary")
    )
    only = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
        assert select_norm_cache_rescue(tmp_path, state) == only


def test_select_norm_cache_rescue_skips_blocklisted_cache_file(tmp_path):
    """A banned song must never re-air through the rescue path. The blocklisted
    cache file is dropped even though it is not a recent identity."""
    state = StationState(blocklist={("alex warren", "ordinary"): {"display": "Alex Warren - Ordinary"}})

    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    allowed = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="Musica Leggera", artist="Colapesce")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == allowed
    choice.assert_called_once_with([allowed])


def test_select_norm_cache_rescue_ignores_preferences_on_hot_path(tmp_path):
    state = StationState(
        song_preferences={
            ("raffaella carra", "a far l amore"): {"score": 1},
            ("alex warren", "ordinary"): {"score": -1},
        }
    )

    first = _write_norm(tmp_path, "norm_aaa_liked.mp3")
    second = _write_norm(tmp_path, "norm_zzz_disliked.mp3")

    with (
        patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice,
        patch("mammamiradio.audio.norm_cache.load_track_metadata") as load_metadata,
    ):
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == first
    choice.assert_called_once_with([first, second])
    load_metadata.assert_not_called()


def test_select_norm_cache_rescue_returns_none_when_only_file_is_banned(tmp_path):
    """If every cache file is banned, the rescue degrades to None so the caller's
    next layer (canned clip / forced banter) keeps audio flowing — never a banned song."""
    state = StationState(blocklist={("alex warren", "ordinary"): {"display": "Alex Warren - Ordinary"}})
    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")

    assert select_norm_cache_rescue(tmp_path, state) is None


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

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
        assert select_norm_cache_rescue(tmp_path, state) == malformed
