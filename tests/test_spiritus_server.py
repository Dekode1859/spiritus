"""Engine process lifecycle.

The property under test: when the app stops the engine, nothing the engine
spawned survives. Regression for orphaned `opencode.exe` processes — the
launcher on PATH is a wrapper that execs the real binary as a grandchild, so
terminating the direct child left the engine running and holding its port.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from spiritus.runtime.server import OpenCodeServer

# Parent spawns a long-lived grandchild, prints its pid, then sleeps. Killing
# only the parent is precisely the bug; killing the tree takes both.
PARENT_SRC = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, flush=True)
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def server(tmp_path):
    return OpenCodeServer(tmp_path)


def test_stop_kills_grandchildren_not_just_the_direct_child(server, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", PARENT_SRC],
        stdout=subprocess.PIPE, text=True,
        start_new_session=(os.name != "nt"),
    )
    grandchild_pid = int(proc.stdout.readline().strip())
    assert _alive(grandchild_pid), "grandchild should be running before stop()"

    server._process = proc
    server.stop()

    assert _wait_gone(proc.pid), "the direct child survived stop()"
    assert _wait_gone(grandchild_pid), (
        f"grandchild {grandchild_pid} outlived stop() — the engine would be "
        "orphaned, holding its port, on every app exit"
    )


# Binds a child to the server's lifetime, reports whether the binding took and
# the child's pid, then waits to be killed. Nothing here cooperates on
# shutdown — that is the point.
BOUND_CHILD_SRC = """
import subprocess, sys, tempfile, time
from spiritus.runtime.server import OpenCodeServer
srv = OpenCodeServer(tempfile.mkdtemp(prefix="bound-"))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
srv._bind_to_lifetime(child)
print(f"{child.pid} {'bound' if srv._job else 'unbound'}", flush=True)
time.sleep(120)
"""


@pytest.mark.skipif(os.name != "nt", reason="Job Objects are a Windows mechanism")
def test_engine_dies_even_when_the_app_is_force_killed():
    """A force quit runs no atexit, no finally, no signal handler.

    Without an OS-level binding the engine survives, reparented and holding its
    port — the exact orphan seen in the wild.

    The binding is best effort by design: a host that already confines us to a
    Job Object forbidding nesting (some CI runners, some sandboxes) will refuse
    it, and Spiritus falls back to cooperative shutdown. Where that happens there is
    nothing to assert, so the test says so rather than failing or pretending.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", BOUND_CHILD_SRC],
        stdout=subprocess.PIPE, text=True,
    )
    child_pid_text, _, state = proc.stdout.readline().strip().partition(" ")
    child_pid = int(child_pid_text)
    assert _alive(child_pid)

    if state != "bound":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
        pytest.skip("this host refused the Job Object; binding is best effort")

    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                   capture_output=True, check=False)

    assert _wait_gone(child_pid), (
        f"child {child_pid} outlived a force kill of its parent — "
        "the Job Object binding is not in effect"
    )


def test_binding_is_best_effort_and_never_breaks_startup(server, monkeypatch):
    """A restricted token or a nested job must degrade, not crash the app."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=(os.name != "nt"),
    )
    try:
        if os.name == "nt":
            import ctypes
            monkeypatch.setattr(ctypes, "WinDLL",
                                lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        server._bind_to_lifetime(proc)      # must not raise
        assert server._job is None
    finally:
        server._process = proc
        server.stop()


def test_stop_is_idempotent(server, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=(os.name != "nt"),
    )
    server._process = proc
    server.stop()
    server.stop()          # must not raise on an already-stopped engine
    assert _wait_gone(proc.pid)


def test_stop_without_a_running_engine_is_a_no_op(server):
    server.stop()
    assert server.port is None


def test_stop_clears_the_port_so_callers_cannot_use_a_dead_engine(server):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=(os.name != "nt"),
    )
    server._process = proc
    server._port = 12345
    server.stop()
    assert server.port is None


def test_home_dir_is_project_local_not_the_user_home(server, tmp_path):
    """Credential isolation: each app keeps its own provider auth."""
    assert server.home_dir == tmp_path / ".opencode-home"
    assert server.home_dir.is_dir()


def test_engine_environment_isolates_every_persistent_path(server, tmp_path):
    env = server._engine_environment()
    home = tmp_path / ".opencode-home"

    assert env["HOME"] == str(home)
    assert env["USERPROFILE"] == str(home)
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")
    assert env["XDG_STATE_HOME"] == str(home / ".local" / "state")


def test_engine_environment_does_not_mutate_the_parent_process(server, monkeypatch):
    monkeypatch.setenv("HOME", "parent-home")
    monkeypatch.setenv("USERPROFILE", "parent-profile")

    isolated = server._engine_environment()

    assert isolated["HOME"] != "parent-home"
    assert isolated["USERPROFILE"] != "parent-profile"
    assert os.environ["HOME"] == "parent-home"
    assert os.environ["USERPROFILE"] == "parent-profile"


def test_start_reports_a_clear_error_when_the_engine_is_absent(server, monkeypatch):
    """The failure must name the command that fixes it, not just the symptom."""
    from spiritus import engine

    monkeypatch.setattr(engine, "resolve", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="spiritus install-engine"):
        server.start()


class _Launched(Exception):
    """Raised in place of actually spawning an engine."""


def test_start_warns_but_still_launches_an_out_of_range_engine(server, monkeypatch, capsys):
    """Version drift is advisory: untested is not the same as known-broken.

    Reaching the launch proves the warning did not abort startup.
    """
    from spiritus import engine
    from spiritus.runtime import server as server_mod

    monkeypatch.setattr(engine, "resolve", lambda *a, **k: Path("engine-binary"))
    monkeypatch.setattr(engine, "binary_version", lambda _: "99.0.0")
    monkeypatch.setattr(server_mod.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(_Launched()))

    with pytest.raises(_Launched):
        server.start()
    assert "newer than the Spiritus version tested against" in capsys.readouterr().err


def test_start_records_the_engine_version_it_launched(server, monkeypatch):
    from spiritus import engine
    from spiritus.runtime import server as server_mod

    monkeypatch.setattr(engine, "resolve", lambda *a, **k: Path("engine-binary"))
    monkeypatch.setattr(engine, "binary_version", lambda _: engine.PINNED_VERSION)
    monkeypatch.setattr(server_mod.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(_Launched()))

    assert server.engine_version is None
    with pytest.raises(_Launched):
        server.start()
    assert server.engine_version == engine.PINNED_VERSION
