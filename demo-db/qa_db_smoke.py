#!/usr/bin/env python3
"""Compatibility wrapper for the Stage 2 DB-backed demo smoke."""
from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
runpy.run_path(str(ROOT / "smoke.py"), run_name="__main__")
