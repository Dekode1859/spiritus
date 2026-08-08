"""Shared pytest setup, and the line between what CI gates on and what it does not.

Three kinds of suite live here:

**Core** — ``test_spiritus_*``, ``test_bridge`` (which characterizes
``spiritus/bridge.py``), ``test_runtime_shell_api``. These are the package's own
contract and always gate.

**Boundary** — ``test_swap_invariant``. Loads both frozen apps against the
current Core and checks they still run. Also always gates: it is the whole
reason ``apps/`` is kept.

**Frozen-app internals** — ``test_lexicon_*``. Characterization tests written to
lock apps/learning-os's own pipeline (file and URL import, wiki indexing,
knowledge jobs, curation) while Core was extracted out of it. They exercise a
multi-step filesystem pipeline driven by a background daemon thread, which makes
them the most platform-sensitive code in the repository and the least relevant
to what the package promises. They are marked ``frozen_app`` and excluded from
the CI gate; ``pytest tests`` still runs them locally, as does
``tests/run_all.py``. Run them when you touch that app — not to release Core.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Suites that test a frozen example app's own internals rather than Core.
FROZEN_APP_PREFIX = "test_lexicon_"


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


collect_ignore_glob: list[str] = []

# apps/learning-os deps: bs4, pypdf, ebooklib, markdownify.
if not (_importable("bs4") and _importable("pypdf")):
    collect_ignore_glob.append(f"{FROZEN_APP_PREFIX}*.py")

# bridge.py imports webview at module scope, which needs a GUI toolkit present.
if not _importable("webview"):
    collect_ignore_glob.append("test_bridge.py")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the frozen-app suites so CI can deselect them by intent, not by path."""
    for item in items:
        if Path(str(item.fspath)).name.startswith(FROZEN_APP_PREFIX):
            item.add_marker(pytest.mark.frozen_app)
