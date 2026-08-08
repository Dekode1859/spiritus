"""The Core↔App contract, and the invariant that makes it worth having.

If these fail, an app that runs on one Core will not run on another.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import spiritus
from spiritus import AppConfig, WorkspaceFolder

PACKAGE_DIR = Path(spiritus.__file__).resolve().parent


def test_public_api_is_exactly_the_documented_surface():
    assert set(spiritus.__all__) == {"run", "AppConfig", "WorkspaceFolder"}
    for name in spiritus.__all__:
        assert hasattr(spiritus, name)


def test_version_is_pep440_parseable():
    assert re.fullmatch(r"\d+\.\d+\.\d+([abrc.].*)?", spiritus.__version__), spiritus.__version__


def test_importing_spiritus_does_not_require_a_gui_toolkit():
    """`import spiritus` must work in CI, packaging, and test contexts.

    shell.py imports webview lazily inside run() for exactly this reason; a
    module-scope import creeping into the run() import chain would break every
    headless consumer.
    """
    import subprocess
    import sys

    code = (
        "import sys, types;"
        "sys.modules['webview'] = None;"   # poison it: any import raises
        "import spiritus;"
        "print(spiritus.__version__)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


class TestWorkspaceFolder:
    def test_label_falls_back_to_name(self):
        assert WorkspaceFolder("raw").display() == "raw"
        assert WorkspaceFolder("raw", "inbox", "Raw Notes").display() == "Raw Notes"

    def test_defaults_are_generic(self):
        f = WorkspaceFolder("anything")
        assert f.icon == "folder"
        assert f.label == ""

    def test_is_hashable_so_config_stays_a_value(self):
        assert len({WorkspaceFolder("a"), WorkspaceFolder("a")}) == 1


class TestAppConfig:
    def _config(self, tmp_path: Path, **kw) -> AppConfig:
        base = {
            "app_id": "test-app",
            "app_title": "Test App",
            "app_root": tmp_path,
            "workspace_folders": (
                WorkspaceFolder("raw", "inbox", "Raw"),
                WorkspaceFolder("out"),
            ),
        }
        base.update(kw)
        return AppConfig(**base)

    def test_app_root_is_coerced_to_path(self, tmp_path):
        cfg = self._config(tmp_path, app_root=str(tmp_path))
        assert isinstance(cfg.app_root, Path)

    def test_relative_ui_dir_resolves_against_app_root(self, tmp_path):
        cfg = self._config(tmp_path, ui_dir="ui")
        assert cfg.ui_dir == tmp_path / "ui"

    def test_absolute_ui_dir_is_left_alone(self, tmp_path):
        elsewhere = tmp_path.parent / "somewhere-else"
        cfg = self._config(tmp_path, ui_dir=elsewhere)
        assert cfg.ui_dir == elsewhere

    def test_ui_dir_defaults_to_none_meaning_shared_ui(self, tmp_path):
        assert self._config(tmp_path).ui_dir is None

    def test_folder_names_preserves_declaration_order(self, tmp_path):
        assert self._config(tmp_path).folder_names() == ["raw", "out"]

    def test_folders_payload_is_json_serializable_with_display_labels(self, tmp_path):
        import json

        payload = self._config(tmp_path).folders_payload()
        assert payload == [
            {"name": "raw", "icon": "inbox", "label": "Raw"},
            {"name": "out", "icon": "folder", "label": "out"},
        ]
        json.dumps(payload)   # must survive the bridge to JS

    def test_an_app_supplying_no_folders_is_valid(self, tmp_path):
        cfg = self._config(tmp_path, workspace_folders=())
        assert cfg.folder_names() == []
        assert cfg.folders_payload() == []

    def test_env_var_names_are_configurable_but_generic(self, tmp_path):
        cfg = self._config(tmp_path)
        assert cfg.env_port_var == "OPENCODE_PORT"
        assert cfg.env_workspace_var == "WORKSPACE_PATH"


# The project's stated invariant: "Core must remain grep-clean of domain words."
# Encoded as a test so it cannot quietly rot. Update the app vocabulary here
# when a new example app arrives; never soften it to make a Core change pass.
# "candidate" is deliberately absent: env_candidates() and local `candidate`
# variables are ordinary English, not domain vocabulary. Words here must be ones
# no generic runtime would ever need.
DOMAIN_WORDS = [
    "curriculum", "flashcard", "lesson",              # learning-os
    "resume", "linkedin", "recruiter", "applicant",   # jobsearch-os / persona
    "careerforge", "jobsearch", "lexicon", "persona",
]

SOURCE_FILES = sorted(
    p for p in PACKAGE_DIR.rglob("*")
    if p.suffix in {".py", ".js", ".html", ".css"} and "__pycache__" not in p.parts
)


def test_the_package_ships_source_to_scan():
    assert len(SOURCE_FILES) >= 10


@pytest.mark.parametrize("word", DOMAIN_WORDS)
def test_core_contains_no_domain_vocabulary(word):
    offenders = []
    for path in SOURCE_FILES:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        if word in text:
            line = next(
                (i for i, ln in enumerate(text.splitlines(), 1) if word in ln), 0
            )
            offenders.append(f"{path.relative_to(PACKAGE_DIR)}:{line}")
    assert not offenders, (
        f"Core leaked the domain word {word!r} at {offenders}. "
        "Domain knowledge belongs in the app, not the runtime."
    )
