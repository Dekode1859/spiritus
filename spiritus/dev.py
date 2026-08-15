"""Local developer launcher for rendering the production diagnostics journal."""
from __future__ import annotations

import json
import os
import queue
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from .tracing import RunStore, TraceKind, TraceRecord, TraceRenderer, TraceStore


def _time() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(body, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _DiagnosticsServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, token: str, events: queue.Queue[TraceRecord]):
        self.token = token
        self.events = events
        super().__init__(("127.0.0.1", 0), _DiagnosticsHandler)


class _DiagnosticsHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        chunks = []
        while True:
            chunk = self.request.recv(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 1_000_000:
                return
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
            if not secrets.compare_digest(str(payload.get("token", "")), self.server.token):
                return
            self.server.events.put(TraceRecord.from_store(payload["record"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return


def _display_record(
    record: TraceRecord, level: str, runs: RunStore | None = None
) -> TraceRecord | None:
    if level == "normal":
        if record.kind not in {
            TraceKind.RUN_STARTED,
            TraceKind.RUN_CHECKPOINT,
            TraceKind.MODEL_REQUESTED,
            TraceKind.MODEL_COMPLETED,
            TraceKind.RUN_COMPLETED,
            TraceKind.RUN_FAILED,
            TraceKind.RUNTIME_PROCESS_CRASHED,
        }:
            return None
        if record.kind is TraceKind.RUN_CHECKPOINT:
            return replace(record, data={"name": record.data.get("name", "checkpoint")})
        if record.kind is TraceKind.MODEL_REQUESTED:
            prompt = record.data.get("prompt")
            return replace(
                record,
                data={"input_chars": len(prompt)} if isinstance(prompt, str) else {},
            )
        if record.kind is TraceKind.MODEL_COMPLETED:
            data = {}
            if "text_length" in record.data:
                data["output_chars"] = record.data["text_length"]
            if "structured" in record.data:
                data["structured"] = record.data["structured"]
            return replace(record, data=data)
        if runs is not None and record.kind in {
            TraceKind.RUN_STARTED,
            TraceKind.RUN_COMPLETED,
        }:
            try:
                run = runs.get(record.run_id)
            except (KeyError, ValueError):
                run = None
            if run is not None:
                data = {"operation": run.operation}
                if run.completed_at:
                    started = datetime.fromisoformat(run.started_at)
                    completed = datetime.fromisoformat(run.completed_at)
                    data["duration_ms"] = max(
                        0, round((completed - started).total_seconds() * 1000)
                    )
                if record.kind is TraceKind.RUN_COMPLETED:
                    output = run.artifacts.get("agent.output")
                    if output is not None:
                        data["output_chars"] = len(str(output))
                return replace(record, data=data)
        if record.kind is TraceKind.RUN_FAILED and runs is not None:
            try:
                failure = runs.get(record.run_id).failure
            except (KeyError, ValueError):
                failure = None
            if failure is not None:
                data = {
                    "kind": failure.kind.value,
                    "owner": failure.owner,
                    "error": failure.message,
                }
                if failure.field_paths:
                    data["field_paths"] = list(failure.field_paths)
                return replace(record, data=data)
        return replace(record, data={})
    if level == "verbose":
        return replace(record, data={key: _summary(value) for key, value in record.data.items()})
    return record


def _summary(value: object, *, limit: int = 180) -> object:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    if isinstance(value, dict):
        return {key: _summary(item, limit=80) for key, item in value.items()}
    if isinstance(value, list):
        return [_summary(item, limit=80) for item in value[:8]] + (["…"] if len(value) > 8 else [])
    return value


def _forward(stream: TextIO | None, target: TextIO, messages: queue.Queue[tuple[TextIO, str]]) -> None:
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        messages.put((target, line))
    stream.close()


def cmd_dev(args) -> int:
    """Run an application child while rendering its durable diagnostics live."""
    entrypoint = Path(args.entrypoint).resolve()
    if not entrypoint.is_file():
        print(f"error: entrypoint does not exist: {entrypoint}", file=sys.stderr)
        return 1
    root = Path(args.project_root).resolve() if args.project_root else entrypoint.parent
    launch_id = f"launch_{uuid.uuid4().hex}"
    launch_path = root / ".spiritus" / "launches" / f"{launch_id}.json"
    events: queue.Queue[TraceRecord] = queue.Queue()
    output: queue.Queue[tuple[TextIO, str]] = queue.Queue()
    token = secrets.token_urlsafe(32)
    server = _DiagnosticsServer(token, events)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    command = [sys.executable, str(entrypoint), *args.entrypoint_args]
    launch = {
        "launch_id": launch_id,
        "entrypoint": str(entrypoint),
        "command": command,
        "started_at": _time(),
        "status": "running",
    }
    _write_json(launch_path, launch)
    environment = os.environ.copy()
    environment.update({
        "SPIRITUS_DIAGNOSTICS_ENDPOINT": f"{host}:{port}",
        "SPIRITUS_DIAGNOSTICS_TOKEN": token,
        "SPIRITUS_DIAGNOSTICS_LAUNCH_ID": launch_id,
    })
    renderer = TraceRenderer(color=not args.no_color)
    runs = RunStore(root / ".spiritus")
    stderr_lines: list[str] = []
    try:
        child = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        launch["pid"] = child.pid
        _write_json(launch_path, launch)
        readers = [
            threading.Thread(target=_forward, args=(child.stdout, sys.stdout, output), daemon=True),
            threading.Thread(target=_forward, args=(child.stderr, sys.stderr, output), daemon=True),
        ]
        for reader in readers:
            reader.start()
        while child.poll() is None or any(reader.is_alive() for reader in readers):
            _drain(output, stderr_lines)
            _drain_events(events, renderer, args.level, runs)
            time.sleep(0.02)
        for reader in readers:
            reader.join(timeout=1)
        _drain(output, stderr_lines)
        _drain_events(events, renderer, args.level, runs)
        code = child.returncode
        launch.update({"completed_at": _time(), "status": "completed" if code == 0 else "failed", "exit_code": code})
        if code:
            tail = "".join(stderr_lines[-30:])
            launch["stderr_tail"] = tail
            record = TraceStore(root / ".spiritus").append(
                TraceKind.RUNTIME_PROCESS_CRASHED,
                run_id=launch_id,
                session_id="",
                launch_id=launch_id,
                data={"exit_code": code, "traceback": tail},
            )
            displayed = _display_record(record, args.level, runs)
            if displayed is not None:
                print(renderer.render(displayed), file=sys.stderr)
        _write_json(launch_path, launch)
        return code
    except BaseException:
        launch.update({"completed_at": _time(), "status": "launcher_failed", "traceback": traceback.format_exc()})
        _write_json(launch_path, launch)
        raise
    finally:
        server.shutdown()
        server.server_close()


def _drain(output: queue.Queue[tuple[TextIO, str]], stderr_lines: list[str]) -> None:
    while True:
        try:
            target, line = output.get_nowait()
        except queue.Empty:
            return
        target.write(line)
        target.flush()
        if target is sys.stderr:
            stderr_lines.append(line)


def _drain_events(
    events: queue.Queue[TraceRecord], renderer: TraceRenderer, level: str, runs: RunStore
) -> None:
    while True:
        try:
            record = events.get_nowait()
        except queue.Empty:
            return
        displayed = _display_record(record, level, runs)
        if displayed is not None:
            print(renderer.render(displayed), file=sys.stderr)
