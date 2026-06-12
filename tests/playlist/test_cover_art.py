"""Tests for the album-cover resolver (off-audio-path, never-raises contract)."""

from __future__ import annotations

import json
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from mammamiradio.playlist.cover_art import (
    _canonical_key,
    maybe_resolve,
    needs_resolve,
    resolve_cover_art,
    upscale_itunes_artwork,
)


def _mock_urlopen(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    mock = MagicMock(return_value=resp)
    return mock


# --- upscale ---


def test_upscale_swaps_known_token():
    assert upscale_itunes_artwork("https://x/100x100bb.jpg") == "https://x/600x600bb.jpg"


def test_upscale_keeps_url_without_token():
    url = "https://x/some-other-size.jpg"
    assert upscale_itunes_artwork(url) == url


# --- needs_resolve (D5 upgrade predicate) ---


def test_needs_resolve_empty():
    assert needs_resolve("") is True
    assert needs_resolve(None) is True


def test_needs_resolve_youtube_thumbnail():
    assert needs_resolve("https://i.ytimg.com/vi/abc/hqdefault.jpg") is True
    assert needs_resolve("https://www.youtube.com/x.jpg") is True


def test_needs_resolve_keeps_real_cover():
    assert needs_resolve("https://is1-ssl.mzstatic.com/image/600x600bb.jpg") is False


# --- canonical cache key ---


def test_canonical_key_no_pipe_collision():
    # "a|b" / "c" vs "a" / "b|c" would collide under a naive "artist|title" key.
    assert _canonical_key("a|b", "c") != _canonical_key("a", "b|c")


def test_canonical_key_unicode_normalized():
    # NFKC + casefold + whitespace-collapse → same key.
    assert _canonical_key("Café  ", "  Song") == _canonical_key("café", "song")


# --- resolve_cover_art happy path ---


def test_resolve_hit_returns_upscaled_url_with_country_it(tmp_path):
    payload = {"results": [{"artworkUrl100": "https://x/100x100bb.jpg"}]}
    mock = _mock_urlopen(payload)
    with patch("mammamiradio.playlist.cover_art.urlopen", mock):
        url = resolve_cover_art("Annalisa", "Bellissima", cache_dir=tmp_path)
    assert url == "https://x/600x600bb.jpg"
    called_url = mock.call_args.args[0]
    assert "country=IT" in called_url
    assert "entity=song" in called_url


def test_resolve_caches_positive_no_second_call(tmp_path):
    payload = {"results": [{"artworkUrl100": "https://x/100x100bb.jpg"}]}
    mock = _mock_urlopen(payload)
    with patch("mammamiradio.playlist.cover_art.urlopen", mock):
        resolve_cover_art("A", "B", cache_dir=tmp_path)
        resolve_cover_art("A", "B", cache_dir=tmp_path)
    assert mock.call_count == 1  # second resolve served from cache


# --- failure modes: all return None, never raise ---


@pytest.mark.parametrize(
    "exc",
    [
        URLError("down"),
        TimeoutError("slow"),
        OSError("socket"),
        HTTPError("https://x", 429, "Too Many Requests", Message(), None),
        # http.client.HTTPException is NOT an OSError — must still be swallowed
        # (a truncated chunked response from iTunes raises this from resp.read()).
        IncompleteRead(b"partial"),
    ],
)
def test_resolve_transient_failure_returns_none_and_not_cached(tmp_path, exc):
    mock = MagicMock(side_effect=exc)
    with patch("mammamiradio.playlist.cover_art.urlopen", mock):
        assert resolve_cover_art("A", "B", cache_dir=tmp_path) is None
        # Transient failures must NOT be cached — second call retries the network.
        assert resolve_cover_art("A", "B", cache_dir=tmp_path) is None
    assert mock.call_count == 2


def test_resolve_malformed_json_returns_none(tmp_path):
    resp = MagicMock()
    resp.read.return_value = b"not json"
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    with patch("mammamiradio.playlist.cover_art.urlopen", MagicMock(return_value=resp)):
        assert resolve_cover_art("A", "B", cache_dir=tmp_path) is None


def test_resolve_bad_encoding_returns_none(tmp_path):
    """A non-UTF-8 body raises UnicodeDecodeError (a ValueError) — must be swallowed."""
    resp = MagicMock()
    resp.read.return_value = b"\xff\xfe invalid utf8"
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    with patch("mammamiradio.playlist.cover_art.urlopen", MagicMock(return_value=resp)):
        assert resolve_cover_art("A", "B", cache_dir=tmp_path) is None


def test_resolve_empty_results_is_definitive_miss_and_cached(tmp_path):
    mock = _mock_urlopen({"results": []})
    with patch("mammamiradio.playlist.cover_art.urlopen", mock):
        assert resolve_cover_art("A", "B", cache_dir=tmp_path) is None
        # Definitive no-match IS cached (within TTL) — second call skips the network.
        assert resolve_cover_art("A", "B", cache_dir=tmp_path) is None
    assert mock.call_count == 1


def test_resolve_blank_inputs_short_circuit(tmp_path):
    mock = MagicMock()
    with patch("mammamiradio.playlist.cover_art.urlopen", mock):
        assert resolve_cover_art("", "", cache_dir=tmp_path) is None
    mock.assert_not_called()


# --- maybe_resolve (single-track entry point) ---


def test_maybe_resolve_keeps_real_cover_without_lookup(tmp_path):
    mock = MagicMock()
    with patch("mammamiradio.playlist.cover_art.urlopen", mock):
        out = maybe_resolve("https://is1.mzstatic.com/600x600bb.jpg", "A", "B", cache_dir=tmp_path)
    assert out == "https://is1.mzstatic.com/600x600bb.jpg"
    mock.assert_not_called()


def test_maybe_resolve_upgrades_youtube_thumbnail(tmp_path):
    payload = {"results": [{"artworkUrl100": "https://x/100x100bb.jpg"}]}
    with patch("mammamiradio.playlist.cover_art.urlopen", _mock_urlopen(payload)):
        out = maybe_resolve("https://i.ytimg.com/vi/abc/hq.jpg", "A", "B", cache_dir=tmp_path)
    assert out == "https://x/600x600bb.jpg"


def test_maybe_resolve_falls_back_to_existing_on_miss(tmp_path):
    # No cover found → keep whatever we had (even a thumbnail) rather than blanking it.
    with patch("mammamiradio.playlist.cover_art.urlopen", _mock_urlopen({"results": []})):
        out = maybe_resolve("https://i.ytimg.com/vi/abc/hq.jpg", "A", "B", cache_dir=tmp_path)
    assert out == "https://i.ytimg.com/vi/abc/hq.jpg"


def test_maybe_resolve_never_raises_on_failure(tmp_path):
    with patch("mammamiradio.playlist.cover_art.urlopen", MagicMock(side_effect=URLError("x"))):
        out = maybe_resolve("", "A", "B", cache_dir=tmp_path)
    assert out == ""
