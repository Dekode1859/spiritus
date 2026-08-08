"""Spiritus — an SDK and runtime for agent-powered applications.

The package provides the runtime foundation and public abstractions that let an
application use OpenCode agents, tools, skills, permissions, and integrations
without implementing the underlying process and communication plumbing.

Public API:

    from spiritus import run, AppConfig, WorkspaceFolder

    run(AppConfig(
        app_id="my-app",
        app_title="My App",
        app_root=Path(__file__).parent,
        workspace_folders=(WorkspaceFolder("inbox", "inbox"), ...),
    ))

"""
from .config import AppConfig, WorkspaceFolder
from .runtime import run

__all__ = ["run", "AppConfig", "WorkspaceFolder"]


def _read_version() -> str:
    """Report the version this package was installed from.

    `pyproject.toml` is the single source of truth; the version is not written
    down anywhere in the source. Reading it back from installed distribution
    metadata is what makes that true in both directions — an installed copy
    reports the version of the artifact it came from, never a number that a
    working tree has since moved past.

    The distribution name is spelled out rather than derived from `__name__` so
    the lookup remains explicit if the package layout changes later.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("spiritus")
    except PackageNotFoundError:
        pass

    # Not installed — a source tree on sys.path. Fall back to the pyproject.toml
    # beside the package, which is the same source of truth an install reads.
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        # Neither installed nor beside its own source: nothing can be known.
        return "0.0.0"


__version__ = _read_version()
