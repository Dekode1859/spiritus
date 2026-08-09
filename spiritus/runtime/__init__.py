"""Execution runtime: desktop shell, OpenCode lifecycle, path resolution."""
from . import paths, subproc
from .client import OpenCodeClient, OpenCodeError
from .server import OpenCodeServer
from .shell import run

__all__ = [
    "run",
    "OpenCodeClient",
    "OpenCodeError",
    "OpenCodeServer",
    "paths",
    "subproc",
]
