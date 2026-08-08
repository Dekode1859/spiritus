# Spiritus Runtime

Spiritus is the runtime foundation of an SDK for building agent-powered
applications. It currently wraps the OpenCode process, its HTTP and event
interfaces, the Python-to-frontend bridge, storage, provider configuration,
and the built-in application shell. The project is expanding these boundaries
into higher-level APIs for agents, tools, skills, permissions, MCP, and future
application bundling.

## Public API

```python
from spiritus import AppConfig, WorkspaceFolder, run
```

- `AppConfig` and `WorkspaceFolder` describe the application runtime contract.
- `run(config)` starts the application shell and OpenCode runtime.

The consuming application supplies its identity, UI, OpenCode configuration,
and product behavior. Spiritus supplies the reusable runtime and the bridge
that connects the UI to Python and OpenCode.

## Package modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Application configuration and workspace declarations. |
| `runtime/shell.py` | PyWebView window, UI HTTP server, and `run()`. |
| `runtime/server.py` | OpenCode subprocess lifecycle and event access. |
| `runtime/paths.py` | Development and bundled path resolution. |
| `storage/` | Safe generic read, write, list, count, and delete operations. |
| `providers/` | Provider credentials and model configuration. |
| `agents/` | Reads and presents application agent configuration. |
| `bridge.py` | JavaScript-to-Python bridge for runtime operations. |
| `integrations/` | Optional integrations that sit above the base runtime. |
| `tools/` | Documentation for OpenCode tool integration. |
| `events/` | Documentation for the OpenCode event stream. |
| `ui/` | Built-in chat UI assets and bridge client. |

## Consuming Spiritus

Install the package from git or use an editable local path during development:

```toml
[project]
dependencies = ["spiritus"]

[tool.uv.sources]
spiritus = { path = "../Spiritus", editable = true }
```

An application normally keeps its own `opencode.json`, front-end, workspace,
and product-specific modules in its own repository. Spiritus does not contain
or require a bundled example application.

## OpenCode boundary

OpenCode remains the underlying engine for model calls, agents, tools, sessions,
and events. Spiritus manages its lifecycle and presents stable application
boundaries around it; it does not reimplement the engine. The intended future
API is to make these capabilities configurable and composable without asking
every application author to understand OpenCode's lower-level configuration.

## Packaging direction

The runtime already supports development and bundled path resolution. A future
bundling layer will build distributable application artifacts, with Windows as
the first target and additional platform formats following as the packaging
surface matures.
