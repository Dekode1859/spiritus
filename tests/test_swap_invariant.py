"""The swap invariant, tested against the frozen example apps.

    "Replace the Core of any Spiritus app with the Core of another and both must
    still run unmodified. Delete any app and Core remains unchanged."

`apps/` is frozen — those apps are no longer developed, they are kept as
evidence that Core runs more than one domain. That makes them a baseline: if a
change to Core stops them from loading, the invariant broke, and an app in
someone else's repository would have broken the same way without warning.

Each app is introspected in a subprocess. Two apps both ship `main.py` and
`app_bridge.py`, so importing them into one interpreter would collide; a
subprocess is also how they actually run. An app whose own dependencies are not
installed is skipped, never silently passed.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = ROOT / "apps"
APP_DIRS = sorted(p.parent for p in APPS_DIR.glob("*/main.py")) if APPS_DIR.is_dir() else []
APP_IDS = [p.name for p in APP_DIRS]


# ── Core's public surface ───────────────────────────────────────────────────
#
# Frozen deliberately. Apps in other repositories bind to these names, and the
# app UIs call bridge methods *by string* over HTTP — a rename is invisible to
# Python and breaks the app at runtime. That is not hypothetical: renaming
# `export_resume_pdf` silently broke every caller until the string was updated.
# Changing either list is a breaking change: bump the version, note it in
# CHANGELOG.md, and update the consumers.

CORE_PUBLIC_API = {"run", "AppConfig", "WorkspaceFolder"}

CORE_BRIDGE_METHODS = {
    "get_config",
    "get_providers", "save_provider_key", "remove_provider_key", "set_default_model",
    "workspace_tree", "workspace_list", "workspace_read", "workspace_write",
    "workspace_delete", "workspace_new_note_path",
    "open_folder_dialog", "open_file_dialog", "open_external",
    "browser_open", "browser_close", "browser_detect_fields", "browser_scrape",
    "browser_get_profile_status", "browser_setup_profile",
    "browser_check_google_login", "browser_reset_profile",
    "export_pdf",
}

# Every way an app's front-end names a bridge method: pywebview's injected API
# (jobsearch-os) and the HTTP endpoints (learning-os, and uploads).
_JS_CALL_PATTERNS = (
    re.compile(r"""call\(\s*['"]([a-z_][a-z0-9_]*)['"]"""),
    re.compile(r"""postBridge\(\s*['"]([a-z_][a-z0-9_]*)['"]"""),
    re.compile(r"""/api/bridge/([a-z_][a-z0-9_]*)"""),
    re.compile(r"""/api/upload/([a-z_][a-z0-9_]*)"""),
)

# Introspects one app the way the runtime does, and reports what Core got.
_PROBE = r"""
import json, os, sys
from pathlib import Path

app_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(app_dir))
sys.path.insert(0, str(app_dir.parents[1]))
os.environ["WORKSPACE_PATH"] = sys.argv[2]      # never touch real user data

try:
    import main
except Exception as exc:                         # app deps absent, or a real break
    print("PROBE_IMPORT_ERROR:" + f"{type(exc).__name__}: {exc}")
    raise SystemExit(3)

from spiritus.config import AppConfig
from spiritus.bridge import Bridge
from spiritus.runtime.server import OpenCodeServer

APP = main.APP
bridge_cls = APP.bridge_cls or Bridge
bridge = bridge_cls(APP, OpenCodeServer(APP.app_root))   # engine never started

print("PROBE_JSON:" + json.dumps({
    "is_app_config": isinstance(APP, AppConfig),
    "app_id": APP.app_id,
    "app_title": APP.app_title,
    "app_root": str(APP.app_root),
    "ui_dir": str(APP.ui_dir) if APP.ui_dir else None,
    "folders": APP.folder_names(),
    "folders_payload": APP.folders_payload(),
    "default_agent": APP.default_agent,
    "default_capture_folder": APP.default_capture_folder,
    "bridge_cls": bridge_cls.__name__,
    "subclasses_core_bridge": issubclass(bridge_cls, Bridge),
    "bridge_methods": sorted(
        m for m in dir(bridge) if not m.startswith("_") and callable(getattr(bridge, m))
    ),
    "config_payload": sorted(bridge.get_config().keys()),
    "agents": [a["name"] for a in bridge.get_config()["agents"]],
    "default_model": bridge.get_config()["default_model"],
}))
"""


def _probe(app_dir: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="swap-ws-") as ws:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, str(app_dir), ws],
            capture_output=True, text=True, timeout=180,
            cwd=str(app_dir),
        )
    out = proc.stdout + proc.stderr
    for line in out.splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line[len("PROBE_JSON:"):])
        if line.startswith("PROBE_IMPORT_ERROR:"):
            detail = line[len("PROBE_IMPORT_ERROR:"):]
            if "ModuleNotFoundError" in detail:
                pytest.skip(f"{app_dir.name}: app dependencies not installed ({detail})")
            pytest.fail(
                f"{app_dir.name} no longer loads against this Core — the swap "
                f"invariant is broken.\n{detail}"
            )
    pytest.fail(f"{app_dir.name}: probe produced no result.\n{out[-2000:]}")


@pytest.fixture(scope="module")
def probes() -> dict[str, dict]:
    return {}


def _app(app_dir: Path, cache: dict) -> dict:
    if app_dir.name not in cache:
        cache[app_dir.name] = _probe(app_dir)
    return cache[app_dir.name]


def test_the_repository_ships_apps_to_test_against():
    """Without a second app there is no evidence the boundary holds at all."""
    assert len(APP_DIRS) >= 2, f"expected frozen example apps under {APPS_DIR}"


class TestCoreSurface:
    """The names apps bind to. These break consumers silently when changed."""

    def test_public_api_is_unchanged(self):
        import spiritus

        assert set(spiritus.__all__) == CORE_PUBLIC_API

    def test_bridge_js_callable_surface_is_unchanged(self):
        from spiritus.bridge import Bridge

        actual = {m for m in dir(Bridge)
                  if not m.startswith("_") and callable(getattr(Bridge, m))}
        added, removed = actual - CORE_BRIDGE_METHODS, CORE_BRIDGE_METHODS - actual
        assert not removed, (
            f"Core removed or renamed bridge methods {sorted(removed)}. App UIs "
            "call these by string over HTTP, so this breaks them at runtime with "
            "no import error. Update the apps and CORE_BRIDGE_METHODS together."
        )
        assert not added, (
            f"Core grew new public bridge methods {sorted(added)}. Add them to "
            "CORE_BRIDGE_METHODS once you are sure they are generic — a "
            "domain-specific method does not belong on Core's Bridge."
        )

    def test_appconfig_contract_fields_are_unchanged(self):
        import dataclasses

        from spiritus import AppConfig

        fields = {f.name for f in dataclasses.fields(AppConfig)}
        required = {
            "app_id", "app_title", "app_root", "ui_dir", "bridge_cls",
            "workspace_dirname", "workspace_folders", "default_capture_folder",
            "default_agent", "window_size", "min_size",
            "env_port_var", "env_workspace_var",
        }
        assert required <= fields, f"AppConfig lost fields apps rely on: {required - fields}"


@pytest.mark.parametrize("app_dir", APP_DIRS, ids=APP_IDS)
class TestFrozenAppStillRuns:
    """Each frozen app, loaded against the current Core exactly as at runtime."""

    def test_declares_a_valid_appconfig(self, app_dir, probes):
        info = _app(app_dir, probes)
        assert info["is_app_config"]
        assert info["app_id"] and info["app_title"]
        assert Path(info["app_root"]).is_dir()

    def test_core_accepts_its_bridge_subclass(self, app_dir, probes):
        info = _app(app_dir, probes)
        assert info["subclasses_core_bridge"], (
            f"{info['bridge_cls']} no longer subclasses Core's Bridge"
        )

    def test_bridge_instantiates_and_answers_get_config(self, app_dir, probes):
        """Construction is where a changed Core contract fails first."""
        info = _app(app_dir, probes)
        assert {"app_title", "app_id", "workspace_path", "workspace_folders",
                "agents", "default_agent", "opencode_port"} <= set(info["config_payload"])

    def test_app_identity_survives_the_round_trip(self, app_dir, probes):
        info = _app(app_dir, probes)
        assert info["app_id"] and info["app_title"]
        assert info["folders"], "app declared no workspace folders"
        for folder in info["folders_payload"]:
            assert set(folder) == {"name", "icon", "label"}

    def test_core_loads_the_agents_the_app_declares(self, app_dir, probes):
        info = _app(app_dir, probes)
        declared = json.loads((app_dir / "opencode.json").read_text(encoding="utf-8"))
        assert sorted(info["agents"]) == sorted(declared.get("agent", {}))
        assert info["default_model"] == declared.get("model", "")

    def test_default_agent_is_one_the_app_actually_declares(self, app_dir, probes):
        info = _app(app_dir, probes)
        if info["default_agent"]:
            assert info["default_agent"] in info["agents"]

    def test_default_capture_folder_is_one_it_declared(self, app_dir, probes):
        info = _app(app_dir, probes)
        if info["default_capture_folder"]:
            assert info["default_capture_folder"] in info["folders"]

    def test_its_custom_ui_is_present_and_served_from_the_app(self, app_dir, probes):
        info = _app(app_dir, probes)
        if info["ui_dir"] is None:
            return                                    # uses the shared chat UI
        ui = Path(info["ui_dir"])
        assert ui.is_dir() and (ui / "index.html").is_file()
        assert app_dir in ui.parents or ui == app_dir

    def test_every_bridge_method_its_ui_calls_still_exists(self, app_dir, probes):
        """The failure mode that renaming export_resume_pdf actually caused.

        App front-ends name bridge methods as strings, so a Core rename passes
        every import and every type check, then fails when a user clicks.
        """
        info = _app(app_dir, probes)
        available = set(info["bridge_methods"])

        called: dict[str, str] = {}
        for js in sorted((app_dir / "ui").rglob("*.js")) if (app_dir / "ui").is_dir() else []:
            text = js.read_text(encoding="utf-8", errors="replace")
            for pattern in _JS_CALL_PATTERNS:
                for name in pattern.findall(text):
                    called.setdefault(name, str(js.relative_to(app_dir)))

        assert called, f"found no bridge calls in {app_dir.name}/ui — check the patterns"
        missing = {n: where for n, where in called.items() if n not in available}
        assert not missing, (
            f"{app_dir.name}'s UI calls bridge methods that no longer exist: "
            f"{missing}. Either Core renamed them, or the app was never updated."
        )


@pytest.mark.parametrize("app_dir", APP_DIRS, ids=APP_IDS)
def test_app_keeps_its_domain_vocabulary_out_of_core(app_dir):
    """The other half of the invariant: deleting an app leaves Core untouched.

    An app extends Core by subclassing Bridge, so its own methods must live in
    its own module — never added to Core's.
    """
    from spiritus.bridge import Bridge

    core_methods = {m for m in dir(Bridge) if not m.startswith("_")}
    app_bridge = app_dir / "app_bridge.py"
    if not app_bridge.is_file():
        pytest.skip(f"{app_dir.name} adds no bridge methods")

    defined = set(re.findall(r"^    def ([a-z_][a-z0-9_]*)\(",
                             app_bridge.read_text(encoding="utf-8"), re.MULTILINE))
    app_specific = {m for m in defined if not m.startswith("_")} - {"__init__"}
    leaked = app_specific & core_methods
    assert not leaked, (
        f"{app_dir.name} defines {sorted(leaked)}, which Core also defines. "
        "Either the app is shadowing Core behavior, or domain vocabulary "
        "reached Core."
    )
