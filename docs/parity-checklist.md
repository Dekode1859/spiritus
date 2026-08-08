# Behavioral Parity Checklist — Learning OS (pre- vs post-extraction)

The extraction is successful only if migrated Learning OS behaves equivalently
to the reference implementation (the `EDU` repo). This checklist enumerates the
workflows and how the migrated version preserves each.

Legend: ✅ verified statically/at protocol level · 🔍 requires a live GUI run
(only runs inside WKWebView — see "Manual verification" below).

## Workflows (before → after)

| # | Workflow | Before (EDU) | After (apps/learning-os) | Status |
|---|----------|--------------|--------------------------|--------|
| 1 | App launches, window titled "Learning OS" | `main.py` title literal | `AppConfig.app_title` → `applyBranding()` sets title/brand/empty-state | ✅ config relays title; 🔍 visual |
| 2 | OpenCode server boots, isolated home | `core/opencode_server.py` | `spiritus.runtime.OpenCodeServer` (same logic, `.opencode-home/` per app) | ✅ same code path |
| 3 | Agent dropdown shows curriculum / session-planner / recap | hardcoded `CUSTOM_AGENTS` | from `opencode.json` via `get_config().agents` | ✅ verified output |
| 4 | Default agent = Session Planner | hardcoded | `AppConfig.default_agent="session-planner"` | ✅ verified output |
| 5 | Model picker lists providers; "Free" badge for OpenCode Zen only | `bridge.get_providers` | `spiritus.providers.list_providers` (same logic) | ✅ same code path |
| 6 | Create session (lazy, on first send) | `app.js` | unchanged UI logic in Core | ✅ same code |
| 7 | Send message; user bubble + streaming reply | `app.js` SSE + delta handling | unchanged in Core | ✅ same code; 🔍 visual |
| 8 | Multi-message turn (text → tool → text) renders live | per-message bubbles | unchanged in Core | 🔍 visual |
| 9 | "Working…" indicator from session.status/idle | `app.js` | unchanged in Core | 🔍 visual |
| 10 | Curriculum agent writes to `curriculum/` | OpenCode `write` tool + prompt | `opencode.json` unchanged; workspace at `workspace/` | ✅ same prompt/tool; 🔍 visual |
| 11 | Session plan written to `sessions/` | prompt | unchanged `opencode.json` | ✅ same prompt |
| 12 | Recap written to `processed/` | prompt | unchanged `opencode.json` | ✅ same prompt |
| 13 | Sidebar shows folder tree with counts + icons | hardcoded icon map | `workspace_folders` config → `workspace_tree()` | ✅ verified counts/icons |
| 14 | Open a folder, read a file | `bridge.vault_*` | `bridge.workspace_*` (same primitives) | ✅ verified read/list |
| 15 | Provider key save/remove restarts server | `bridge` | `spiritus.providers` + `Bridge` (same logic) | ✅ same code path |
| 16 | Set default model rewrites opencode.json | `bridge.set_default_model` | `spiritus.providers.set_default_model` (same logic) | ✅ same code path |

## Storage behavior

| Aspect | Before | After | Status |
|--------|--------|-------|--------|
| Data root dir | `vault/` | `workspace/` (renamed; same content copied) | ✅ |
| Folders | raw/processed/knowledge/curriculum/sessions | identical (app-declared) | ✅ |
| Path-traversal protection | `vault._safe_path` | `storage._safe` (same check) | ✅ |
| Existing roadmap file preserved | `curriculum/javascript-...md` | copied into `workspace/curriculum/` | ✅ |

> Note: the data directory was renamed `vault/` → `workspace/` to match the
> Core's domain-neutral vocabulary. File **contents** and folder names are
> unchanged. If exact path parity (`vault/`) is required, set
> `workspace_dirname="vault"` in `main.py` — Core supports either.

## What was verified automatically

- `import spiritus` succeeds without a GUI (lazy `webview` import). ✅
- `Bridge.get_config()` returns correct title, agents (from opencode.json),
  default_model, default_agent, and workspace_folders. ✅
- `Bridge.workspace_tree()` counts files per folder (curriculum=1). ✅
- `app.js` passes `node --check`. ✅
- Core source is grep-clean of domain words (learning/curriculum/job/resume/…). ✅

## Manual verification (live GUI — run `make run` in apps/learning-os)

These can only be confirmed inside the running WKWebView:

- [ ] Window opens titled "Learning OS"; sidebar + empty state branded.
- [ ] Agent dropdown lists the 3 agents; Session Planner preselected.
- [ ] Send a message → user bubble + streamed reply.
- [ ] Ask curriculum agent to write a roadmap → file appears under
      `curriculum/`, sidebar count increments.
- [ ] Multi-step (tool-using) turn renders the post-tool message live.
- [ ] Provider settings: add/remove a key, switch model.
