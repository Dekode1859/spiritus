# CareerForge

A Spiritus application. It consumes the **same unmodified Core** as Learning OS,
proving the swapability boundary — but ships its **own UI** (an About Me
dashboard) via `AppConfig.ui_dir` instead of the shared chat UI.

## V0 — About Me (built)

The first surface: a Profile workspace.

- **Ingest** — drop/upload `.txt` / `.md` / `.json`, or paste résumé text.
  Uploaded docs are stored in `workspace/documents/`.
- **Extract** — "Generate About Me" sends the documents to the `profile` agent
  (`opencode.json`), which writes a structured profile to
  `workspace/profile/profile.json` following `schemas/profile.schema.json`.
- **Render & edit** — the profile renders as a card (name, summary, skills,
  experience, projects, certifications, education). Edit the JSON and save.

The extraction is driven from the app's JS via OpenCode's synchronous
`POST /session/{id}/message` endpoint; the agent writes the file and the UI reads
it back (no prose-to-JSON parsing). If reading fails, the page falls back to an
editable empty schema so it's always usable.

### V0 limitations
- Text inputs only (`.txt`/`.md`/`.json` or pasted). **PDF/DOCX is the planned
  fast-follow** (needs a Python-side parser + a small Core bridge extension).

## Run

```bash
make install
make run
```

Requires the `opencode` CLI on PATH. **No key setup needed to start** — the app
defaults to a free OpenCode Zen model (`opencode/mimo-v2.5-free`).

### Settings (in-app)

Click **Settings** in the sidebar to:
- **Connect a provider** — paste an API key (Anthropic, OpenAI, …); the engine
  restarts automatically. Keys are stored in the app-local `.opencode-home/`.
- **Pick the model** — choose any connected provider + model (including the free
  OpenCode Zen models that need no key) and set it as the default.

This replaces the old `make auth-setup` CLI flow. (That target still exists for
scripting, but you no longer need it.) The active model is shown above the
Settings button.

## How it consumes Core

`main.py` adds the sibling `core/` to `sys.path`, declares an `AppConfig` (its
own folders, agent, branding, and `ui_dir="ui"`), and calls `spiritus.run(APP)`.
Core gained exactly one generic field for this — `ui_dir` — and otherwise did not
change. Learning OS still uses the shared chat UI unchanged.

## Roadmap (future)
- **V1** — Manual job import (paste a job → normalize to a job schema → store).
- **V2** — Matching (profile × job → score, strengths, gaps, résumé suggestions).
- A shared "views" system (or a chat panel) when matching/application agents
  need conversational infrastructure.
