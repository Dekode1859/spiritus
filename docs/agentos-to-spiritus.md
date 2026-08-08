# Porting an AgentOS application to Spiritus

This is the short handoff guide for moving an application such as Persona from
AgentOS to Spiritus. Spiritus owns the reusable runtime and OpenCode adapter;
Persona should keep its product workflows, domain state, UI, and application
storage in its own repository.

## The target shape

Use Spiritus as the agent boundary:

```text
Persona UI and product logic
            │
            ▼
      spiritus.App / AgentRuntime
            │
            ▼
         OpenCode
```

Do not port AgentOS's HTTP, SSE, process, or OpenCode configuration plumbing
into Persona. Those are implementation details behind Spiritus.

## Install the local Spiritus checkout

During porting, use an editable sibling checkout so Persona sees local changes:

```toml
[project]
dependencies = ["spiritus"]

[tool.uv.sources]
spiritus = { path = "../spiritus", editable = true }
```

Adjust the relative path to match the two repositories. Spiritus currently
targets OpenCode `1.18.13`; an `opencode` executable must be available on
`PATH` (or configured through Spiritus's engine provisioning path).

## Minimal single-agent port

Start with one explicit model and one agent. The default Zen model used by the
Spiritus parity suite is `opencode/mimo-v2.5-free`; Persona may select another
available provider/model explicitly.

```python
from pathlib import Path

from spiritus import Agent, App, TextDelta


app = App(
    id="persona",
    title="Persona",
    root=Path(__file__).resolve().parent,
    agents=(
        Agent(
            name="assistant",
            description="Persona's primary assistant",
            prompt="Follow Persona's task instructions and be concise.",
            model="opencode/mimo-v2.5-free",
        ),
    ),
)


async def run_turn(session, prompt: str) -> str:
    run = await session.send(prompt)
    async for event in run.events():
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
    result = await run.result()
    return result.text


async def main() -> None:
    async with app.runtime() as runtime:
        session = await runtime.require_sessions().create()
        await run_turn(session, "Remember this codeword: PERSONA_OK")
        await run_turn(session, "What codeword did I ask you to remember?")
```

For a desktop application, `app.run()` starts the existing Spiritus shell and
bridge. For a Persona-owned UI or a headless worker, use `app.runtime()` and
connect the typed events to Persona's own presentation layer.

## Mapping common AgentOS concepts

| AgentOS concern | Spiritus replacement |
| --- | --- |
| Agent definition/config | `Agent` inside `App` |
| Provider/model string | Explicit `Model` value or `"provider/model"` string |
| Create/resume conversation | `SessionManager.create()` / `.resume(session_id)` |
| Streaming callback/SSE event | `RunHandle.events()` and typed `RunEvent` values |
| Final assistant response | `await RunHandle.result()` → `RunResult.text` |
| Blocking task call | `await Session.run(prompt)` |
| Conversation memory | Persist the `Session.id`; use `.history()` and `.resume()` |
| Structured task result | `Session.run(prompt, output=JSON_SCHEMA)` or `OutputSchema` |
| Python tool/function | `Tool` with JSON input/output schemas |
| File permissions | `Workspace` plus per-agent `WorkspaceAccess` and `Access` |
| Approval callback | `ApprovalRequested`, `RunHandle.respond()`, `ApprovalResolved` |
| Subagent | `Agent(mode="subagent")` and `delegates=(...)` |
| Slash/prompt command | `Command` and `Session.run_command()` |
| Reusable skill | `Skill` |
| MCP integration | `MCPServer` and `agent.mcp_servers` |

The stable application-facing event types include `RunStarted`, `TextDelta`,
`TextSnapshot`, `ToolStarted`, `ToolProgress`, `ToolCompleted`,
`ApprovalRequested`, `RunCompleted`, `RunFailed`, and `RunIdle`.

## Add capabilities in this order

Port one complete flow before moving to the next:

1. One agent accepts input, streams visible text, and returns a final result.
2. The same session can be resumed after a runtime restart and retains memory.
3. Structured output is validated against a JSON Schema.
4. Named workspace access works with explicit allow/deny/ask policy.
5. One typed Python tool runs and appears in the result/history.
6. Only then add delegation, skills, commands, or MCP integrations as Persona
   requires them.

Keep agent capabilities closed by default. Declare tools, workspace folders,
delegates, skills, and MCP servers on the relevant `Agent`; do not use a broad
wildcard permission to make a failing test pass.

## Persona migration checklist

- Replace AgentOS agent/config classes with `App` and `Agent`.
- Remove direct OpenCode HTTP, SSE, subprocess, and event parsing from Persona.
- Keep the session ID in Persona's conversation record; do not treat a runtime
  object as memory.
- Convert model output callbacks into `RunHandle.events()` and final results.
- Move application tools into `Tool` definitions with input/output schemas.
- Give file access only through named `WorkspaceFolder` values and
  `WorkspaceAccess` grants.
- Keep Persona's business state separate from `.spiritus` runtime state.
- Use `raw_config` only for an OpenCode field that Spiritus does not yet expose,
  and add a focused contract test when doing so.

## Required Persona smoke test

Before porting larger workflows, add one real integration test that:

1. Defines exactly one Persona agent with an explicit model.
2. Creates a session and sends a prompt containing a unique marker.
3. Observes a visible `TextDelta` and a final `RunResult` containing the marker.
4. Stops the runtime, creates a new runtime, resumes the same session, and asks
   for a remembered codeword.
5. Verifies the codeword and both turns through `session.history()`.

Run Spiritus's baseline gates from the Spiritus checkout while developing:

```powershell
uv run pytest
uv run ruff check .

$env:SPIRITUS_RUN_ENGINE = "1"
uv run pytest -m engine -v

$env:SPIRITUS_RUN_LIVE = "1"
uv run pytest -m live_opencode -v
```

The full capability rationale and current acceptance evidence live in
[`opencode-capabilities.md`](opencode-capabilities.md). The lower-level runtime
boundary is documented in [`runtime.md`](runtime.md).
