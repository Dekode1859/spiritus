# Spiritus — Architecture Audit (Phase 1)

This document is the result of auditing the **Learning OS** reference
implementation (the `EDU` repository) and identifying which parts are reusable
*platform/runtime* and which parts are *application/domain* logic.

The governing constraint comes from the Spiritus Runtime spec:

> **Spiritus Core is a fully generic execution runtime. It must never contain
> domain knowledge.** Replacing the Core of one app with the Core of another
> must leave both apps running unchanged. Deleting an app must leave Core
> unchanged.

Everything below is an **extraction** of what already exists in Learning OS.
No new frameworks, abstractions, or tool systems were invented. Where Learning
OS delegates a concern to an external system (OpenCode), the audit says so
plainly rather than inventing a parallel mechanism.

---

## 1. Source repository (Learning OS / EDU)

```
EDU/
├── main.py                  # PyWebView shell + local HTTP server + lifecycle
├── opencode.json            # 3 agent definitions + default model   (DOMAIN)
├── pyproject.toml
├── Makefile
├── core/
│   ├── paths.py             # path resolution (dev vs bundle)
│   ├── opencode_server.py   # opencode `serve` subprocess lifecycle
│   └── vault.py             # markdown file CRUD + folder taxonomy
├── api/
│   └── bridge.py            # JS↔Python bridge: config, providers, auth, vault, dialogs
├── ui/
│   ├── index.html           # Shoelace chat UI                       (mostly platform)
│   ├── app.js               # chat logic, SSE streaming, sessions    (mostly platform)
│   └── style.css            # design system                          (platform)
└── vault/                   # markdown data: raw/processed/knowledge/curriculum/sessions  (DOMAIN data)
```

---

## 2. Classification

### 2.1 Platform / Runtime (reusable — belongs in Core)

| Component | Source | What makes it generic |
|-----------|--------|-----------------------|
| OpenCode subprocess lifecycle | `core/opencode_server.py` | Starts `opencode serve` on a port, isolates `HOME`, polls until ready. No domain knowledge. |
| Path resolution | `core/paths.py` | Dev vs PyInstaller bundle resolution, app-data dir. Only the literal `"learning-os"` name was domain-coupled → now injected. |
| PyWebView shell + UI HTTP server | `main.py` | Opens a window, serves the UI over `http://127.0.0.1` with no-cache headers, wires lifecycle. Title was the only domain string → now injected. |
| File CRUD primitives | `core/vault.py` (read/write/delete/list + path-safety) | Generic read/write/list/delete over a root with traversal protection. |
| Provider / auth / model switching | `api/bridge.py` (`get_providers`, `save_provider_key`, `remove_provider_key`, `set_default_model`) | LLM provider abstraction; provider IDs are generic, not domain. |
| JS↔Python bridge plumbing | `api/bridge.py` | Config exposure, file ops, dialogs. The *methods* are generic; the *data* they relay is app-supplied. |
| Chat UI runtime | `ui/app.js`, `ui/style.css`, `ui/index.html` | Session list, per-message streaming bubbles, delta handling, working state, model/agent pickers. None of it is learning-specific *except* three hardcoded strings (see below). |

### 2.2 Application / Domain (must NOT be in Core)

| Component | Source | Why it is domain |
|-----------|--------|------------------|
| Agent definitions + prompts | `opencode.json` (`curriculum`, `session-planner`, `recap`) | Pure educational prompting + workflow. |
| Workspace folder taxonomy | `core/vault.py` `FOLDERS = (raw, processed, knowledge, curriculum, sessions)` | These folders *mean* something only to Learning OS. |
| New-note location | `core/vault.py` `new_session_file()` → `raw/` | Domain convention. |
| Folder icons in sidebar | `ui/app.js` `renderVaultFolders` icon map | Maps domain folders → icons. |
| Agent pre-population list | `ui/app.js` `CUSTOM_AGENTS` | The 3 learning agents. |
| App identity | `"Learning OS"` window title, `"learning-os"` app-data dir, `vault/` data | Branding + data identity. |
| Default model choice | `opencode.json` `"model"` | App preference. |

### 2.3 Delegated to OpenCode (NOT re-implemented in Core)

The spec lists a *Tool System* and *Agent execution pipeline* as Core concerns.
In the reference implementation these are **owned by OpenCode**, which Core
launches as a subprocess:

- **Tool registry / execution / permissions** — OpenCode's built-in tools
  (`read`, `write`, `bash`, `webfetch`, …) and its permission model. Core does
  **not** define a parallel tool framework. Generic core-level tools
  (`write_file`, `http_request`, …) referenced by the spec map onto OpenCode's
  built-ins. App-level tools (`parse_resume`, `compute_match_score`, …) would be
  added per-app via `opencode.json` / MCP servers — never in Core.
- **Agent execution pipeline / multi-agent orchestration** — OpenCode executes
  agents defined in `opencode.json`. Core provides the *runtime that hosts*
  OpenCode and a thin loader that surfaces the app's agent definitions to the
  UI. Core never contains agent logic.
- **Event routing** — OpenCode emits an SSE stream (`/event`) consumed by the
  UI's event bus in `app.js`. Core ships that generic bus; the events carry no
  domain meaning.

This is faithful to the "don't invent" rule: Core is the **runtime that hosts a
generic execution engine (OpenCode)** plus the desktop shell, storage
primitives, provider abstraction, and UI bus around it.

---

## 3. Extraction map (Current → New)

```
EDU/main.py                       → core/spiritus/runtime/shell.py        (+ run())
EDU/core/opencode_server.py       → core/spiritus/runtime/server.py
EDU/core/paths.py                 → core/spiritus/runtime/paths.py        (app_id injected)
EDU/core/vault.py  (primitives)   → core/spiritus/storage/__init__.py     (generic, no folders)
EDU/core/vault.py  (FOLDERS/taxo) → apps/learning-os/  (app config: workspace_folders)
EDU/api/bridge.py  (providers)    → core/spiritus/providers/__init__.py
EDU/api/bridge.py  (agents read)  → core/spiritus/agents/__init__.py
EDU/api/bridge.py  (bridge glue)  → core/spiritus/bridge.py               (uses AppConfig)
EDU/ui/*                          → core/spiritus/ui/*                    (branding via config)
EDU/opencode.json                 → apps/learning-os/opencode.json       (DOMAIN, unchanged)
EDU/vault/*                       → apps/learning-os/workspace/*         (DOMAIN data)

(tools)                           → core/spiritus/tools/   README: delegated to OpenCode
(events)                          → core/spiritus/events/  README: OpenCode SSE + UI bus
```

### The Core↔App contract

The single seam between Core and an app is **`spiritus.AppConfig`**. An app
constructs one object and calls `spiritus.run(config)`. Core reads *only* this
object for everything domain-specific:

```python
AppConfig(
    app_id="learning-os",          # data isolation dir name
    app_title="Learning OS",       # window title + UI header
    app_root=<this app dir>,       # where opencode.json + workspace live
    workspace_dirname="vault",     # name of the data root
    workspace_folders=(            # taxonomy — APP-defined, Core is blind to meaning
        WorkspaceFolder("raw", "inbox"),
        WorkspaceFolder("processed", "file-check-2"),
        WorkspaceFolder("knowledge", "brain"),
        WorkspaceFolder("curriculum", "graduation-cap"),
        WorkspaceFolder("sessions", "calendar-days"),
    ),
    default_capture_folder="raw",
)
```

Core never hardcodes any of these values. Swap in Job Search OS's `AppConfig`
(different folders, title, agents) and the *same Core* runs it unchanged.

---

## 4. Swapability check against the spec

| Spec invariant | How the extraction satisfies it |
|----------------|---------------------------------|
| Core contains no domain words (job/learning/curriculum/…) | All domain strings moved into each app's `AppConfig` + `opencode.json`. Core source is grep-clean of domain terms. |
| Delete an app → Core unchanged | Apps live under `apps/`; Core under `core/` with zero references to any app. |
| Swap Core → apps unmodified | Apps depend only on the public API `spiritus.run` / `AppConfig` / `WorkspaceFolder`. Any Core implementing that contract is valid. |
| Storage has no folder semantics | `core/spiritus/storage` exposes read/write/list/delete over a root. Folder names come from `AppConfig`, supplied by the app. |

See `docs/parity-checklist.md` for the behavioral-equivalence validation of the
migrated Learning OS.
