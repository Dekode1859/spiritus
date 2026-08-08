"""Path resolution, agent loading, provider config, and multipart upload parsing."""
from __future__ import annotations

import json

import pytest

from spiritus import agents, providers
from spiritus.runtime import paths
from spiritus.runtime.shell import _parse_multipart_files


class TestPaths:
    def test_resource_path_resolves_inside_the_installed_package(self):
        ui = paths.resource_path("ui")
        assert ui.is_dir()
        assert {p.name for p in ui.iterdir()} >= {"index.html", "app.js", "style.css"}

    def test_dev_project_root_is_the_app_root(self, tmp_path):
        assert paths.project_root(tmp_path, "any-app") == tmp_path

    def test_workspace_path_creates_and_returns_the_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WORKSPACE_PATH", raising=False)
        ws = paths.workspace_path(tmp_path, "any-app", "workspace")
        assert ws == tmp_path / "workspace"
        assert ws.is_dir()

    def test_workspace_path_env_override_wins(self, tmp_path, monkeypatch):
        override = tmp_path / "elsewhere"
        monkeypatch.setenv("WORKSPACE_PATH", str(override))
        assert paths.workspace_path(tmp_path, "any-app", "workspace") == override
        assert override.is_dir()

    def test_blank_env_override_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_PATH", "   ")
        assert paths.workspace_path(tmp_path, "any-app", "ws") == tmp_path / "ws"

    def test_workspace_dirname_comes_from_the_app(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WORKSPACE_PATH", raising=False)
        assert paths.workspace_path(tmp_path, "a", "vault").name == "vault"

    def test_dev_env_candidates_are_app_local(self, tmp_path):
        assert paths.env_candidates(tmp_path, "any-app") == [tmp_path / ".env"]

    def test_not_bundled_in_a_normal_interpreter(self):
        assert paths.is_bundled() is False


class TestAppDataDir:
    """Where a bundled app keeps user data, per platform.

    Each branch is exercised on every host: getting this wrong does not raise,
    it just writes to a directory the platform reserves for nothing, so only an
    assertion catches it.
    """

    def test_windows_uses_localappdata(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert paths.app_data_dir("my-app") == tmp_path / "my-app"

    def test_windows_falls_back_when_localappdata_is_unset(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "win32")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
        assert paths.app_data_dir("my-app") == tmp_path / "AppData" / "Local" / "my-app"

    def test_macos_uses_application_support(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "darwin")
        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
        expected = tmp_path / "Library" / "Application Support" / "my-app"
        assert paths.app_data_dir("my-app") == expected

    def test_linux_honors_xdg_data_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert paths.app_data_dir("my-app") == tmp_path / "my-app"

    def test_linux_falls_back_to_local_share(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "linux")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
        assert paths.app_data_dir("my-app") == tmp_path / ".local" / "share" / "my-app"

    def test_the_directory_is_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "new"))
        assert paths.app_data_dir("my-app").is_dir()

    def test_no_platform_puts_data_under_a_foreign_convention(self, tmp_path, monkeypatch):
        """The bug this replaced: every OS got the macOS path."""
        monkeypatch.setattr(paths.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert "Library" not in paths.app_data_dir("my-app").parts


def _write_opencode(root, config: dict):
    (root / "opencode.json").write_text(json.dumps(config), encoding="utf-8")


class TestAgentLoading:
    def test_reads_agents_declared_by_the_app(self, tmp_path):
        _write_opencode(tmp_path, {"agent": {
            "session-planner": {"description": "Plans a session"},
            "note-taker": {"label": "Scribe"},
        }})
        loaded = agents.load_agents(tmp_path)
        assert loaded == [
            {"name": "session-planner", "label": "Session Planner",
             "description": "Plans a session"},
            {"name": "note-taker", "label": "Scribe", "description": ""},
        ]

    def test_declaration_order_is_preserved(self, tmp_path):
        _write_opencode(tmp_path, {"agent": {"z": {}, "a": {}, "m": {}}})
        assert [a["name"] for a in agents.load_agents(tmp_path)] == ["z", "a", "m"]

    def test_null_agent_spec_does_not_crash(self, tmp_path):
        _write_opencode(tmp_path, {"agent": {"broken": None}})
        assert agents.load_agents(tmp_path) == [
            {"name": "broken", "label": "Broken", "description": ""}
        ]

    @pytest.mark.parametrize("config", [{}, {"agent": {}}, {"agent": None}])
    def test_absent_agents_yield_an_empty_list(self, tmp_path, config):
        _write_opencode(tmp_path, config)
        assert agents.load_agents(tmp_path) == []

    def test_missing_file_yields_empty_rather_than_raising(self, tmp_path):
        assert agents.load_agents(tmp_path) == []

    def test_malformed_json_yields_empty_rather_than_raising(self, tmp_path):
        (tmp_path / "opencode.json").write_text("{not json", encoding="utf-8")
        assert agents.load_agents(tmp_path) == []
        assert agents.default_model(tmp_path) == ""

    def test_default_model_is_read_from_the_app_config(self, tmp_path):
        _write_opencode(tmp_path, {"model": "opencode/some-model"})
        assert agents.default_model(tmp_path) == "opencode/some-model"

    def test_default_model_absent_is_empty_string(self, tmp_path):
        _write_opencode(tmp_path, {})
        assert agents.default_model(tmp_path) == ""


class TestProviders:
    def test_credentials_are_written_app_locally(self, tmp_path):
        providers.save_key(tmp_path, "anthropic", "sk-test")
        auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
        assert json.loads(auth.read_text(encoding="utf-8")) == {
            "anthropic": {"type": "api", "key": "sk-test"}
        }

    def test_saving_a_second_key_preserves_the_first(self, tmp_path):
        providers.save_key(tmp_path, "anthropic", "a")
        providers.save_key(tmp_path, "openai", "b")
        auth = json.loads(
            (tmp_path / ".local/share/opencode/auth.json").read_text(encoding="utf-8"))
        assert set(auth) == {"anthropic", "openai"}

    def test_remove_key_drops_only_the_named_provider(self, tmp_path):
        providers.save_key(tmp_path, "anthropic", "a")
        providers.save_key(tmp_path, "openai", "b")
        providers.remove_key(tmp_path, "anthropic")
        auth = json.loads(
            (tmp_path / ".local/share/opencode/auth.json").read_text(encoding="utf-8"))
        assert set(auth) == {"openai"}

    def test_remove_key_on_a_fresh_home_is_a_no_op(self, tmp_path):
        assert providers.remove_key(tmp_path, "anthropic") == {"ok": True}

    def test_list_providers_without_a_port_returns_empty(self):
        assert providers.list_providers(None) == {"featured": [], "connected": []}

    def test_list_providers_reports_transport_errors(self):
        result = providers.list_providers(1)   # nothing listening on port 1
        assert result["featured"] == [] and "error" in result

    def test_set_default_model_preserves_the_rest_of_the_config(self, tmp_path):
        _write_opencode(tmp_path, {"agent": {"a": {"description": "keep me"}},
                                   "model": "old/model"})
        result = providers.set_default_model(tmp_path, "anthropic", "claude-x")

        assert result == {"ok": True, "model": "anthropic/claude-x"}
        written = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
        assert written["model"] == "anthropic/claude-x"
        assert written["agent"] == {"a": {"description": "keep me"}}


def _multipart(*parts: tuple[str, str, str], boundary: str = "----SpiritusTest") -> bytes:
    """Build a body from (field_name, filename, content) triples."""
    chunks = []
    for name, filename, content in parts:
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: text/plain\r\n\r\n{content}\r\n"
        )
    chunks.append(f"--{boundary}--\r\n")
    return "".join(chunks).encode("utf-8")


CT = "multipart/form-data; boundary=----SpiritusTest"


class TestMultipartParsing:
    """Replaces cgi.FieldStorage, which Python 3.13 removed."""

    def test_extracts_filename_and_content(self):
        body = _multipart(("files", "note.txt", "hello from upload"))
        assert _parse_multipart_files(body, CT) == [("note.txt", b"hello from upload")]

    def test_multiple_files_keep_document_order(self):
        body = _multipart(("files", "a.txt", "A"), ("files", "b.txt", "B"))
        assert _parse_multipart_files(body, CT) == [("a.txt", b"A"), ("b.txt", b"B")]

    def test_other_field_names_are_ignored(self):
        body = _multipart(("files", "keep.txt", "K"), ("other", "drop.txt", "D"))
        assert _parse_multipart_files(body, CT) == [("keep.txt", b"K")]

    def test_utf8_content_is_preserved_byte_exact(self):
        body = _multipart(("files", "u.txt", "em—dash ± α"))
        assert _parse_multipart_files(body, CT)[0][1].decode("utf-8") == "em—dash ± α"

    def test_a_traversing_filename_is_returned_verbatim_for_the_caller_to_strip(self):
        body = _multipart(("files", "../../etc/passwd", "x"))
        assert _parse_multipart_files(body, CT) == [("../../etc/passwd", b"x")]

    @pytest.mark.parametrize("body,content_type", [
        (b"garbage", "text/plain"),
        (b"", CT),
        (b"--wrong-boundary--\r\n", CT),
    ])
    def test_malformed_bodies_yield_empty_rather_than_raising(self, body, content_type):
        assert _parse_multipart_files(body, content_type) == []
