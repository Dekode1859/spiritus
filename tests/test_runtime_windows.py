from __future__ import annotations

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
