# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Spiritus

Spiritus is a deterministic runtime engine for executing AI-driven workflows defined entirely outside the core system. It is a runtime with a swappable core — not a framework, library, or template.

**Core invariant:** Replace the Core of any Spiritus app with the Core of another and both must still run unmodified. Delete any app and Core remains unchanged and functional.

## Running Apps

**Prerequisites:** Python 3.11+, `uv`, and `opencode` CLI (`curl -fsSL https://opencode.ai/install | bash`).

**Learning OS** (reference app, shared chat UI):
```bash
cd apps/learning-os
make install    # uv sync
make run        # uv run python main.py
```

**Job Search OS** (custom UI, requires Playwright):
```bash
cd apps/jobsearch-os
make install    # uv sync
make run        # uv run python run.py  (bootstraps Playwright on first run)
```

**Provider credentials** (optional — free `opencode/mimo-v2.5-free` model works without keys):
```bash
make auth-setup    # add API key (Anthropic, OpenAI, etc.)
make auth-status   # list connected providers
```

Credentials are stored app-locally in `.opencode-home/` (not `~/.opencode`), so each app is isolated.

## Architecture

```
Spiritus/
├── spiritus/             # Generic runtime — zero domain knowledge (the package)
└── apps/
    ├── learning-os/     # Reference implementation
    └── jobsearch-os/    # V0 (About Me dashboard)
```

### Core ↔ App Contract

The sole seam between Core and any app is the `AppConfig` object (`spiritus/config.py`). Every app's `main.py` is exactly one object + one call:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # monorepo apps only
from spiritus import run, AppConfig, WorkspaceFolder

run(AppConfig(
    app_id="my-app",
    app_title="My App",
    app_root=Path(__file__).resolve().parent,
    workspace_dirname="workspace",
    workspace_folders=(
        WorkspaceFolder("raw", "inbox", "Raw"),   # (name, lucide_icon, label)
    ),
    default_capture_folder="raw",
    default_agent="my-agent",
    ui_dir="ui",   # omit to use shared chat UI; set to use a custom frontend dir
))
```

Agent definitions live in the app's `opencode.json`. Core reads them but never defines them.

### Core Modules

| File | Role |
|------|------|
| `spiritus/__init__.py` | Public API: `run()`, `AppConfig`, `WorkspaceFolder` |
| `spiritus/config.py` | Contract types |
| `spiritus/runtime/shell.py` | PyWebView window + HTTP server entry point |
| `spiritus/runtime/server.py` | OpenCode subprocess lifecycle |
| `spiritus/runtime/paths.py` | Dev vs PyInstaller bundle path resolution |
| `spiritus/bridge.py` | JS↔Python API (config, storage, providers) |
| `spiritus/storage/__init__.py` | Generic file CRUD — no folder semantics |
| `spiritus/providers/__init__.py` | LLM provider abstraction |
| `spiritus/ui/app.js` | Shared chat UI (sessions, SSE stream, agent picker) |

### Execution Model

- **Agents** — defined in app's `opencode.json`; executed by OpenCode; Core only surfaces them to the UI
- **Tools** — OpenCode's built-ins (`read`, `write`, `bash`, `webfetch`, etc.); app-specific tools via MCP servers in `opencode.json`
- **Events** — OpenCode emits an SSE stream (`/event`); `app.js` drives the UI event bus
- **Model switching** — rewrites the `"model"` field in the app's `opencode.json`

### Storage

`spiritus/storage/__init__.py` exposes `read()`, `write()`, `list_dir()`, `delete()`, `count_dir()`. All paths are relative-safe (`_safe()` prevents traversal). Core has no opinion on what folders mean — that's entirely `AppConfig.workspace_folders`.

### Custom vs Shared UI

- **Shared chat UI** (`spiritus/ui/`) — used when `ui_dir` is unset; built with vanilla JS + Shoelace 2.19.1 web components + Lucide icons
- **Custom frontend** — set `ui_dir="ui"` in `AppConfig`; app ships its own HTML/JS/CSS (see `apps/jobsearch-os/ui/`)

## Creating a New App

1. Create `apps/my-app/` with `main.py`, `opencode.json`, `pyproject.toml`, `Makefile`
2. In `main.py`: import from `../../core` and call `run(AppConfig(...))`
3. In `opencode.json`: define agents with system prompts; set `"model"`
4. Core handles everything else: window, server, UI, storage, providers, bridge

## Git Commit Rules (MUST FOLLOW)

**Authorship:** Every commit in this repo must use only the global git identity — `Dekode1859 <prateekdwivedi30@gmail.com>`. Never append `Co-Authored-By`, `Co-authored-by`, or any other authorship trailer to commit messages. No exceptions.

**When to commit:** Do not commit autonomously during active feature work. Once the work reaches a point where the user has tested the change and confirmed it behaves as expected (even if minor tweaks remain), stop and ask: *"Would you like to commit the current state?"* Only commit after the user says yes.

**How to commit:**
```bash
git -c user.name="Dekode1859" -c user.email="prateekdwivedi30@gmail.com" commit -m "message here"
```

## Core Purity Rules

Core must remain grep-clean of domain words (learning, curriculum, job, resume, etc.). If you're adding something to Core, ask: "would this make sense in a cooking-recipe app?" If not, it belongs in the app.

## Job Search OS — Scanner Roadmap

The Scanner tab (`apps/jobsearch-os/scanner/`, `app_bridge.py`) headlessly pulls jobs from
the user's logged-in LinkedIn session via the shared `workspace/browser-profile` Chromium
profile. It's app-owned, not Core — LinkedIn selectors/URLs live entirely in
`apps/jobsearch-os/scanner/linkedin_scan.py`, kept out of `spiritus` per the Core
Purity Rules below. Scanned jobs land in their own `workspace/jobs/scanner-feed.json`,
separate from the user's tracked `jobs.json`, until explicitly promoted.

Shipped in v1 (manual only): a "Scan now" button that scrapes the recommended-for-you feed
plus any configured keyword/location searches, deduped card-level fields only (title,
company, location, link, posted time, easy-apply).

Deliberately deferred — do not build without discussing the approach first:
- **Recurrence while the app is closed.** V1 only scans on demand while the app is open.
  The user wants this to eventually run in the background (e.g. a system-tray mode) so
  near-real-time notifications are possible even when the app isn't open — no mechanism
  chosen yet.
- **Windows notifications** for new matching jobs once recurrence exists.
- **Full per-job detail extraction** — deterministic parsing vs. plugging in the existing
  `job-extract` agent — deliberately not decided until the scan pipeline itself is proven
  out.
- **Scanner-side insights** — e.g. flagging job descriptions with incoherent requirements
  (implausible years-of-experience claims for tools that haven't existed that long), as a
  signal of how disorganized/undesirable a listing's originating company is. Not built yet.

## Tech Stack

- **Runtime:** Python 3.11+, PyWebView 4.4+, OpenCode CLI
- **Frontend:** Vanilla JS, Shoelace 2.19.1, Lucide icons, SSE for streaming
- **Packaging:** `uv` for deps, PyInstaller 6.0+ for bundles, Makefiles for orchestration
- **Job Search OS extra:** Playwright 1.40+ (browser automation for field detection)
