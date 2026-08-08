"""
Characterization tests for spiritus/bridge.py.

Locks the current behavior of Bridge before refactoring. Uses stdlib unittest
(no new deps). The workspace is redirected to a temp dir via WORKSPACE_PATH,
which paths.workspace_path() honors, so no real user data is touched.

Run with the jobsearch venv (it provides `webview`):
    apps/jobsearch-os/.venv/Scripts/python.exe -m unittest tests.test_bridge -v
or via tests/run_tests.py.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spiritus import bridge as bridge_mod  # noqa: E402
from spiritus.bridge import Bridge  # noqa: E402
from spiritus.config import AppConfig, WorkspaceFolder  # noqa: E402


class FakeServer:
    """Stand-in for OpenCodeServer: records lifecycle calls, hands out ports."""
    def __init__(self):
        self.port = 4096
        self.home_dir = Path(tempfile.mkdtemp(prefix="fake-home-"))
        self.stops = 0
        self.starts = 0

    def stop(self):
        self.stops += 1

    def start(self):
        self.starts += 1
        self.port += 1          # restart yields a new port, like the real server
        return self.port


def make_bridge(tmp: Path) -> tuple[Bridge, FakeServer]:
    import os
    os.environ["WORKSPACE_PATH"] = str(tmp / "workspace")
    cfg = AppConfig(
        app_id="test-app",
        app_title="Test App",
        app_root=tmp,
        workspace_dirname="workspace",
        workspace_folders=(
            WorkspaceFolder("documents", "inbox", "Documents"),
            WorkspaceFolder("jobs", "briefcase", "Jobs"),
        ),
    )
    server = FakeServer()
    return Bridge(cfg, server), server


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self._provider_hooks = {
            name: getattr(bridge_mod.providers_mod, name)
            for name in ("save_key", "remove_key", "set_default_model")
        }
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.bridge, self.server = make_bridge(self.tmp)
        self.workspace = self.bridge._workspace

    def tearDown(self):
        import os
        for name, function in self._provider_hooks.items():
            setattr(bridge_mod.providers_mod, name, function)
        os.environ.pop("WORKSPACE_PATH", None)
        self._tmpdir.cleanup()


class TestBrowserAgentScript(unittest.TestCase):
    def test_browser_agent_is_valid_python(self):
        # Guards the planned extract-to-file refactor: the script must keep parsing.
        compile(bridge_mod._BROWSER_AGENT, "<browser_agent>", "exec")

    def test_browser_agent_declares_control_endpoints(self):
        for ep in ("/navigate", "/focus", "/detect-fields", "/scrape",
                   "/check-google-login", "/stop"):
            self.assertIn(ep, bridge_mod._BROWSER_AGENT, ep)


class TestStorage(BridgeTestBase):
    def test_write_read_roundtrip(self):
        res = self.bridge.workspace_write("profile/profile.json", '{"x":1}')
        self.assertTrue(res["ok"])
        got = self.bridge.workspace_read("profile/profile.json")
        self.assertEqual(got["content"], '{"x":1}')

    def test_read_missing_returns_error(self):
        got = self.bridge.workspace_read("profile/nope.json")
        self.assertIn("error", got)
        self.assertNotIn("content", got)

    def test_delete(self):
        self.bridge.workspace_write("jobs/jobs.json", "[]")
        self.assertTrue(self.bridge.workspace_delete("jobs/jobs.json")["ok"])
        self.assertIn("error", self.bridge.workspace_read("jobs/jobs.json"))

    def test_list_dir_only_surfaces_text_files(self):
        self.bridge.workspace_write("documents/a.md", "hi")
        self.bridge.workspace_write("documents/b.txt", "yo")
        self.bridge.workspace_write("documents/c.json", "{}")
        names = {f["name"] for f in self.bridge.workspace_list("documents")}
        self.assertEqual(names, {"a.md", "b.txt"})  # .json not surfaced by storage

    def test_traversal_blocked(self):
        with self.assertRaises(ValueError):
            self.bridge.workspace_write("../escape.txt", "x")


class TestBrowserProfileStatus(BridgeTestBase):
    def _meta_path(self) -> Path:
        return self.workspace / "browser-profile" / "profile-meta.json"

    def test_absent_profile(self):
        self.assertEqual(self.bridge.browser_get_profile_status(), {"exists": False})

    def test_valid_profile(self):
        self._meta_path().parent.mkdir(parents=True, exist_ok=True)
        self._meta_path().write_text(
            json.dumps({"google_email": "a@b.com", "setup_date": "2026-01-01"}),
            encoding="utf-8",
        )
        st = self.bridge.browser_get_profile_status()
        self.assertEqual(st["exists"], True)
        self.assertEqual(st["google_email"], "a@b.com")
        self.assertEqual(st["setup_date"], "2026-01-01")

    def test_corrupt_profile_reports_absent(self):
        # Current behavior: unreadable meta is swallowed and treated as absent.
        self._meta_path().parent.mkdir(parents=True, exist_ok=True)
        self._meta_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(self.bridge.browser_get_profile_status(), {"exists": False})


class TestBrowserReset(BridgeTestBase):
    def test_reset_recreates_dir_with_gitkeep(self):
        pdir = self.workspace / "browser-profile"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "profile-meta.json").write_text("{}", encoding="utf-8")
        (pdir / "Cookies").write_text("secret", encoding="utf-8")
        res = self.bridge.browser_reset_profile()
        self.assertTrue(res["ok"])
        self.assertTrue(pdir.is_dir())
        self.assertTrue((pdir / ".gitkeep").exists())
        self.assertFalse((pdir / "profile-meta.json").exists())
        self.assertFalse((pdir / "Cookies").exists())


class TestBrowserGuards(BridgeTestBase):
    def test_detect_fields_without_browser(self):
        res = self.bridge.browser_detect_fields()
        self.assertFalse(res["ok"])
        self.assertIn("not open", res["error"].lower())

    def test_check_google_login_without_browser(self):
        res = self.bridge.browser_check_google_login()
        self.assertFalse(res["ok"])
        self.assertIn("not open", res["error"].lower())

    def test_scrape_empty_url_is_rejected(self):
        # Guards the cheap path: no URL must fail fast without spawning Chromium.
        res = self.bridge.browser_scrape("   ")
        self.assertFalse(res["ok"])
        self.assertIn("no url", res["error"].lower())


class TestServerRestartMethods(BridgeTestBase):
    """The three provider methods share a stop/start/new-port pattern (refactor #2)."""

    def test_save_provider_key_restarts_and_returns_port(self):
        bridge_mod.providers_mod.save_key = lambda home, pid, key: None
        before = self.server.port
        res = self.bridge.save_provider_key("anthropic", "sk-test")
        self.assertTrue(res["ok"])
        self.assertEqual(self.server.stops, 1)
        self.assertEqual(self.server.starts, 1)
        self.assertEqual(res["port"], before + 1)

    def test_remove_provider_key_restarts(self):
        bridge_mod.providers_mod.remove_key = lambda home, pid: None
        res = self.bridge.remove_provider_key("anthropic")
        self.assertTrue(res["ok"])
        self.assertEqual((self.server.stops, self.server.starts), (1, 1))

    def test_set_default_model_returns_model_and_port(self):
        bridge_mod.providers_mod.set_default_model = lambda root, pid, mid: {"model": f"{pid}/{mid}"}
        res = self.bridge.set_default_model("anthropic", "claude")
        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], "anthropic/claude")
        self.assertEqual(res["port"], self.server.port)

    def test_save_provider_key_error_is_reported(self):
        def boom(*a, **k):
            raise RuntimeError("disk full")
        bridge_mod.providers_mod.save_key = boom
        res = self.bridge.save_provider_key("anthropic", "sk")
        self.assertFalse(res["ok"])
        self.assertIn("disk full", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
