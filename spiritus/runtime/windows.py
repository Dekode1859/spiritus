"""Windows-specific process launch options used by the generic runtime."""
from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_console_kwargs(
    *,
    creationflags: int = 0,
    startupinfo: Any | None = None,
) -> dict[str, Any]:
    """Return subprocess options that keep console children invisible on Windows.

    The options are intentionally opt-in at each Spiritus-owned process launch.
    Applications that explicitly request ``CREATE_NEW_CONSOLE`` or
    ``DETACHED_PROCESS`` retain that behavior.
    """
    if os.name != "nt":
        return {}

    new_console = subprocess.CREATE_NEW_CONSOLE
    detached = subprocess.DETACHED_PROCESS
    if creationflags & (new_console | detached):
        result: dict[str, Any] = {"creationflags": creationflags}
        if startupinfo is not None:
            result["startupinfo"] = startupinfo
        return result

    result = {"creationflags": creationflags | subprocess.CREATE_NO_WINDOW}
    if startupinfo is None:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    result["startupinfo"] = startupinfo
    return result


__all__ = ["hidden_console_kwargs"]
