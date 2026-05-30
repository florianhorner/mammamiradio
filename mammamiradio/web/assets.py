"""Static-asset paths and content-hash versioning for the web layer.

Extracted verbatim from ``web/streamer.py`` (god-module split). Both the
playback loop (demo-rescue audio under ``_ASSETS_DIR``) and page rendering
(``_ASSET_VERSION`` cache-busting) depend on these, so they live in one neutral
module rather than being duplicated. ``_ASSET_VERSION`` is computed once at
import time from the package version plus a hash of the browser-visible CSS/JS.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import re as _re
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent  # mammamiradio/web/
_PKG_ROOT = _THIS_DIR.parent  # mammamiradio/
_TEMPLATES_DIR = _THIS_DIR / "templates"
_STATIC_DIR = _THIS_DIR / "static"
_ASSETS_DIR = _PKG_ROOT / "assets"


def _static_asset_digest() -> str:
    """Return a short content hash for browser-visible CSS/JS assets."""
    digest = hashlib.sha256()
    for name in ("tokens.css", "base.css", "listener.css", "waveform.js", "listener.js", "sw.js"):
        path = _STATIC_DIR / name
        if path.exists():
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


_ASSET_VERSION = f"{importlib.metadata.version('mammamiradio')}-{_static_asset_digest()}"


def _bust_static_cache(html: str) -> str:
    """Append a content-based version to /static/*.css and /static/*.js URLs."""
    return _re.sub(r'(/static/[^"?]+\.(css|js))"', rf'\1?v={_ASSET_VERSION}"', html)
