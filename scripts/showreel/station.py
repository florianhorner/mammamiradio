#!/usr/bin/env python3
"""Run a disposable loopback station with explicitly synthetic Home bindings.

This launcher is outside the shipped package. It injects a local dependency in
this process; it never writes a profile or enables a production override.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]


def validate_mock_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ValueError("the mock Home Assistant URL must be an explicit loopback HTTP origin")
    return value.rstrip("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock-ha", default="http://127.0.0.1:8123")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--music-dir", type=Path, default=REPO_ROOT / "music")
    args = parser.parse_args(argv)
    mock_url = validate_mock_url(args.mock_ha)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    with tempfile.TemporaryDirectory(prefix="mmr-showreel-") as temporary:
        root = Path(temporary)
        os.environ.update(
            {
                "MAMMAMIRADIO_CACHE_DIR": str(root / "cache"),
                "MAMMAMIRADIO_TMP_DIR": str(root / "tmp"),
                "MAMMAMIRADIO_MUSIC_DIR": str(args.music_dir.resolve()),
                "MAMMAMIRADIO_HA_CONTEXT_ENABLED": "true",
                "MAMMAMIRADIO_BIND_HOST": "127.0.0.1",
                "MAMMAMIRADIO_PORT": str(args.port),
                "MAMMAMIRADIO_ALLOW_YTDLP": "false",
                "HA_ENABLED": "true",
                "HA_URL": mock_url,
                "HA_TOKEN": "synthetic-showreel-token",
                "SUPERVISOR_TOKEN": "",
                "HASSIO_TOKEN": "",
            }
        )
        import uvicorn

        from mammamiradio import main as station
        from mammamiradio.home.authorization import HomeAuthorization
        from mammamiradio.home.bindings import HomeBindings
        from mammamiradio.home.compatibility import HomeCompatibility

        load_config = station.load_config

        def isolated_config(path: str = "radio.toml"):
            config = load_config(path)
            if (
                config.homeassistant.url != mock_url
                or config.ha_token != "synthetic-showreel-token"
                or config.cache_dir != root / "cache"
                or config.tmp_dir != root / "tmp"
            ):
                raise ValueError("showreel configuration escaped its isolated runtime")
            return config

        station.load_config = isolated_config
        document = json.loads((REPO_ROOT / "tests/fixtures/home_profile.json").read_text())
        if any(row["entity_id"] != "sun.sun" and ".example_" not in row["entity_id"] for row in document["entities"]):
            raise ValueError("showreel bindings must be synthetic")
        bindings = HomeBindings.from_document(document, identity="local-showreel")
        station.read_home_compatibility = lambda *_: HomeCompatibility(HomeAuthorization.legacy(bindings), "verified")
        uvicorn.run(station.app, host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
