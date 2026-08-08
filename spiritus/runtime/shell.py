"""
Desktop shell: the generic PyWebView window + local UI HTTP server + lifecycle.

This is the Core entry point an application calls via ``spiritus.run(config)``.
It is application-independent: the only app-specific input is the ``AppConfig``.
"""
from __future__ import annotations

import atexit
import email
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from ..config import AppConfig
from . import paths
from .server import OpenCodeServer
from .subproc import dispatch_child


def _parse_multipart_files(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """Extract the ``files`` parts of a multipart/form-data body.

    Returns ``[(filename, content), ...]`` in document order. Built on the
    stdlib ``email`` parser rather than ``cgi.FieldStorage``, which Python 3.13
    removed; keeping this in Core means apps do not need a multipart dependency.
    Malformed bodies yield an empty list — the caller answers 400.
    """
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    try:
        message = email.message_from_bytes(header + body)
    except Exception:
        return []
    if not message.is_multipart():
        return []

    out: list[tuple[str, bytes]] = []
    for part in message.get_payload():
        if part.get_param("name", header="content-disposition") != "files":
            continue
        out.append((part.get_filename() or "", part.get_payload(decode=True) or b""))
    return out


def _load_env(config: AppConfig):
    """Load .env into os.environ before anything else reads it."""
    for env_path in paths.env_candidates(config.app_root, config.app_id):
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        v = v.strip().strip('"').strip("'")
                        os.environ.setdefault(k, v)
            break


def _set_app_name(title: str):
    """Best-effort: set the macOS menu-bar app name in dev runs."""
    if paths.is_bundled():
        return
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = title
        info["CFBundleDisplayName"] = title
    except Exception:
        pass


def _make_ui_handler(ui_dir: str, bridge):
    class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=ui_dir, **kwargs)

        def end_headers(self):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            super().end_headers()

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/api/health":
                return self._write_json({"ok": True})
            return super().do_GET()

        def do_POST(self):
            if self.path.startswith("/api/bridge/"):
                return self._handle_bridge_call()
            if self.path.startswith("/api/upload/"):
                return self._handle_file_upload()
            self.send_error(404)

        def _handle_bridge_call(self):
            method = self.path.split("/api/bridge/", 1)[1].split("?", 1)[0]
            target = self._resolve_method(method)
            if target is None:
                return self._write_json({"ok": False, "error": f"Unknown bridge method: {method}"}, status=404)

            try:
                payload = self._read_json()
                args = payload.get("args", [])
                result = target(*args)
                return self._write_json(result)
            except Exception as exc:
                return self._write_json({"ok": False, "error": str(exc)}, status=500)

        def _handle_file_upload(self):
            method = self.path.split("/api/upload/", 1)[1].split("?", 1)[0]
            target = self._resolve_method(method)
            if target is None:
                return self._write_json({"ok": False, "error": f"Unknown upload method: {method}"}, status=404)

            content_type = self.headers.get("content-type", "")
            if "multipart/form-data" not in content_type:
                return self._write_json({"ok": False, "error": "Expected multipart/form-data"}, status=400)

            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                return self._write_json({"ok": False, "error": "Invalid Content-Length"}, status=400)

            uploads = _parse_multipart_files(self.rfile.read(length) if length > 0 else b"",
                                             content_type)
            if not uploads:
                return self._write_json({"ok": False, "error": "No files uploaded"}, status=400)

            with tempfile.TemporaryDirectory(prefix="spiritus-upload-") as tmpdir:
                paths_to_import: list[str] = []
                for index, (raw_name, content) in enumerate(uploads):
                    # Basename only: a part may claim "../../etc/passwd" as its
                    # filename, and the temp dir must stay the write boundary.
                    filename = Path(raw_name or f"upload-{index}").name or f"upload-{index}"
                    destination = Path(tmpdir) / filename
                    destination.write_bytes(content)
                    paths_to_import.append(str(destination))
                try:
                    result = target(paths_to_import)
                except Exception as exc:
                    return self._write_json({"ok": False, "error": str(exc)}, status=500)
            return self._write_json(result)

        def _resolve_method(self, name: str):
            if not bridge or not name or name.startswith("_"):
                return None
            candidate = getattr(bridge, name, None)
            return candidate if callable(candidate) else None

        def _read_json(self) -> dict:
            length = int(self.headers.get("content-length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _write_json(self, payload: dict | list, status: int = 200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return NoCacheHandler


def _start_ui_server(ui_dir: str, bridge) -> int:
    """Serve the Core UI over http://127.0.0.1 with no-cache headers.

    Serving over HTTP (not file://) makes WKWebView apply standard CORS to the
    UI's fetch() calls to OpenCode, and no-cache guarantees fresh assets.

    Threaded so a slow bridge call (a URL fetch, a long extraction, an app's
    background job kickoff) cannot stall unrelated requests — static assets,
    the health check, and other bridge calls keep flowing. Each request runs
    on its own daemon thread; apps own any shared-state locking they need.
    """
    NoCacheHandler = _make_ui_handler(ui_dir, bridge)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoCacheHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def run(config: AppConfig):
    """Boot a Spiritus application. Blocks until the window is closed."""
    # Before anything else, and before a window exists: in a frozen build this
    # process may be a child Core spawned to run a snippet, not the app. It has
    # to find that out here — the alternative is booting a second copy of the
    # application, which spawns a third. See runtime/subproc.py.
    dispatch_child()

    import webview  # imported late so non-GUI tooling can import spiritus cleanly

    from ..bridge import Bridge  # late import to avoid a cycle

    _load_env(config)
    _set_app_name(config.app_title)

    proot = paths.project_root(config.app_root, config.app_id)
    opencode = OpenCodeServer(proot, port_env_var=config.env_port_var)
    try:
        opencode.start()
    except RuntimeError as e:
        print(f"[spiritus] WARNING: {e}", file=sys.stderr)
        print("[spiritus] Continuing without OpenCode — chat will not function.",
              file=sys.stderr)

    atexit.register(opencode.stop)  # covers Ctrl+C and any non-SIGKILL exit

    bridge_cls = config.bridge_cls or Bridge
    bridge = bridge_cls(config, opencode)

    # Serve the app's own UI if it provides one; otherwise the shared chat UI.
    ui_dir = str(config.ui_dir) if config.ui_dir else str(paths.resource_path("ui"))
    ui_port = _start_ui_server(ui_dir, bridge)

    window = webview.create_window(
        title=config.app_title,
        url=f"http://127.0.0.1:{ui_port}/index.html",
        js_api=bridge,
        width=config.window_size[0],
        height=config.window_size[1],
        min_size=config.min_size,
    )
    window.events.closed += lambda: opencode.stop()
    try:
        webview.start(debug=False)
    finally:
        opencode.stop()  # catches KeyboardInterrupt / abrupt exits that skip events.closed
