# Spiritus

[![CI](https://github.com/Dekode1859/Spiritus/actions/workflows/ci.yml/badge.svg)](https://github.com/Dekode1859/Spiritus/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/Dekode1859/Spiritus)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Spiritus is an SDK and runtime for building agent-powered applications with
Python, HTML, CSS, and JavaScript. It sits on top of OpenCode and exposes the
agent ecosystem through application-friendly primitives, so developers can
focus on the product they are building instead of the process, IPC, tool,
skill, permission, and model plumbing underneath it.

The name comes from *Spiritus Machinae* — “spirit of the machine”: the layer
that gives an otherwise conventional application its behavior and agency.

## Direction

An application built with Spiritus is more than an agent configuration. It is a
complete application whose agents, tools, skills, permissions, MCP connections,
storage, and UI are composed through one local runtime. The long-term goal is
to make that application easy to develop, bundle, and distribute without
requiring users to understand the agent system inside it.

The architecture is intentionally layered:

```
Application UI and logic
          │
   Spiritus bridge/runtime
          │
Spiritus agents, tools, skills, permissions, and integrations
          │
        OpenCode
```

Consumer applications are maintained in their own repositories. This
repository contains the reusable Spiritus package and its tests, not example
applications or product-specific workflows.

## Install

The distribution and import package are both named **`spiritus`**. There is no
PyPI release yet; install from git, pinned to a tag:

```bash
uv add "spiritus @ git+https://github.com/Dekode1859/Spiritus@v0.3.0"
```

```bash
pip install "spiritus @ git+https://github.com/Dekode1859/Spiritus@v0.3.0"
```

Applications can then start with the public runtime contract:

```python
from pathlib import Path
from spiritus import AppConfig, WorkspaceFolder, run

run(AppConfig(
    app_id="my-app",
    app_title="My App",
    app_root=Path(__file__).resolve().parent,
    workspace_folders=(WorkspaceFolder("inbox", "inbox", "Inbox"),),
))
```

The configuration is the current stable entry point. Higher-level APIs for
agents, tools, skills, permissions, IPC, MCP, and bundling are part of the
direction of the project and will grow around this runtime foundation.

## The execution engine

Spiritus hosts [OpenCode](https://opencode.ai) as its execution engine. The
engine is a native binary, so Spiritus provides explicit provisioning commands:

```bash
spiritus install-engine
spiritus engine-info
```

Nothing is downloaded implicitly. `run()` resolves an available engine but does
not fetch one; an application can call `spiritus.engine.ensure()` from its own
bootstrap when it wants to make installation explicit.

Resolution order:

1. `SPIRITUS_OPENCODE_BIN`, when set.
2. `opencode` on `PATH`.
3. The per-user cache populated by `install-engine`.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv build
```

The package supports Python 3.11 through 3.13. `pywebview` is loaded lazily by
the application shell so headless consumers can import and test Spiritus.

## Layout

```
Spiritus/
├── spiritus/       # runtime and SDK package
├── tests/          # package contract and behavior tests
├── docs/           # architecture and integration notes
└── .github/        # CI and release automation
```

## Releasing

The version lives in `pyproject.toml` (`project.version`) and nowhere else.
Bumping that line is the release request. CI runs lint, tests, and packaging
checks before the release workflow tags and publishes the artifacts.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE).
