"""Spiritus — an SDK and runtime for agent-powered applications.

The package provides the runtime foundation and public abstractions that let an
application use OpenCode agents, tools, skills, permissions, and integrations
without implementing the underlying process and communication plumbing.

Public API:

    from spiritus import Agent, App, AppConfig, WorkspaceFolder, run

    run(AppConfig(
        app_id="my-app",
        app_title="My App",
        app_root=Path(__file__).parent,
        workspace_folders=(WorkspaceFolder("inbox", "inbox"), ...),
    ))

"""
from .agents import Agent
from .app import AgentRuntime, App
from .bundling import (
    BundleError,
    BundleResource,
    BundleResult,
    BundleSpec,
    build_bundle,
    check_bundle,
)
from .commands import Command
from .config import AppConfig, WebViewConfig, WindowConfig, WorkspaceFolder
from .events import (
    ApprovalRequested,
    ApprovalResolved,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunIdle,
    RunStarted,
    TextDelta,
    TextSnapshot,
    ToolCompleted,
    ToolFailed,
    ToolProgress,
    ToolStarted,
)
from .mcp import MCPServer
from .models import Model
from .permissions import Access, ApprovalDecision
from .runtime import run
from .sessions import (
    Message,
    OutputSchema,
    OutputValidationError,
    RunCancelledError,
    RunExecutionError,
    RunHandle,
    RunResult,
    Session,
    SessionInfo,
    StructuredOutputError,
)
from .skills import Skill
from .tools import Tool, ToolContext
from .updates import (
    GitHubReleaseSource,
    GitLabReleaseSource,
    InstallerHandoff,
    JsonFeedSource,
    ReleaseCandidate,
    ReleaseSource,
    SemVerPolicy,
    StagedUpdate,
    SubprocessInstallerHandoff,
    UpdateArtifact,
    UpdateCheck,
    UpdateClient,
    UpdateConfig,
    UpdateConfigurationError,
    UpdateDownloader,
    UpdateDownloadError,
    UpdateError,
    UpdateInstallerError,
    UpdateSourceError,
    UpdateStatus,
    UpdateVerificationError,
    VersionPolicy,
)
from .workspace import Workspace, WorkspaceAccess

__all__ = [
    "Access",
    "Agent",
    "AgentRuntime",
    "App",
    "AppConfig",
    "WebViewConfig",
    "WindowConfig",
    "BundleError",
    "BundleResource",
    "BundleResult",
    "BundleSpec",
    "ApprovalDecision",
    "ApprovalRequested",
    "ApprovalResolved",
    "Command",
    "Message",
    "MCPServer",
    "Model",
    "OutputSchema",
    "OutputValidationError",
    "RunCompleted",
    "RunCancelledError",
    "RunEvent",
    "RunExecutionError",
    "RunFailed",
    "RunHandle",
    "RunIdle",
    "RunResult",
    "RunStarted",
    "Session",
    "SessionInfo",
    "Skill",
    "StructuredOutputError",
    "TextDelta",
    "TextSnapshot",
    "Tool",
    "ToolContext",
    "ToolCompleted",
    "ToolFailed",
    "ToolProgress",
    "ToolStarted",
    "WorkspaceFolder",
    "Workspace",
    "WorkspaceAccess",
    "GitLabReleaseSource",
    "GitHubReleaseSource",
    "InstallerHandoff",
    "JsonFeedSource",
    "ReleaseCandidate",
    "ReleaseSource",
    "SemVerPolicy",
    "UpdateArtifact",
    "UpdateCheck",
    "UpdateClient",
    "UpdateConfig",
    "UpdateDownloader",
    "UpdateConfigurationError",
    "UpdateDownloadError",
    "UpdateError",
    "UpdateInstallerError",
    "UpdateSourceError",
    "UpdateStatus",
    "UpdateVerificationError",
    "VersionPolicy",
    "StagedUpdate",
    "SubprocessInstallerHandoff",
    "build_bundle",
    "check_bundle",
    "run",
]


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
