# AGENTS.md

This file is the single source of repository guidance for Spiritus.

## What Spiritus is

Spiritus is a Python runtime and SDK for building agent-powered desktop
applications on top of OpenCode. It combines the application shell, the
Python↔HTML/CSS/JavaScript bridge, the managed agent runtime, local application
state, and future application bundling into one developer-facing package.

The developer builds an application that solves a specific problem. The end
user should receive one application and should not need to understand OpenCode,
agent orchestration, tools, skills, permissions, MCP servers, or the processes
underneath it.

The long-term direction is to expose those OpenCode capabilities through
Spiritus abstractions while keeping OpenCode as the underlying engine:

```text
Application UI + Python logic
            │
            ▼
     Spiritus bridge/runtime
            │
            ▼
     Spiritus agent abstractions
            │
            ▼
          OpenCode
```

Spiritus is a library and application runtime, not a collection of example
applications. Consumer applications live in their own repositories.

## Current architecture

```text
Spiritus/
├── spiritus/
│   ├── config.py              # application/runtime configuration contract
│   ├── runtime/               # PyWebView shell, server lifecycle, paths
│   ├── bridge.py              # Python↔JavaScript application bridge
│   ├── storage/               # safe generic filesystem primitives
│   ├── providers/             # provider/auth/model integration
│   ├── agents/                # current OpenCode agent configuration loader
│   ├── engine/                # OpenCode resolution and provisioning
│   ├── integrations/          # optional integrations kept outside base runtime
│   ├── tools/                 # tool-system boundary documentation
│   ├── events/                # event-system boundary documentation
│   └── ui/                    # built-in chat frontend
├── tests/                     # Spiritus runtime and bridge tests
├── docs/                      # package and architecture documentation
└── .github/workflows/         # CI, build, install, and release checks
```

The current public entry point remains:

```python
from spiritus import AppConfig, WorkspaceFolder, run

run(AppConfig(
    app_id="my-app",
    app_title="My App",
    app_root=Path(__file__).resolve().parent,
    workspace_folders=(WorkspaceFolder("inbox", "inbox", "Inbox"),),
))
```

This contract is expected to evolve toward a higher-level application SDK. Do
not add new application-specific behavior to the runtime. New general
capabilities should be designed as reusable Spiritus APIs or internal engine
adapters.

## Design boundaries

- OpenCode is an implementation dependency behind Spiritus, not the preferred
  application-facing protocol.
- Frontends should communicate through the Spiritus bridge rather than knowing
  OpenCode HTTP endpoints or event formats directly.
- Agent definitions, tools, skills, permissions, schemas, MCP integrations,
  and sub-agent orchestration are future Spiritus API surfaces. Keep the
  underlying OpenCode mapping replaceable.
- Storage and process lifecycle code must remain generic and application-safe.
- Browser automation is an integration, not a requirement of the base package.
  Applications that use it own the corresponding dependency.
- Bundling is a future Spiritus capability, with Windows as the first target.
  Keep resource lookup and subprocess behavior compatible with frozen builds.

## Development

Requirements: Python 3.11+, `uv`, and an available OpenCode installation when
running live agent functionality.

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv build
```

The test suite must remain runnable without a GUI toolkit at import time. The
runtime imports PyWebView lazily where possible so packaging and headless tests
continue to work.

## Packaging

The distribution and import package are both named `spiritus`. Do not introduce
product names such as `spiritus-desktop` or a browser-specific package extra.
Optional integrations should remain isolated and application-owned until a
stable integration API exists.

The future packaging interface is expected to grow toward commands such as
`spiritus build`, but bundling is not yet implemented. Do not claim that an app
can be packaged until the relevant platform build has been verified.

## Git rules

Every commit must use only:

```text
Dekode1859 <prateekdwivedi30@gmail.com>
```

Never add `Co-Authored-By`, `Co-authored-by`, or another authorship trailer.
Use the explicit identity when committing:

```bash
git -c user.name="Dekode1859" -c user.email="prateekdwivedi30@gmail.com" commit -m "message"
```

Keep commits focused and verify the test suite, lint, and build checks that are
relevant to the change before committing.

## Internal workstream and public naming

Conversation labels such as a target release number are planning context used
to keep work aligned. Do not automatically copy those labels into branch
names, commit messages, pull-request titles or bodies, or other public
metadata. Public artifacts should describe the behavior and user-visible
purpose of the change. Include a release number publicly only when the user
explicitly asks for it or when the repository's release workflow requires it.
