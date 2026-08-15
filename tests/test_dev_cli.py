"""Contracts for the local diagnostics launcher."""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

from spiritus.__main__ import main as cli_main
from spiritus.dev import _DiagnosticsServer, _display_record
from spiritus.tracing import (
    Diagnostics,
    FailureKind,
    RunFailure,
    RunStore,
    TraceKind,
    TraceRenderer,
    TraceStore,
)


def _child_script(path: Path, *, crash: bool = False) -> None:
    script = """
import json
import os
import socket
from datetime import UTC, datetime

endpoint = os.environ['SPIRITUS_DIAGNOSTICS_ENDPOINT']
host, port = endpoint.rsplit(':', 1)
payload = {
    'token': os.environ['SPIRITUS_DIAGNOSTICS_TOKEN'],
    'record': {
        'id': 'trc_child', 'time': datetime.now(UTC).isoformat(),
        'kind': 'run.started', 'run_id': 'run_child', 'session_id': 'ses_child',
        'agent': 'probe', 'model': '', 'message_id': '', 'call_id': '',
        'failure_layer': None, 'data': {'prompt': 'private input'},
    },
}
with socket.create_connection((host, int(port))) as connection:
    connection.sendall(json.dumps(payload).encode('utf-8'))
print('child stdout')
print('child stderr', file=__import__('sys').stderr)
"""
    if crash:
        script += "raise RuntimeError('child crash')\n"
    path.write_text(script, encoding="utf-8")


def test_dev_forwards_child_output_renders_live_journal_and_retains_launch(tmp_path, capsys):
    entrypoint = tmp_path / "run.py"
    _child_script(entrypoint)

    assert cli_main(["dev", "--no-color", str(entrypoint)]) == 0

    captured = capsys.readouterr()
    assert "child stdout" in captured.out
    assert "child stderr" in captured.err
    assert "RUN.STARTED" in captured.err
    assert "private input" not in captured.err
    launch = next((tmp_path / ".spiritus" / "launches").glob("launch_*.json"))
    assert json.loads(launch.read_text(encoding="utf-8"))["status"] == "completed"


def test_dev_records_a_process_crash_with_the_child_traceback(tmp_path):
    entrypoint = tmp_path / "run.py"
    _child_script(entrypoint, crash=True)

    assert cli_main(["dev", "--no-color", str(entrypoint)]) != 0

    launches = list((tmp_path / ".spiritus" / "launches").glob("launch_*.json"))
    launch = json.loads(launches[0].read_text(encoding="utf-8"))
    assert launch["status"] == "failed"
    assert "RuntimeError" in launch["stderr_tail"]
    assert "child crash" in launch["stderr_tail"]
    assert TraceStore(tmp_path / ".spiritus").entries()[-1].kind is TraceKind.RUNTIME_PROCESS_CRASHED
    assert TraceStore(tmp_path / ".spiritus").entries()[-1].launch_id == launch["launch_id"]


def test_trace_store_publishes_only_after_recording_to_the_live_subscriber(monkeypatch, tmp_path):
    events = queue.Queue()
    server = _DiagnosticsServer("one-time-token", events)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    monkeypatch.setenv("SPIRITUS_DIAGNOSTICS_ENDPOINT", f"{host}:{port}")
    monkeypatch.setenv("SPIRITUS_DIAGNOSTICS_TOKEN", "one-time-token")
    try:
        stored = TraceStore(tmp_path / ".spiritus").append(
            TraceKind.RUN_STARTED, run_id="run_probe", session_id="ses_probe"
        )
        received = events.get(timeout=1)
    finally:
        server.shutdown()
        server.server_close()
    assert received == stored
    assert TraceStore(tmp_path / ".spiritus").entries() == [stored]


def test_normal_terminal_failure_includes_the_durable_kind_owner_and_error(tmp_path):
    runs = RunStore(tmp_path / ".spiritus")
    runs.create(
        run_id="run_failure", operation="profile.import", session_id="ses_failure",
        agent="profile-pdf", model="opencode/nemotron",
    )
    runs.fail(
        "run_failure",
        RunFailure(
            FailureKind.MODEL_FAILED,
            "model",
            "Streaming response failed: [502] Upstream error from Nvidia: Internal server error",
        ),
    )
    trace = TraceStore(tmp_path / ".spiritus").append(
        TraceKind.RUN_FAILED, run_id="run_failure", session_id="ses_failure"
    )

    rendered = TraceRenderer(color=False).render(_display_record(trace, "normal", runs))

    assert "kind=model_failed" in rendered
    assert "owner=model" in rendered
    assert 'error="Streaming response failed: [502] Upstream error from Nvidia: Internal server error"' in rendered


def test_diagnostics_facade_shares_run_trace_and_artifact_storage(tmp_path):
    diagnostics = Diagnostics(tmp_path / ".spiritus")
    diagnostics.runs.create(
        run_id="run_artifact", operation="profile.import", session_id="ses_artifact",
        agent="profile-pdf", model="opencode/test",
    )
    diagnostics.runs.complete("run_artifact", artifacts={"agent.output": "{}"})

    assert diagnostics.get("run_artifact").status.value == "completed"
    assert diagnostics.artifact("run_artifact", "agent.output") == "{}"
    assert (
        tmp_path / ".spiritus" / "artifacts" / "run_artifact" / "agent.output.json"
    ).is_file()
