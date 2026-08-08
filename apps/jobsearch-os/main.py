"""
CareerForge - a Spiritus application.

V0 scope: a Profile ("About Me") workspace. Upload / paste candidate documents,
extract a structured profile via the `profile` agent, render and edit it.

Like every Spiritus app, this is only configuration + domain assets. Execution
(window, OpenCode runtime, storage, providers) comes from Spiritus Core. This app
ships its own UI (an About Me dashboard) via AppConfig.ui_dir.
"""
import sys
from pathlib import Path

# Consume Spiritus Core as shared source (monorepo). Apps in their own
# repos install the `spiritus` package instead and drop these two lines.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_bridge import JobSearchBridge

from spiritus import AppConfig, WorkspaceFolder, run

APP = AppConfig(
    app_id="jobsearch-os",
    app_title="CareerForge",
    app_root=Path(__file__).resolve().parent,
    ui_dir="ui",                      # this app ships its own front-end
    workspace_dirname="workspace",
    workspace_folders=(
        WorkspaceFolder("documents", "file-text", "documents"),
        WorkspaceFolder("profile",   "user",      "profile"),
    ),
    default_capture_folder="documents",
    default_agent="profile",
    bridge_cls=JobSearchBridge,       # adds Scanner methods; see app_bridge.py
)


if __name__ == "__main__":
    run(APP)
