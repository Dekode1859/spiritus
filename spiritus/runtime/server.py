"""
OpenCode `serve` subprocess lifecycle.

Spiritus hosts OpenCode as the execution engine (agents, tools, sessions,
events). This module only manages the process: start on a port, isolate HOME,
poll until ready, stop. It has no knowledge of agents or domains.

OpenCode is launched with its home and XDG directories pointed at
<project>/.opencode-home so that provider credentials, sessions, caches, and
config are isolated from the user's global OpenCode installation per
application. Windows needs USERPROFILE as well as HOME because the engine's
runtime resolves its home directory from USERPROFILE there.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import requests

from .. import engine


class OpenCodeServer:
    """One running ``opencode serve`` instance, scoped to a project directory."""

    def __init__(
        self,
        project_root: Path,
        port_env_var: str = "OPENCODE_PORT",
        *,
        environment: Mapping[str, str] | None = None,
    ):
        self._project_root = Path(project_root)
        self._port_env_var = port_env_var
        self._process: subprocess.Popen | None = None
        self._port: int | None = None
        self._engine_version: str | None = None
        self._environment = dict(environment or {})
        self._job = None      # Windows Job Object handle; see _bind_to_lifetime

    @property
    def home_dir(self) -> Path:
        d = self._project_root / ".opencode-home"
        d.mkdir(exist_ok=True)
        return d

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _engine_environment(self) -> dict[str, str]:
        """Return an environment that keeps all OpenCode state app-local.

        Merely setting ``HOME`` is insufficient on Windows: Bun's home
        resolution uses ``USERPROFILE`` and silently falls back to the real
        user's global OpenCode directories. Explicit XDG paths make the data,
        config, cache, and state locations deterministic on every platform and
        keep them aligned with :mod:`spiritus.providers`.
        """
        env = os.environ.copy()
        home = self.home_dir
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        })
        env.update(self._environment)
        env.setdefault("PATH", os.environ.get("PATH", ""))
        return env

    def set_environment(self, values: Mapping[str, str]) -> None:
        """Add adapter-owned environment values before the next start."""
        if self._process is not None:
            raise RuntimeError("cannot change the OpenCode environment while running")
        self._environment.update({str(key): str(value) for key, value in values.items()})

    def start(self) -> int:
        opencode_bin = engine.resolve()
        if not opencode_bin:
            raise RuntimeError(engine.missing_engine_message())

        version = engine.binary_version(opencode_bin)
        warning = engine.version_warning(version)
        if warning:
            print(f"[spiritus] WARNING: {warning}", file=sys.stderr)
        self._engine_version = version

        configured = int(os.environ.get(self._port_env_var, "0"))
        port = configured if configured > 0 else self._find_free_port()

        env = self._engine_environment()

        self._process = subprocess.Popen(
            [opencode_bin, "serve", "--port", str(port)],
            cwd=str(self._project_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # POSIX: put the engine in its own process group so stop() can
            # signal the whole group. Windows gets tree-killed by PID instead.
            start_new_session=(os.name != "nt"),
        )
        self._bind_to_lifetime(self._process)

        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                r = requests.get(f"http://127.0.0.1:{port}/session", timeout=1)
                if r.status_code == 200:
                    self._port = port
                    return port
            except Exception:
                pass
            time.sleep(0.3)

        self.stop()
        raise RuntimeError(
            f"opencode server did not become ready on port {port} within 20 seconds"
        )

    def _bind_to_lifetime(self, proc: subprocess.Popen) -> None:
        """Windows: tie the engine's lifetime to this process at the OS level.

        ``stop()`` handles every exit that runs Python cleanup. A force quit —
        Task Manager, ``taskkill /F``, a crash — runs none of it, and the engine
        survives holding its port. A Job Object with KILL_ON_JOB_CLOSE closes
        that gap: when this process dies for any reason, the kernel terminates
        everything in the job.

        Best effort. If the job cannot be created (an existing job that
        disallows nesting, a restricted token), the engine simply keeps the
        cooperative-shutdown behavior it already had.
        """
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.OpenProcess.restype = wintypes.HANDLE

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return

            info = EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                k32.CloseHandle(job)
                return

            handle = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
            if not handle:
                k32.CloseHandle(job)
                return
            assigned = k32.AssignProcessToJobObject(job, handle)
            k32.CloseHandle(handle)
            if not assigned:
                k32.CloseHandle(job)
                return

            # Held for this process's lifetime: closing the handle is what
            # triggers the kill, so it must outlive every normal code path.
            self._job = job
        except Exception:
            pass

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        """Kill the engine and everything it spawned.

        Terminating the direct child is not enough. The launcher on PATH is
        frequently a wrapper — an npm shim on Windows, a shell script
        elsewhere — that execs the real binary as a *grandchild*. Killing the
        wrapper leaves the engine running, reparented and unreachable, holding
        its port. Every app exit would leak one.
        """
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False,
            )
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()   # group already gone, or we never got one

    def stop(self):
        proc, self._process = self._process, None
        self._port = None
        if proc is None or proc.poll() is not None:
            return
        try:
            self._kill_tree(proc)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def engine_version(self) -> str | None:
        """Version of the engine binary that was launched, if it reported one."""
        return self._engine_version
