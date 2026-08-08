"""Spawning a fresh Python process from a frozen build.

Unfrozen, `[sys.executable, "-c", code]` is correct and these helpers are a
pass-through. Frozen, that command re-runs the application: the bootloader
ignores `-c`, so the "child" is a second copy of the app, which reaches the same
call site and spawns a third. It never raises — the parent blocks on a pipe
while processes multiply — so only assertions catch a regression here.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from spiritus.runtime import subproc


@pytest.fixture
def frozen(monkeypatch):
    """Make is_bundled() report a PyInstaller bundle."""
    monkeypatch.setattr(subproc, "is_bundled", lambda: True)


class TestPythonC:
    def test_unfrozen_is_the_plain_interpreter_form(self, monkeypatch):
        monkeypatch.setattr(subproc, "is_bundled", lambda: False)
        assert subproc.python_c("print(1)") == [sys.executable, "-c", "print(1)"]

    def test_unfrozen_appends_arguments(self, monkeypatch):
        monkeypatch.setattr(subproc, "is_bundled", lambda: False)
        cmd = subproc.python_c("code", "a", "b")
        assert cmd == [sys.executable, "-c", "code", "a", "b"]

    def test_frozen_never_passes_dash_c(self, frozen):
        """`-c` is what the bootloader ignores, and ignoring it is the bug."""
        assert "-c" not in subproc.python_c("print(1)")

    def test_frozen_uses_the_sentinel_flag(self, frozen):
        cmd = subproc.python_c("print(1)")
        assert cmd == [sys.executable, subproc.CHILD_FLAG, "print(1)"]

    def test_frozen_appends_arguments_after_the_code(self, frozen):
        cmd = subproc.python_c("code", "a", 2)
        assert cmd == [sys.executable, subproc.CHILD_FLAG, "code", "a", "2"]

    def test_arguments_are_stringified(self, frozen):
        assert all(isinstance(a, str) for a in subproc.python_c("c", 1, 2.5))


class TestDispatchChild:
    def test_unfrozen_processes_are_never_children(self, monkeypatch):
        monkeypatch.setattr(subproc, "is_bundled", lambda: False)
        monkeypatch.setattr(sys, "argv", ["app", subproc.CHILD_FLAG, "raise SystemExit(3)"])
        subproc.dispatch_child()  # must return, not exit

    def test_a_parent_process_returns(self, frozen, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["app"])
        subproc.dispatch_child()

    def test_an_unrelated_argument_is_not_a_child(self, frozen, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["app", "--verbose", "somefile"])
        subproc.dispatch_child()

    def test_the_flag_alone_is_not_enough(self, frozen, monkeypatch):
        """No code to run — treat as a parent rather than exiting."""
        monkeypatch.setattr(sys, "argv", ["app", subproc.CHILD_FLAG])
        subproc.dispatch_child()

    def test_a_child_runs_the_code_and_exits(self, frozen, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["app", subproc.CHILD_FLAG, "print('ran')"])
        with pytest.raises(SystemExit) as exc:
            subproc.dispatch_child()
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == "ran"

    def test_a_child_sees_python_c_argv(self, frozen, monkeypatch, capsys):
        """The snippet's own args must land at argv[1:], as with `python -c`."""
        monkeypatch.setattr(
            sys, "argv",
            ["app", subproc.CHILD_FLAG, "import sys; print(sys.argv[1:])", "x", "y"],
        )
        with pytest.raises(SystemExit):
            subproc.dispatch_child()
        assert capsys.readouterr().out.strip() == "['x', 'y']"

    def test_a_child_runs_as_main(self, frozen, monkeypatch, capsys):
        code = "print('main' if __name__ == '__main__' else 'not-main')"
        monkeypatch.setattr(sys, "argv", ["app", subproc.CHILD_FLAG, code])
        with pytest.raises(SystemExit):
            subproc.dispatch_child()
        assert capsys.readouterr().out.strip() == "main"

    def test_a_child_exit_code_propagates(self, frozen, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["app", subproc.CHILD_FLAG, "raise SystemExit(7)"])
        with pytest.raises(SystemExit) as exc:
            subproc.dispatch_child()
        assert exc.value.code == 7


class TestRoundTripInARealInterpreter:
    """The unfrozen path must actually execute, not merely look right."""

    def test_python_c_output_runs(self, monkeypatch):
        monkeypatch.setattr(subproc, "is_bundled", lambda: False)
        cmd = subproc.python_c("import sys; print('ok', sys.argv[1])", "arg")
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0
        assert out.stdout.strip() == "ok arg"

    def test_the_dispatch_protocol_round_trips(self, monkeypatch):
        """Simulate a frozen app: an entry point that dispatches, then spawns."""
        entry = textwrap.dedent(
            """
            import sys
            from spiritus.runtime import subproc
            subproc.is_bundled = lambda: True     # pretend to be frozen
            subproc.dispatch_child()              # child exits inside here
            print("PARENT")                       # only a parent reaches this
            """
        )
        # Parent run: no sentinel, so it falls through and prints.
        parent = subprocess.run([sys.executable, "-c", entry],
                                capture_output=True, text=True, timeout=60)
        assert parent.stdout.strip() == "PARENT"

        # Child run: the sentinel makes it run the snippet and exit first.
        child = subprocess.run(
            [sys.executable, "-c", entry, subproc.CHILD_FLAG, "print('CHILD')"],
            capture_output=True, text=True, timeout=60,
        )
        assert child.returncode == 0
        assert "CHILD" in child.stdout
        assert "PARENT" not in child.stdout
