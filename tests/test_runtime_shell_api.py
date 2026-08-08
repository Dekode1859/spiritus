from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from spiritus.runtime.shell import _make_ui_handler


class FakeBridge:
    def get_config(self) -> dict:
        return {"app_title": "Commonplace", "default_model": "opencode/mimo-v2.5-free"}

    def commonplace_overview(self) -> dict:
        return {"raw_count": 0, "processed_count": 0, "wiki_count": 0}

    def commonplace_import_files(self, paths: list[str]) -> dict:
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
        self.assertEqual(data["app_title"], "Commonplace")

    def test_health_endpoint_responds(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/health")
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])

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
            "/api/upload/commonplace_import_files",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
