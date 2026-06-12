"""Album-cover resolution for the now-playing surfaces.

The listener PWA already pushes ``Track.album_art`` to the phone lock screen /
CarPlay / Control Center via the MediaSession API (``web/static/listener.js``),
and the Home Assistant push surfaces it as ``entity_picture``. Chart tracks read
their cover straight from the Apple charts RSS feed; everything else (admin adds,
listener requests, tracks carrying only a YouTube video thumbnail) is resolved
here against the iTunes Search API.

Resolution is best-effort and OFF the audio path. Every failure mode returns
``None`` — it must NEVER raise into the producer/playback or HA-push code. The
network timeout lives on ``urlopen`` itself so a stuck socket cannot hang a caller.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import unicodedata
from http.client import HTTPException
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

logger = logging.getLogger(__name__)

COVER_CACHE_FILENAME = "cover_art_cache.json"

# Per-call ceiling on the iTunes lookup. Lives on urlopen (socket timeout) so a
# stalled server can never block the caller longer than this.
_ITUNES_TIMEOUT_S = 2.0
_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

# How long a *definitive* "no match" stays cached before we try again. Transient
# failures (timeout / 429 / network) are never cached, so they retry next time.
_NEGATIVE_TTL_S = 7 * 24 * 3600

# YouTube thumbnail hosts: tracks carrying one of these as album_art get upgraded
# to a real cover. A genuine cover (charts RSS, Jamendo image) is left untouched.
_YT_THUMB_HOSTS = ("ytimg.com", "youtube.com", "ggpht.com")

# Single writer for the JSON cache file. Admin-add and listener-request resolves
# can overlap, so read-modify-write is serialized and the file is replaced atomically.
_cache_lock = threading.Lock()


def upscale_itunes_artwork(url: str) -> str:
    """Swap iTunes' ``100x100bb`` artwork URL for a 600px variant.

    The resize is an undocumented CDN convention, so only rewrite when the exact
    token is present and keep the original URL otherwise.
    """
    token = "100x100bb"
    return url.replace(token, "600x600bb") if token in url else url


def needs_resolve(album_art: str | None) -> bool:
    """True when album_art is missing or a YouTube video thumbnail (not a real cover)."""
    art = (album_art or "").strip()
    if not art:
        return True
    host = (urlsplit(art).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _YT_THUMB_HOSTS)


def _canonical_key(artist: str, title: str) -> str:
    """Hash a normalized (artist, title) so keys never collide on separator chars."""

    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKC", s or "").casefold().strip()
        return " ".join(s.split())

    raw = f"{norm(artist)}\x00{norm(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _read_cache(cache_dir: Path) -> dict:
    path = cache_dir / COVER_CACHE_FILENAME
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Cover-art cache is unreadable: %s", path)
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cache_entry(cache_dir: Path, key: str, url: str) -> None:
    """Merge one entry into the cache and replace the file atomically."""
    path = cache_dir / COVER_CACHE_FILENAME
    with _cache_lock:
        cache = _read_cache(cache_dir)
        cache[key] = {"url": url, "ts": time.time()}
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(cache, sort_keys=True))
            tmp.replace(path)
        except OSError as exc:
            logger.debug("Cover-art cache write failed: %s", exc)


def _itunes_search(artist: str, title: str) -> str | None:
    """Look up a song cover on the iTunes Search API.

    Returns the upscaled artwork URL, or ``None`` for a definitive non-match.
    Raises (URLError/OSError/HTTPException/ValueError) on a transient network or
    parse failure so the caller can decline to cache it.
    """
    query = urlencode(
        {
            "term": f"{artist} {title}".strip(),
            "media": "music",
            "entity": "song",
            "country": "IT",
            "limit": "1",
        }
    )
    url = f"{_ITUNES_SEARCH_URL}?{query}"
    with urlopen(url, timeout=_ITUNES_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    results = payload.get("results") or []
    if not results:
        return None
    art = str(results[0].get("artworkUrl100") or results[0].get("artworkUrl60") or "").strip()
    return upscale_itunes_artwork(art) if art else None


def resolve_cover_art(artist: str, title: str, *, cache_dir: Path) -> str | None:
    """Resolve a real album cover for (artist, title). Best-effort, never raises.

    Order: cache hit → iTunes lookup → cache the result. A definitive no-match is
    cached with a TTL; transient failures (timeout / 429 / network) are not cached
    and retry next time. Returns ``None`` when nothing resolves; callers keep
    whatever art they already had (or fall back to the station default).
    """
    if not (artist or title):
        return None
    key = _canonical_key(artist, title)

    cache = _read_cache(cache_dir)
    entry = cache.get(key)
    if isinstance(entry, dict):
        cached_url = str(entry.get("url") or "")
        if cached_url:
            return cached_url
        # Negative entry: honor it until the TTL lapses, then re-try.
        ts = float(entry.get("ts") or 0.0)
        if time.time() - ts < _NEGATIVE_TTL_S:
            return None

    try:
        art = _itunes_search(artist, title)
    except (URLError, OSError, HTTPException, ValueError) as exc:
        # Best-effort: any network or parse failure (timeout, 429, truncated
        # body, bad encoding, malformed JSON) yields no cover and is NOT cached,
        # so it retries next time. Honors the module's never-raises contract —
        # the catch covers every stdlib exception urlopen/read/decode/json can
        # raise (HTTPError/TimeoutError ⊂ OSError; IncompleteRead ⊂ HTTPException;
        # JSONDecodeError/UnicodeDecodeError ⊂ ValueError).
        logger.debug("Cover-art lookup failed for %s - %s: %s", artist, title, exc)
        return None

    # Definitive result (hit or confirmed no-match): cache it.
    _write_cache_entry(cache_dir, key, art or "")
    return art


def maybe_resolve(album_art: str | None, artist: str, title: str, *, cache_dir: Path) -> str:
    """Return a real cover for a single track, preferring existing real art.

    Resolves only when ``album_art`` is missing or a YouTube thumbnail; otherwise
    keeps it. Always returns a string (possibly empty) so callers can assign it
    straight onto ``Track.album_art``.
    """
    current = (album_art or "").strip()
    if not needs_resolve(current):
        return current
    resolved = resolve_cover_art(artist, title, cache_dir=cache_dir)
    return resolved or current
