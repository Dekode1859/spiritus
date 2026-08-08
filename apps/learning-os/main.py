"""
Lexicon.ai - a personal, source-first knowledge workspace.

This app owns the domain workflow for raw sources, deterministic processing,
and an LLM-maintained wiki. Execution mechanics stay in Spiritus Core.
"""
import sys
from pathlib import Path

# Spiritus Core is consumed as a shared source directory (monorepo): add the
# sibling core/ to the path so `import spiritus` resolves. Swapping the Core
# implementation behind this path requires no change to this app.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_bridge import LexiconBridge

from spiritus import AppConfig, WorkspaceFolder, run

APP = AppConfig(
    app_id="lexicon",
    app_title="Lexicon.ai",
    app_root=Path(__file__).resolve().parent,
    ui_dir="ui",
    bridge_cls=LexiconBridge,
    workspace_dirname="workspace",
    workspace_folders=(
        WorkspaceFolder("raw", "inbox", "raw"),
        WorkspaceFolder("processed", "file-check-2", "processed"),
        WorkspaceFolder("wiki", "brain", "wiki"),
    ),
    default_capture_folder="raw",
    default_agent="librarian",
)


if __name__ == "__main__":
    run(APP)
