"""Named agent workspaces compiled to narrow OpenCode permission rules."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import WorkspaceFolder
from .permissions import Access

_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class WorkspaceAccess:
    """One agent's access to a named workspace folder.

    ``access`` controls entry into the folder. ``read`` and ``write`` control
    which operation classes are exposed to the agent at all. Keeping those
    dimensions separate lets OpenCode's external-directory guard broker a
    precise folder approval without exposing the application/configuration
    directory as part of the agent worktree.
    """

    folder: str
    access: Access | str = Access.ALLOW
    read: bool = True
    write: bool = False

    def __post_init__(self) -> None:
        folder = self.folder.strip()
        if not _COMPONENT.fullmatch(folder) or folder in {".", ".."}:
            raise ValueError("workspace folder references must be safe single names")
        if not self.read and not self.write:
            raise ValueError("workspace access must enable read, write, or both")
        object.__setattr__(self, "folder", folder)
        object.__setattr__(self, "access", Access.parse(self.access))


@dataclass(frozen=True, slots=True)
class Workspace:
    """Application-owned folders that may be granted to agents by name."""

    folders: tuple[WorkspaceFolder, ...] = field(default_factory=tuple)
    dirname: str = "workspace"

    def __post_init__(self) -> None:
        dirname = self.dirname.strip()
        if not _COMPONENT.fullmatch(dirname) or dirname in {".", ".."}:
            raise ValueError("workspace dirname must be a safe single directory name")
        folders = tuple(self.folders)
        names: list[str] = []
        for folder in folders:
            if not isinstance(folder, WorkspaceFolder):
                raise TypeError("workspace folders must be WorkspaceFolder values")
            name = folder.name.strip()
            if not _COMPONENT.fullmatch(name) or name in {".", ".."}:
                raise ValueError("workspace folder names must be safe single names")
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError("workspace folder names must be unique")
        object.__setattr__(self, "folders", folders)
        object.__setattr__(self, "dirname", dirname)

    @property
    def folder_names(self) -> tuple[str, ...]:
        return tuple(folder.name for folder in self.folders)

    def root(self, project_root: Path) -> Path:
        return Path(project_root) / self.dirname

    def ensure(self, project_root: Path) -> Path:
        root = self.root(project_root)
        root.mkdir(parents=True, exist_ok=True)
        for folder in self.folders:
            (root / folder.name).mkdir(parents=True, exist_ok=True)
        return root

    def compile_policy(
        self,
        project_root: Path,
        grants: tuple[WorkspaceAccess, ...],
    ) -> tuple[dict[str, bool], dict]:
        """Return tool and permission overrides for one agent."""
        known = set(self.folder_names)
        seen: set[str] = set()
        external: dict[str, str] = {"*": Access.DENY.value}
        enable_read = False
        enable_write = False
        root = self.root(project_root).resolve()

        for grant in grants:
            if grant.folder not in known:
                raise ValueError(f"unknown workspace folder {grant.folder!r}")
            if grant.folder in seen:
                raise ValueError(f"duplicate workspace access for {grant.folder!r}")
            seen.add(grant.folder)
            folder = (root / grant.folder).resolve()
            action = grant.access.value
            # OpenCode emits the immediate-parent wildcard for file access on
            # Windows. Include the directory itself and recursive descendants
            # so the same contract works for directory and nested operations.
            external[str(folder)] = action
            external[str(folder / "*")] = action
            external[str(folder / "**")] = action
            enable_read = enable_read or grant.read
            enable_write = enable_write or grant.write

        tools: dict[str, bool] = {}
        permissions: dict = {"external_directory": external}
        if enable_read:
            tools["read"] = True
            permissions["read"] = Access.ALLOW.value
        if enable_write:
            for name in ("edit", "write", "apply_patch"):
                tools[name] = True
            permissions["edit"] = Access.ALLOW.value
        return tools, permissions


__all__ = ["Workspace", "WorkspaceAccess"]
