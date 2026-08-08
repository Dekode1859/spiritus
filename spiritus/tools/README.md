# Core Tools

Per the runtime spec, tools are **stateless, composable, generic**, and the
*tool registry / execution / permission* machinery is a Core responsibility.

In this implementation that machinery is **provided by OpenCode**, which Core
hosts as a subprocess (see `spiritus/runtime/server.py`). OpenCode ships the
generic tool runtime referenced by the spec:

| Spec example (core-level) | Provided by OpenCode as |
|---------------------------|-------------------------|
| `fetch_file`              | `read`                  |
| `write_file`             | `write` / `edit`        |
| `http_request`           | `webfetch`              |
| `parse_json`             | available via `bash`    |

We deliberately do **not** build a second, parallel tool framework here — that
would violate the "do not invent" rule and duplicate OpenCode's permission
model and execution pipeline.

## Application-level tools

Domain tools (e.g. `parse_resume`, `extract_job`, `compute_match_score`) are
**not** core tools. An application adds them to its own `opencode.json` or via
an MCP server declared in that file. They never live in Core.

This package is intentionally code-free; it documents the boundary.
