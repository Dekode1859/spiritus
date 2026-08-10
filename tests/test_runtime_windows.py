from __future__ import annotations

from types import SimpleNamespace

from spiritus.runtime import windows


def _fake_windows(monkeypatch):
    monkeypatch.setattr(windows.os, "name", "nt")
    monkeypatch.setattr(windows.subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(windows.subprocess, "DETACHED_PROCESS", 0x08, raising=False)
    monkeypatch.setattr(windows.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(windows.subprocess, "STARTF_USESHOWWINDOW", 0x01, raising=False)
    monkeypatch.setattr(windows.subprocess, "SW_HIDE", 0, raising=False)

    class StartupInfo:
        dwFlags = 0
        wShowWindow = 1

    monkeypatch.setattr(windows.subprocess, "STARTUPINFO", StartupInfo, raising=False)


def test_hidden_console_options_are_empty_off_windows(monkeypatch):
    monkeypatch.setattr(windows.os, "name", "posix")

    assert windows.hidden_console_kwargs() == {}


def test_hidden_console_options_add_flags_and_hide_startup(monkeypatch):
    _fake_windows(monkeypatch)

    options = windows.hidden_console_kwargs()

    assert options["creationflags"] == 0x08000000
    assert options["startupinfo"].wShowWindow == 0
    assert options["startupinfo"].dwFlags == 0x01


def test_hidden_console_options_preserve_explicit_console_choice(monkeypatch):
    _fake_windows(monkeypatch)

    options = windows.hidden_console_kwargs(creationflags=0x10)

    assert options == {"creationflags": 0x10}


def test_windows_engine_tree_shutdown_uses_hidden_console_options(monkeypatch, tmp_path):
    from spiritus.runtime import server as server_module
    from spiritus.runtime.server import OpenCodeServer

    calls = {}
    monkeypatch.setattr(server_module.os, "name", "nt")
    monkeypatch.setattr(
        server_module,
        "hidden_console_kwargs",
        lambda: {"creationflags": 0x08000000, "startupinfo": "hidden"},
    )

    def fake_run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs

    monkeypatch.setattr(server_module.subprocess, "run", fake_run)
    OpenCodeServer(tmp_path)._kill_tree(SimpleNamespace(pid=1234))

    assert calls == {
        "command": ["taskkill", "/F", "/T", "/PID", "1234"],
        "kwargs": {
            "capture_output": True,
            "check": False,
            "creationflags": 0x08000000,
            "startupinfo": "hidden",
        },
    }
