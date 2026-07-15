#!/usr/bin/env python3
"""Validate packaged spoken-audio inventory, hashes, and transcripts."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mammamiradio.core.spoken_assets import validate_spoken_asset_manifest  # noqa: E402


def main() -> int:
    errors = validate_spoken_asset_manifest()
    if errors:
        for error in errors:
            print(f"spoken-assets: {error}", file=sys.stderr)
        return 1
    print("spoken-assets: manifest, hashes, and transcripts are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
