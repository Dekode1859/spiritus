# Spiritus Tools

Tools are **stateless, composable, and generic**. OpenCode currently provides
the tool registry, execution, and permission machinery that Spiritus exposes to
applications.

OpenCode is hosted as a subprocess (see `spiritus/runtime/server.py`) and ships
the generic tool runtime used by Spiritus:

| Capability | Provided by OpenCode as |
|---------------------------|-------------------------|
| `fetch_file`              | `read`                  |
| `write_file`             | `write` / `edit`        |
| `http_request`           | `webfetch`              |
| `parse_json`             | available via `bash`    |

Spiritus will add higher-level tool abstractions where they reduce application
complexity without duplicating OpenCode's permission model and execution
pipeline.

## Application-level tools

Application-specific tools belong in the consuming application's
`opencode.json` or in an MCP server declared there. They do not belong in the
reusable Spiritus package.

This package is intentionally code-free; it documents the boundary.
