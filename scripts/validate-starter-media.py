#!/usr/bin/env python3
"""Run the canonical starter-media validator."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "scripts" / "starter-catalog.py"


if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, str(COMMAND), "check", *sys.argv[1:]])
