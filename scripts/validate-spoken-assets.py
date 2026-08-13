#!/usr/bin/env python3
"""Validate packaged spoken-audio inventory, hashes, and transcripts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mammamiradio.core.spoken_assets import validate_spoken_asset_manifest  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate packaged spoken-audio inventory, hashes, and transcripts.",
    )
    parser.add_argument(
        "--assets-root",
        type=Path,
        help=("assets directory containing spoken_assets.json; defaults to the repository's packaged demo assets"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.assets_root is None:
        errors = validate_spoken_asset_manifest()
    else:
        errors = validate_spoken_asset_manifest(assets_root=args.assets_root)
    if errors:
        for error in errors:
            print(f"spoken-assets: {error}", file=sys.stderr)
        return 1
    print("spoken-assets: manifest, hashes, and transcripts are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
