"""
Desktop shell: the generic PyWebView window + local UI HTTP server + lifecycle.

This is the Spiritus entry point an application calls via ``spiritus.run(config)``.
It is application-independent: the only application-specific input is
``AppConfig``.
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
from urllib.parse import parse_qs, urlparse

from ..config import AppConfig
from . import paths
from .lifecycle import ShutdownCoordinator
from .server import OpenCodeServer
from .subproc import dispatch_child
from .window import WindowController


def _parse_multipart_files(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """Extract the ``files`` parts of a multipart/form-data body.

    Returns ``[(filename, content), ...]`` in document order. Built on the
    stdlib ``email`` parser rather than ``cgi.FieldStorage``, which Python 3.13
    removed; keeping this in Spiritus means applications do not need a
    multipart dependency.
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
            if self.path.startswith("/api/events"):
                return self._handle_event_stream()
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

        def _handle_event_stream(self):
            target = self._resolve_method("session_events")
            query = parse_qs(urlparse(self.path).query)
            session_id = (query.get("session_id") or [""])[0]
            if target is None or not session_id:
                return self._write_json(
                    {"ok": False, "error": "session_id is required"}, status=400
                )

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for event in target(session_id):
                    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
                    self.wfile.write(b"data: " + body + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

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


class _UiServer:
    """Handle for the HTTP server owned by the Spiritus desktop shell."""

    def __init__(self, server: http.server.ThreadingHTTPServer, thread: threading.Thread):
        self.server = server
        self.thread = thread
        self.port = server.server_address[1]
        self._stopped = False
        self._lock = threading.Lock()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _start_ui_server(ui_dir: str, bridge) -> _UiServer:
    """Serve the Spiritus UI over http://127.0.0.1 with no-cache headers.

    Serving over HTTP (not file://) makes WKWebView apply standard CORS to the
    UI's fetch() calls to OpenCode, and no-cache guarantees fresh assets.

    Threaded so a slow bridge call (a URL fetch, a long extraction, an app's
    background job kickoff) cannot stall unrelated requests — static assets,
    the health check, and other bridge calls keep flowing. Each request runs
    on its own daemon thread; applications own any shared-state locking they need.
    """
    NoCacheHandler = _make_ui_handler(ui_dir, bridge)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoCacheHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _UiServer(server, thread)


def run(config: AppConfig):
    """Boot a Spiritus application. Blocks until the window is closed."""
    # Before anything else, and before a window exists: in a frozen build this
    # process may be a child Spiritus process spawned to run a snippet, not the
    # application. It has
    # to find that out here — the alternative is booting a second copy of the
    # application, which spawns a third. See runtime/subproc.py.
    dispatch_child()

    # Preserve the established AppConfig entry point while allowing the new
    # declarative App to use the same shell and bridge.
    from ..app import App

    tool_server = None
    if isinstance(config, App):
        config.compile()
        if config.tools:
            from ..tools import ToolServer

            tool_server = ToolServer(config.tools)
        config = config.to_config()
    if not isinstance(config, AppConfig):
        raise TypeError("run() expects an App or AppConfig")

    import webview  # imported late so non-GUI tooling can import spiritus cleanly

    from ..bridge import Bridge  # late import to avoid a cycle

    lifecycle = ShutdownCoordinator()
    atexit.register(lifecycle.stop_once)  # covers non-SIGKILL process exits

    _load_env(config)
    _set_app_name(config.app_title)

    proot = paths.project_root(config.app_root, config.app_id)
    tool_environment = tool_server.start() if tool_server is not None else {}
    if tool_server is not None:
        lifecycle.add(tool_server.stop)
    opencode = OpenCodeServer(
        proot,
        port_env_var=config.env_port_var,
        environment=tool_environment,
    )
    lifecycle.add(opencode.stop)

    try:
        opencode.start()
    except RuntimeError as e:
        print(f"[spiritus] WARNING: {e}", file=sys.stderr)
        print("[spiritus] Continuing without OpenCode — chat will not function.",
              file=sys.stderr)

    bridge_cls = config.bridge_cls or Bridge
    bridge = bridge_cls(config, opencode)

    # Serve the app's own UI if it provides one; otherwise the shared chat UI.
    ui_dir = str(config.ui_dir) if config.ui_dir else str(paths.resource_path("ui"))
    ui_server = _start_ui_server(ui_dir, bridge)
    ui_stop = getattr(ui_server, "stop", None)
    if ui_stop is not None:
        lifecycle.add(ui_stop)
    ui_port = ui_server.port if hasattr(ui_server, "port") else ui_server

    window = webview.create_window(
        title=config.app_title,
        url=f"http://127.0.0.1:{ui_port}/index.html",
        js_api=bridge,
        **config.resolved_window().create_window_kwargs(),
    )
    if window is None:
        raise RuntimeError("PyWebView did not create the application window")
    window_controller = WindowController(window)
    attach_window = getattr(bridge, "attach_window", None)
    if attach_window is not None:
        attach_window(window_controller)
    window_controller.on("closed", lifecycle.stop_once)
    try:
        webview.start(**config.resolved_webview().start_kwargs())
    finally:
        lifecycle.stop_once()  # catches KeyboardInterrupt / missed window events
