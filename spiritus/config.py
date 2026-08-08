"""Application configuration consumed by the Spiritus runtime.

This is where an application supplies its identity and declarative runtime
configuration. Spiritus does not hardcode a title, folder name, agent, or data
directory.

The configuration remains deliberately small while the higher-level Spiritus
agent abstractions continue to grow around it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkspaceFolder:
    """A folder the application wants surfaced in its workspace UI.

    Spiritus treats this as opaque: it does not know what ``name`` means, only
    that the application wants a folder by that name shown with that icon.
    """
    name: str                 # directory name under the workspace root
    icon: str = "folder"      # lucide icon name used by the UI
    label: str = ""           # display label; falls back to ``name``

    def display(self) -> str:
        return self.label or self.name


@dataclass
class AppConfig:
    """The application configuration required by the Spiritus runtime.

    No field here carries logic — only identity and declarative configuration.
    Application behavior can be defined through OpenCode configuration and the
    higher-level Spiritus APIs.
    """
    app_id: str                          # data-isolation id, e.g. "my-app"
    app_title: str                       # window title + UI header
    app_root: Path                       # app dir: holds opencode.json + workspace

    # Optional: an application may ship its own front-end (its own index.html +
    # assets). When set, Spiritus serves this directory instead of the built-in
    # chat UI. The UI still uses the same bridge and OpenCode API.
    ui_dir: Path | None = None
    bridge_cls: type[Any] | None = None

    workspace_dirname: str = "workspace"          # data root dir name under app_root
    workspace_folders: tuple[WorkspaceFolder, ...] = ()  # taxonomy (application-defined)
    default_capture_folder: str = ""              # where ad-hoc input is written
    default_agent: str = ""                       # agent selected on launch

    window_size: tuple[int, int] = (1440, 900)
    min_size: tuple[int, int] = (900, 600)

    # Optional environment overrides honored by the runtime (all generic).
    env_port_var: str = "OPENCODE_PORT"
    env_workspace_var: str = "WORKSPACE_PATH"
    engine_directory: Path | None = None          # scoped OpenCode session worktree

    def __post_init__(self):
        self.app_root = Path(self.app_root)
        if self.ui_dir is not None:
            ui = Path(self.ui_dir)
            # Resolve a relative ui_dir against the app root.
            self.ui_dir = ui if ui.is_absolute() else (self.app_root / ui)
        if self.engine_directory is not None:
            engine = Path(self.engine_directory)
            self.engine_directory = (
                engine if engine.is_absolute() else self.app_root / engine
            )

    # Convenience for runtime and bridge internals.
    def folder_names(self) -> list[str]:
        return [f.name for f in self.workspace_folders]

    def folders_payload(self) -> list[dict]:
        """Serializable folder list handed to the UI (icons + labels)."""
        return [
            {"name": f.name, "icon": f.icon, "label": f.display()}
            for f in self.workspace_folders
        ]
