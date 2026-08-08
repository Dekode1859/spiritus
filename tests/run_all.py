#!/usr/bin/env python3
"""
Run the whole test suite (Python bridge tests + JS app tests).

The Python tests need `webview`, which lives in the app venvs, so we invoke
those interpreters explicitly: jobsearch-os for its bridge tests, learning-os
(Lexicon.ai) for the source pipeline / wiki tests, which also need bs4, pypdf,
etc. The JS tests need `node` on PATH.

    python tests/run_all.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def venv_python(app: str) -> Path:
    py = ROOT / "apps" / app / ".venv" / "Scripts" / "python.exe"
    if not py.exists():  # POSIX layout fallback
        py = ROOT / "apps" / app / ".venv" / "bin" / "python"
    return py


def run(label: str, cmd: list[str]) -> int:
    print(f"\n=== {label} ===")
    return subprocess.run(cmd, cwd=ROOT).returncode


def main() -> int:
    jobsearch_py = venv_python("jobsearch-os")
    lexicon_py = venv_python("learning-os")
    js_py = str(jobsearch_py) if jobsearch_py.exists() else sys.executable
    lx_py = str(lexicon_py) if lexicon_py.exists() else sys.executable

    rc = 0
    rc |= run("Python (jobsearch bridge)", [js_py, "-m", "unittest", "tests.test_bridge"])
    rc |= run("Python (runtime shell API)", [js_py, "-m", "unittest", "tests.test_runtime_shell_api"])
    rc |= run("Python (lexicon pipeline)", [lx_py, "-m", "unittest", "tests.test_lexicon_pipeline"])
    rc |= run("Python (lexicon wiki)", [lx_py, "-m", "unittest", "tests.test_lexicon_wiki"])
    rc |= run("Python (lexicon knowledge)", [lx_py, "-m", "unittest", "tests.test_lexicon_knowledge"])
    rc |= run("Python (lexicon curator)", [lx_py, "-m", "unittest", "tests.test_lexicon_curator"])
    rc |= run("Python (lexicon bridge)", [lx_py, "-m", "unittest", "tests.test_lexicon_bridge"])
    rc |= run("JavaScript (app.js)", ["node", "tests/test_app.mjs"])
    print("\n" + ("ALL GREEN" if rc == 0 else "FAILURES ABOVE"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
