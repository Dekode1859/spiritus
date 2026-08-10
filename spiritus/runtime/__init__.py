"""Execution runtime: desktop shell, OpenCode lifecycle, path resolution."""
from . import paths, subproc
from .client import OpenCodeClient, OpenCodeError
from .lifecycle import ShutdownCoordinator
from .server import OpenCodeServer
from .shell import run
from .window import WindowController

__all__ = [
    "run",
    "OpenCodeClient",
    "OpenCodeError",
    "OpenCodeServer",
    "ShutdownCoordinator",
    "WindowController",
    "paths",
    "subproc",
]
