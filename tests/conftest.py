"""Shared pytest setup for Spiritus runtime tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


collect_ignore_glob: list[str] = []

# bridge.py imports webview at module scope, which needs a GUI toolkit present.
if not _importable("webview"):
    collect_ignore_glob.append("test_bridge.py")
