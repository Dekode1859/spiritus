"""Execution runtime: desktop shell, OpenCode lifecycle, path resolution."""
from . import paths, subproc
from .server import OpenCodeServer
from .shell import run

__all__ = ["run", "OpenCodeServer", "paths", "subproc"]
