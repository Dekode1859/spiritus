"""Application configuration consumed by the Spiritus runtime.

This is where an application supplies its identity and declarative runtime
configuration. Spiritus does not hardcode a title, folder name, agent, or data
directory.

The configuration remains deliberately small while the higher-level Spiritus
agent abstractions continue to grow around it.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tracing import DiagnosticPolicy


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


@dataclass(frozen=True, slots=True)
class WindowConfig:
    """Declarative options for the application's PyWebView window.

    The runtime owns the window object and maps this stable configuration to
    the installed PyWebView version. ``AppConfig.window_size`` and
    ``AppConfig.min_size`` remain supported for existing applications.
    """

    width: int = 1440
    height: int = 900
    x: int | None = None
    y: int | None = None
    resizable: bool = True
    fullscreen: bool = False
    min_size: tuple[int, int] = (900, 600)
    hidden: bool = False
    frameless: bool = False
    easy_drag: bool = True
    minimized: bool = False
    maximized: bool = False
    on_top: bool = False
    confirm_close: bool = False
    background_color: str = "#FFFFFF"
    transparent: bool = False
    text_select: bool = False

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("window width and height must be positive")
        if len(self.min_size) != 2 or any(size <= 0 for size in self.min_size):
            raise ValueError("window min_size must contain two positive values")

    def create_window_kwargs(self) -> dict[str, Any]:
        """Return only the PyWebView ``create_window`` options we own."""
        return {
            "width": self.width,
            "height": self.height,
            "x": self.x,
            "y": self.y,
            "resizable": self.resizable,
            "fullscreen": self.fullscreen,
            "min_size": self.min_size,
            "hidden": self.hidden,
            "frameless": self.frameless,
            "easy_drag": self.easy_drag,
            "minimized": self.minimized,
            "maximized": self.maximized,
            "on_top": self.on_top,
            "confirm_close": self.confirm_close,
            "background_color": self.background_color,
            "transparent": self.transparent,
            "text_select": self.text_select,
        }


@dataclass(frozen=True, slots=True)
class WebViewConfig:
    """Rendering and GUI-loop options for the Spiritus PyWebView runtime."""

    gui: str | None = None
    debug: bool = False
    user_agent: str | None = None
    localization: Mapping[str, str] = field(default_factory=dict)

    def start_kwargs(self) -> dict[str, Any]:
        """Return the Spiritus-owned options for ``webview.start``."""
        return {
            "gui": self.gui,
            "debug": self.debug,
            "user_agent": self.user_agent,
            "localization": dict(self.localization),
            # Spiritus serves the UI itself so it can control the bridge and
            # lifecycle. Do not let PyWebView start a second HTTP server.
            "http_server": False,
        }


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
    diagnostic_policy: DiagnosticPolicy = field(default_factory=DiagnosticPolicy)

    window_size: tuple[int, int] = (1440, 900)
    min_size: tuple[int, int] = (900, 600)
    window: WindowConfig | None = None
    webview: WebViewConfig | None = None

    # Optional environment overrides honored by the runtime (all generic).
    env_port_var: str = "OPENCODE_PORT"
    env_workspace_var: str = "WORKSPACE_PATH"
    engine_directory: Path | None = None          # scoped OpenCode session worktree

    def __post_init__(self):
        self.app_root = Path(self.app_root)
        if not isinstance(self.diagnostic_policy, DiagnosticPolicy):
            raise TypeError("diagnostic_policy must be a DiagnosticPolicy value")
        if self.ui_dir is not None:
            ui = Path(self.ui_dir)
            # Resolve a relative ui_dir against the app root.
            self.ui_dir = ui if ui.is_absolute() else (self.app_root / ui)
        if self.engine_directory is not None:
            engine = Path(self.engine_directory)
            self.engine_directory = (
                engine if engine.is_absolute() else self.app_root / engine
            )

    def apply_bundle_environment(self) -> None:
        """Apply identity and workspace overrides emitted by a bundle variant."""
        app_id = os.environ.get("SPIRITUS_APP_ID", "").strip()
        if app_id:
            self.app_id = app_id
        title = os.environ.get("SPIRITUS_APP_TITLE", "").strip()
        if title:
            self.app_title = title
        workspace_dirname = os.environ.get("SPIRITUS_WORKSPACE_DIRNAME", "").strip()
        if workspace_dirname:
            self.workspace_dirname = workspace_dirname

    def resolved_window(self) -> WindowConfig:
        """Return the new window contract, preserving legacy options."""
        return self.window or WindowConfig(
            width=self.window_size[0],
            height=self.window_size[1],
            min_size=self.min_size,
        )

    def resolved_webview(self) -> WebViewConfig:
        return self.webview or WebViewConfig()

    # Convenience for runtime and bridge internals.
    def folder_names(self) -> list[str]:
        return [f.name for f in self.workspace_folders]

    def folders_payload(self) -> list[dict]:
        """Serializable folder list handed to the UI (icons + labels)."""
        return [
            {"name": f.name, "icon": f.icon, "label": f.display()}
            for f in self.workspace_folders
        ]
