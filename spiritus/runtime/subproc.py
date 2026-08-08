"""Running a snippet of Python in a fresh process, frozen or not.

Core does several things in short-lived child processes — driving Playwright,
rendering a PDF, opening a native file picker — because they cannot share a
thread with the pywebview event loop. Unfrozen, that is just
``[sys.executable, "-c", code]``.

Frozen, that line is a fork bomb. ``sys.executable`` is no longer an
interpreter, it is the application; the PyInstaller bootloader ignores ``-c``
and runs the bundled entry script again. The "child" is therefore a second copy
of the whole app, which reaches the same call site and spawns a third, and so
on until something is exhausted. Nothing reports an error — the parent simply
blocks on a pipe until its timeout while processes multiply behind it.

The fix is a private argv protocol. A frozen parent asks for a child with a
sentinel flag, and every frozen process checks for that flag before it does
anything else: if it is there, the process is a child, so it runs the requested
code and exits instead of becoming another app.

Two halves that must stay in step:

    parent   cmd = python_c(code, *args)      # build the command
    child    dispatch_child()                 # first thing every process does

`run()` calls `dispatch_child()` before it builds a window, so applications
using the normal entry point get this for free.
"""
from __future__ import annotations

import sys

from .paths import is_bundled

# Deliberately obscure: this must never collide with an argument a real user
# could pass to the application.
CHILD_FLAG = "--spiritus-exec-child"


def python_c(code: str, *args: object) -> list[str]:
    """Command that runs ``code`` in a fresh Python process.

    Mirrors ``[sys.executable, "-c", code, *args]``, which is exactly what it
    returns when not frozen. Frozen, it returns the sentinel form that
    `dispatch_child` understands.
    """
    argv = [str(a) for a in args]
    if is_bundled():
        return [sys.executable, CHILD_FLAG, code, *argv]
    return [sys.executable, "-c", code, *argv]


def dispatch_child() -> None:
    """Run the requested code and exit, if this process is a spawned child.

    Returns immediately in a parent process, so it is safe — and intended — to
    call unconditionally at start-up. Never returns in a child.
    """
    if not is_bundled():
        return
    if len(sys.argv) < 3 or sys.argv[1] != CHILD_FLAG:
        return

    code = sys.argv[2]
    # Present the child with the argv shape `python -c` would have given it:
    # argv[0] is the program, and the snippet's own arguments follow.
    sys.argv = [sys.argv[0], *sys.argv[3:]]

    # `__name__ == "__main__"` so snippets guarded that way still run.
    namespace = {"__name__": "__main__", "__file__": "<spiritus-child>"}
    exec(compile(code, "<spiritus-child>", "exec"), namespace)  # noqa: S102
    raise SystemExit(0)
