from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import spiritus.bridge as bridge_module
import spiritus.tools as tools_module
from spiritus import Agent, App, Tool
from spiritus.runtime import shell
from spiritus.runtime.shell import _make_ui_handler


class FakeBridge:
    def get_config(self) -> dict:
        return {"app_title": "Test App", "default_model": "opencode/mimo-v2.5-free"}

    def test_overview(self) -> dict:
        return {"item_count": 0}

    def import_files(self, paths: list[str]) -> dict:
        imported = []
        for path in paths:
            imported.append({
                "source": {
                    "id": Path(path).stem,
                    "title": Path(path).name,
                    "processed_markdown": Path(path).read_text(encoding="utf-8"),
                }
            })
        return {"ok": True, "sources": imported, "errors": []}

    def session_events(self, session_id: str):
        yield {"type": "run.started", "session_id": session_id}
        yield {"type": "run.idle", "session_id": session_id}


class RuntimeShellApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ui_dir = Path(self.tmpdir.name) / "ui"
        self.ui_dir.mkdir(parents=True, exist_ok=True)
        (self.ui_dir / "index.html").write_text("<!doctype html><title>Test</title>", encoding="utf-8")

        handler = _make_ui_handler(str(self.ui_dir), FakeBridge())
        import http.server

        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmpdir.cleanup()

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_bridge_post_endpoint_calls_bridge_method(self):
        status, data = self._post_json("/api/bridge/get_config", {"args": []})
        self.assertEqual(status, 200)
        self.assertEqual(data["app_title"], "Test App")

    def test_health_endpoint_responds(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/health")
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])

    def test_event_endpoint_streams_normalized_same_origin_sse(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events?session_id=ses_1")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()

        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.headers["Content-Type"])
        events = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events, [
            {"type": "run.started", "session_id": "ses_1"},
            {"type": "run.idle", "session_id": "ses_1"},
        ])

    def test_upload_endpoint_passes_uploaded_files_to_bridge(self):
        boundary = "----SpiritusTestBoundary"
        parts = [
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"files\"; filename=\"note.txt\"\r\n"
            "Content-Type: text/plain\r\n\r\n"
            "hello from upload\r\n",
            f"--{boundary}--\r\n",
        ]
        body = "".join(parts).encode("utf-8")

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            "/api/upload/import_files",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["sources"][0]["source"]["title"], "note.txt")
        self.assertEqual(data["sources"][0]["source"]["processed_markdown"], "hello from upload")


def test_desktop_shell_starts_python_tool_adapter_for_full_app(tmp_path, monkeypatch):
    tool = Tool(
        "marker",
        "Return a marker",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _arguments: "TOOL_OK",
    )
    app = App(
        "desktop-tool-probe",
        "Desktop Tool Probe",
        tmp_path,
        (
            Agent(
                "assistant",
                "Desktop tool agent",
                "Call the declared tool.",
                "opencode/test-model",
                tools=("marker",),
            ),
        ),
        tools=(tool,),
    )

    class FakeToolServer:
        instances = []

        def __init__(self, declared_tools):
            self.tools = declared_tools
            self.started = False
            self.stopped = False
            self.instances.append(self)

        def start(self):
            self.started = True
            return {"SPIRITUS_TOOL_URL": "http://127.0.0.1:1", "TOKEN": "test"}

        def stop(self):
            self.stopped = True

    class FakeOpenCodeServer:
        instances = []

        def __init__(self, project_root, port_env_var, *, environment):
            self.project_root = project_root
            self.port_env_var = port_env_var
            self.environment = environment
            self.started = False
            self.stopped = False
            self.instances.append(self)

        def start(self):
            self.started = True
            return 12345

        def stop(self):
            self.stopped = True

    class ClosedEvent:
        def __iadd__(self, callback):
            self.callback = callback
            return self

    window = SimpleNamespace(events=SimpleNamespace(closed=ClosedEvent()))
    webview = SimpleNamespace(
        create_window=lambda **_kwargs: window,
        start=lambda **_kwargs: None,
    )

    monkeypatch.setattr(shell, "dispatch_child", lambda: None)
    monkeypatch.setattr(shell, "_load_env", lambda _config: None)
    monkeypatch.setattr(shell, "_set_app_name", lambda _title: None)
    monkeypatch.setattr(shell, "_start_ui_server", lambda _ui, _bridge: 54321)
    monkeypatch.setattr(shell.atexit, "register", lambda _callback: None)
    monkeypatch.setattr(shell, "OpenCodeServer", FakeOpenCodeServer)
    monkeypatch.setattr(tools_module, "ToolServer", FakeToolServer)
    monkeypatch.setattr(bridge_module, "Bridge", lambda config, server: (config, server))
    monkeypatch.setitem(sys.modules, "webview", webview)

    shell.run(app)

    tool_server = FakeToolServer.instances[0]
    opencode = FakeOpenCodeServer.instances[0]
    assert tool_server.tools == (tool,)
    assert tool_server.started is True
    assert tool_server.stopped is True
    assert opencode.environment == {
        "SPIRITUS_TOOL_URL": "http://127.0.0.1:1",
        "TOKEN": "test",
    }
    assert opencode.started is True
    assert opencode.stopped is True


if __name__ == "__main__":
    unittest.main(verbosity=2)
