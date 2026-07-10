#!/usr/bin/env python3
"""Compatibility entrypoint for the public recorded imaging pack.

The original command generated oscillator-based assets.  It intentionally no
longer does that: running an old command must not overwrite reviewed CC0
recordings with synthetic replacements.  Use ``--validate-only`` in normal CI,
or pass the separately archived, hash-verified masters to rebuild the pack.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import build_public_imaging_pack
import validate_audio_asset_pack


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-only", action="store_true", help="Validate the checked-in public pack and attribution ledger"
    )
    parser.add_argument("--source-dir", type=Path, help="Directory holding the reviewed, non-shipped CC0 masters")
    parser.add_argument("--output-root", type=Path, help="Directory for rebuilt recorded assets")
    parser.add_argument("--verify-sources", action="store_true", help="Verify master checksums without rendering")
    args = parser.parse_args(argv)

    if args.validate_only:
        if args.source_dir is not None or args.output_root is not None or args.verify_sources:
            parser.error("--validate-only cannot be combined with source rebuild options")
        return validate_audio_asset_pack.main([])
    if args.source_dir is None:
        parser.error("recorded-pack rebuilds require --source-dir; use --validate-only for CI")

    forwarded = ["--source-dir", str(args.source_dir)]
    if args.output_root is not None:
        forwarded.extend(("--output-root", str(args.output_root)))
    if args.verify_sources:
        forwarded.append("--verify-sources")
    return build_public_imaging_pack.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
