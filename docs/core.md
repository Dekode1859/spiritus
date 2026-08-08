# Spiritus Core

The generic runtime. Contains **no domain knowledge** — no jobs, learning,
curriculum, resumes, games, or app workflows. If a name describes *what* the
system does, it does not belong here; Core only defines *how* execution happens.

## Public API

```python
from spiritus import run, AppConfig, WorkspaceFolder
```

- `AppConfig` / `WorkspaceFolder` — the contract an app fills in.
- `run(config)` — boots the desktop shell + OpenCode runtime for that app.

## Modules

| Module | Responsibility |
|--------|----------------|
| `config.py` | The Core↔App contract (`AppConfig`). |
| `runtime/shell.py` | PyWebView window + UI HTTP server + `run()`. |
| `runtime/server.py` | OpenCode `serve` subprocess lifecycle. |
| `runtime/paths.py` | Dev/bundle path resolution (app_id injected). |
| `storage/` | Generic read/write/list/delete; no folder semantics. |
| `providers/` | LLM provider list, auth, model switching. |
| `agents/` | Reads app-declared agents from `opencode.json` (executes none). |
| `tools/` | Documents delegation to OpenCode's tool runtime (code-free). |
| `events/` | Documents the OpenCode SSE → UI event bus (code-free here). |
| `bridge.py` | JS↔Python UI API; relays app config, runs generic ops. |
| `ui/` | Shared chat UI; branding/folders/agents injected via config. |

## Consuming Core

Two supported modes; both yield the same `import spiritus`.

**Shared source (monorepo).** The example apps under `apps/` add the repo root
to `sys.path` and import. This is how the frozen in-repo apps consume Core.

**Installed package.** Core builds as a wheel (`uv build`) named `spiritus`, with
`ui/` shipped as package data. Apps in their own repos depend on it:

```toml
[project]
dependencies = ["spiritus"]

[tool.uv.sources]
spiritus = { path = "../Spiritus", editable = true }
```

or straight from git:
`spiritus @ git+https://github.com/Dekode1859/Spiritus@v0.3.0`

Swapping this Core for another that implements the same public API requires no
change to any app, in either mode.

Browser automation is kept in an optional integration module. Applications that
use those bridge methods declare their own Playwright dependency.

Python 3.11 through 3.13 are supported and tested in CI.

## Engine

Core hosts **OpenCode** as the execution engine (agents, tools, sessions,
events). Core manages its process lifecycle and wraps it with the desktop
shell, storage, providers, and UI. It does not reimplement that engine.
