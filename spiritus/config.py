"""
The Core ↔ Application contract.

This is the *only* place an application injects domain identity into the Core
runtime. Core reads an ``AppConfig`` and nothing else app-specific; it never
hardcodes a title, folder name, agent, or data directory.

Swapability invariant: any Core that consumes an ``AppConfig`` with these fields
can run any app, and any app that produces one runs on any such Core.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkspaceFolder:
    """A folder the application wants surfaced in the workspace sidebar.

    Core treats this as opaque: it does not know what ``name`` *means*, only
    that the app wants a folder by that name shown with that icon.
    """
    name: str                 # directory name under the workspace root
    icon: str = "folder"      # lucide icon name used by the UI
    label: str = ""           # display label; falls back to ``name``

    def display(self) -> str:
        return self.label or self.name


@dataclass
class AppConfig:
    """Everything Core needs from an application to run it.

    No field here carries logic — only identity and declarative configuration.
    Domain behavior (prompts, schemas, workflows) lives in the app's
    ``opencode.json`` and its own modules, never in Core.
    """
    app_id: str                          # data-isolation id, e.g. "learning-os"
    app_title: str                       # window title + UI header
    app_root: Path                       # app dir: holds opencode.json + workspace

    # Optional: an application may ship its own front-end (its own index.html +
    # assets). When set, Core serves this directory instead of the built-in chat
    # UI. The app's UI still uses the same generic bridge + OpenCode API. Leaving
    # it unset (the default) keeps the shared chat UI — so existing apps are
    # unaffected and the swap invariant holds.
    ui_dir: Path | None = None
    bridge_cls: type[Any] | None = None

    workspace_dirname: str = "workspace"          # data root dir name under app_root
    workspace_folders: tuple[WorkspaceFolder, ...] = ()  # taxonomy (app-defined)
    default_capture_folder: str = ""              # where ad-hoc notes are written
    default_agent: str = ""                       # agent selected on launch (app pref)

    window_size: tuple[int, int] = (1440, 900)
    min_size: tuple[int, int] = (900, 600)

    # Optional environment overrides honored by Core (all generic).
    env_port_var: str = "OPENCODE_PORT"
    env_workspace_var: str = "WORKSPACE_PATH"

    def __post_init__(self):
        self.app_root = Path(self.app_root)
        if self.ui_dir is not None:
            ui = Path(self.ui_dir)
            # Resolve a relative ui_dir against the app root.
            self.ui_dir = ui if ui.is_absolute() else (self.app_root / ui)

    # Convenience for Core internals — still no domain knowledge.
    def folder_names(self) -> list[str]:
        return [f.name for f in self.workspace_folders]

    def folders_payload(self) -> list[dict]:
        """Serializable folder list handed to the UI (icons + labels)."""
        return [
            {"name": f.name, "icon": f.icon, "label": f.display()}
            for f in self.workspace_folders
        ]
