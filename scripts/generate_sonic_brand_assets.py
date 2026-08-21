#!/usr/bin/env python3
"""Validate the installed imaging pack through the retired generator command.

This command no longer writes assets. Use ``complete_audio_pack_gate.py`` and
``promote_complete_audio_pack.py`` to build and install Modern Night Drive. This
wrapper accepts ``--validate-only`` and checks the runtime manifest and
attribution ledger. The listening board stores the approval record.
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
        help="Validate the installed runtime pack and attribution ledger",
    )
    args = parser.parse_args(argv)

    if not args.validate_only:
        parser.error("asset generation is retired. Use --validate-only")
    return validate_audio_asset_pack.main([])


if __name__ == "__main__":
    raise SystemExit(main())
