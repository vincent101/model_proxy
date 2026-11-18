#!/usr/bin/env python3
"""Absolute-path bootstrap for the shared Python CLI."""
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Error: Python >= 3.10 is required by current code; platform support has not been fully validated.")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.cli.dispatch import main

if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
