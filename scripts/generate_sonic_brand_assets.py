#!/usr/bin/env python3
"""Validation-only compatibility entrypoint for the installed imaging pack.

The original command generated oscillator-based assets and later exposed the
now-rejected Recorded Night Drive source rebuild. Both write paths are retired.
The complete-pack gate and digest-bound promoter are the only supported way to
materialize Modern Night Drive. This wrapper can only validate the installed
immutable runtime manifest; human listening approval remains on the external
board manifest and is intentionally not required here.
"""

from __future__ import annotations

import argparse
from importlib import import_module

_VALIDATOR_MODULE = f"{__package__}.validate_audio_asset_pack" if __package__ else "validate_audio_asset_pack"
validate_audio_asset_pack = import_module(_VALIDATOR_MODULE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the installed immutable runtime pack and attribution ledger",
    )
    args = parser.parse_args(argv)

    if not args.validate_only:
        parser.error("asset generation is retired; use --validate-only")
    return validate_audio_asset_pack.main([])


if __name__ == "__main__":
    raise SystemExit(main())
