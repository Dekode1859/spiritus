# OpenCode capabilities and Spiritus abstraction design

Status: G0-G6 implemented and verified on 2026-08-09; the 0.0.3 application
bundle builder is now available and installed-app parity remains a separate
gate. This document records the
pinned OpenCode server/configuration
contract, the Spiritus abstraction boundary built around it, the acceptance
order, and the evidence produced by each gate. The 0.0.3 bundle builder now
covers the reusable application-bundle layer; native installers remain separate.

## Executive summary

OpenCode already provides the difficult agent runtime: configuration merging,
providers and models, primary agents and subagents, built-in and custom tools,
skills, commands, plugins, MCP servers, permissions, sessions, SSE events,
structured output, and a headless OpenAPI server.

Spiritus does not rebuild those mechanisms. It makes them safe and
pleasant to compose into a product application. The developer should define
the product's capabilities and user experience; Spiritus should compile those
definitions into OpenCode configuration, host the process, broker events and
approvals, enforce application policy, and expose stable Python and frontend
APIs.

Implementation proceeded as runnable vertical slices, not as a collection of
disconnected type definitions. The first release gate was deliberately small:
one Spiritus-defined agent using one explicit model must accept input,
stream user-visible output, return a final result, persist its transcript,
survive an engine restart, resume the same session, and retain conversational
context. Every later abstraction is added to a similarly executable flow.

```text
Product application
  UI, domain logic, product workflows
          │
          ▼
Spiritus application SDK
  App, Agent, Tool, Skill, Policy, Workspace, Session, Run
          │
          ▼
Spiritus runtime and adapters
  process, config, IPC, events, approvals, persistence, packaging boundary
          │
          ▼
OpenCode
  models, agents, tools, MCP, sessions, permissions, execution
```

## Scope and version boundary

Spiritus currently pins OpenCode `1.18.13`. This document targets that binary
and its runtime contract:

- `opencode serve` exposes the headless HTTP server;
- configuration uses fields such as `agent`, `permission`, `provider`, and
  `mcp`;
- the server exposes its OpenAPI 3.1 document at `/doc` and the generated
  `@opencode-ai/sdk` client.

OpenCode also publishes `/v2` documentation for a separate `opencode2`
direction. That documentation changes names and shapes, including
`permissions` arrays instead of the current `permission` object and
`mcp.servers` instead of the current `mcp` map. It also describes an in-process
Effect SDK that is not the public package Spiritus is using. Spiritus should
not mix these contracts. The adapter must pin and test one OpenCode version,
using that version's generated OpenAPI document as the runtime source of truth.

## OpenCode capability inventory

The delivery gate is the first end-to-end flow that needs the capability. It
does not imply that the complete abstraction for that area must be designed at
once.

| Capability | What OpenCode provides | Spiritus abstraction target | First gate |
| --- | --- | --- | --- |
| Configuration | JSON/JSONC, schema, merged config, instructions, watchers, compaction | Compile the minimal typed application and agent definitions, with a raw-config escape hatch | G1 |
| Engine/server | `opencode serve`, health, OpenAPI, HTTP auth, CORS, lifecycle | Managed, version-checked, app-isolated engine lifecycle | G1 |
| Agents | Primary/subagent/all modes, prompts, model, temperature, steps, hidden/disabled state | Begin with one primary `Agent`; add exposure and delegation only when their flows are built | G1 |
| Providers/models | Auth, catalog, model IDs, options, provider allow/deny | Begin with one explicit provider/model ID and preflight; add profiles and credential UX later | G1 |
| Sessions | Create/list/get/delete, parent/children, messages, abort, summarize, revert, diffs | `Session` and `Run` lifecycle with durable IDs, history, final result, and resume | G1 |
| Events | SSE for server, session, message, permission, tool, and file events | Typed run stream that separates visible text, reasoning, status, errors, and completion | G1 |
| Structured output | JSON Schema output, validation retries, structured errors | Schema-backed task result that remains retrievable after the run | G2 |
| Permissions | `allow`, `ask`, `deny`; wildcard/resource matching; per-agent rules | Secure policy objects, approval events, and audit decisions | G3 |
| Workspace/files | `read`, `edit`, `glob`, `grep`, `bash`, external-directory guard | Named filesystem capabilities separated from app storage | G3 |
| Tools | Built-ins, custom tools, plugin hooks, JSON input schemas | Python-first typed tool registration and engine adapter | G4 |
| Subagents | Child sessions and `task` permission rules | Declarative delegation graph and child-run lifecycle | G5 |
| Skills | On-demand `SKILL.md` discovery and `skill` permission | Packaged skill definitions with policy and versioning | G6 |
| Commands | Prompt templates with arguments, agent, model, subtask mode | Product actions with stable input/output contracts | G6 |
| MCP | Local/remote servers, environment, headers, OAuth, timeouts | Managed integration with lifecycle, secrets, timeout, and policy | G6 |
| Plugins | JavaScript/TypeScript hooks and custom tools | Advanced integration escape hatch | Later |
| LSP/formatters | Optional code intelligence and formatting services | Optional integrations after policy stabilizes | Later |
| Sharing/TUI | Session sharing and TUI control endpoints | Not part of the first Spiritus contract | Later |

### Delivery gates

| Gate | Runnable product flow | Verification result |
| --- | --- | --- |
| G0: engine oracle | Exercise the pinned OpenCode contract without new Spiritus abstractions | Complete: raw config, model, direct result, stream, history, restart/resume, memory, schema behavior, and engine defects are executable tests |
| G1: single-agent conversation | Define one `Agent`, create/resume one `Session`, stream a turn, and await a final text result through Spiritus | Complete: the same black-box contract passes through raw OpenCode and the public Spiritus API |
| G2: application task result | Run the same agent with a JSON Schema result | Complete: schema validation, decoding, typed failures, normalized persistence, restart, and retrievable history pass |
| G3: safe file task | Give the agent named temporary workspace capabilities and broker an approval/denial | Complete: exact-folder allow, deny, `ask -> once`, typed events, final output, and durable audit all pass live |
| G4: one typed tool | Register and call one Python tool from the agent | Complete: schema input/output validation, authenticated loopback execution, lifecycle events, result, error mapping, and restart persistence pass |
| G5: delegation | Let the primary agent call exactly one declared subagent | Complete: exact task policy, tool result, parent/child sessions, both histories, and parent cancellation pass live |
| G6: packaged extensions | Add one skill, command, and MCP server as separate flows | Complete: each compiles, preflights where the pinned API permits it, executes through its own public flow, and persists its result |

G1 remains the minimum functional parity milestone. G3 is also required before
a general-purpose application can be considered safe to ship. Passing G0-G6
does not yet constitute a packaged desktop release: installed/frozen Windows
validation remains a separate installed-application gate.

## Important OpenCode semantics

### Configuration is layered

OpenCode supports JSON and JSONC. Configuration sources are merged; later
sources override conflicting keys while preserving non-conflicting values. The
documented sources include remote organization config, global config, custom
config, project `opencode.json`, `.opencode` directories, inline environment
config, and managed settings.

Spiritus should own a simpler application configuration pipeline:

1. Spiritus application definition.
2. App-local OpenCode configuration generated by Spiritus.
3. Explicit developer override files.
4. Runtime-only overrides such as selected model or approval mode.

The generated file should remain inspectable for debugging, but it should not
be the normal authoring surface.

Source: [OpenCode configuration](https://opencode.ai/docs/config/).

### Agents are personas and execution boundaries

OpenCode agents can be configured in JSON or Markdown. The useful fields for
Spiritus are `description`, `mode` (`primary`, `subagent`, or `all`), `prompt`,
`model`, `temperature`, `top_p`, `steps`, `disable`, `hidden`, `permission`,
and `permission.task`. Global `subagent_depth` also limits nesting.

Spiritus should separate concepts OpenCode currently combines:

```text
Agent
├── identity: name, description, prompt
├── model: provider/model, options, limits
├── behavior: steps, temperature, output mode
├── capabilities: tools, skills, MCP servers
├── delegation: agents this agent may call
└── exposure: visible to user, callable by app, callable by other agents
```

OpenCode's `task` permission controls model-driven subagent invocation. The
documentation also notes that users can invoke a subagent directly through the
`@` menu even when task permissions would deny it. `hidden` changes visibility,
not authorization. Spiritus must therefore distinguish agents shown in the
product UI, callable by application code, callable by another agent, and
internal agents that should never be user-selected.

Source: [OpenCode agents](https://opencode.ai/docs/agents/).

### Permissions are ordered policy rules

OpenCode permission outcomes are `allow`, `ask`, and `deny`. Rules can be
global or agent-specific, and object syntax can match commands, paths, URLs,
queries, agent IDs, and tool names. Later matching rules take precedence.

Important permission areas include:

```text
read                 file reads
edit                 edit/write/patch operations
glob                 file discovery patterns
grep                 content searches
bash                 shell commands
task                 subagent invocation
skill                skill loading
webfetch/websearch   network retrieval
external_directory   paths outside the project directory
question             user questions
doom_loop            repeated identical tool calls
```

OpenCode's documented defaults are permissive for most operations. That is
reasonable for an interactive coding assistant but unsafe as an accidental
default for a shipped product. Spiritus should generate an explicit policy for
every application.

The Spiritus policy model should support:

```text
default       allow | ask | deny
capability    read | write | execute | network | delegate | skill | mcp
resource      path | command | URL | tool | agent | skill | server
subject       app | agent | user action | background task
decision      allow | ask | deny
reason        user-visible explanation and audit label
```

OpenCode approval supports one-time approval, remembered approval, and
rejection. Spiritus should translate that into a product-level approval broker
so the frontend can show “Allow the Import agent to read files in Inbox?”
instead of exposing a raw engine payload.

Sources: [OpenCode permissions](https://opencode.ai/docs/permissions/) and
[OpenCode tools](https://opencode.ai/docs/tools/).

### File access is a capability boundary

OpenCode's built-in tools operate against the project/workspace context. File
modification is controlled by `edit`, which covers edit, write, and patch.
Access outside the project is represented by `external_directory` and can be
allowed or denied by path pattern. `bash` is broader because it executes shell
commands in the project environment.

Spiritus should provide three distinct storage concepts:

1. **Application data** — private user data managed by Spiritus storage APIs.
2. **Agent workspace** — files the agent is explicitly allowed to inspect or
   modify through OpenCode.
3. **External resources** — host paths or services requiring an explicit grant.

The developer should declare capabilities rather than write path patterns:

```python
Workspace(
    folders={
        "inbox": Folder(read=True, write=False),
        "drafts": Folder(read=True, write=True),
    },
    external=ExternalAccess.ask,
)
```

This is proposed syntax. Spiritus must compile it into both OpenCode rules and
its own storage/bridge checks. OpenCode permissions are not an operating-system
sandbox; applications granting shell or external access still need process and
OS-level defense in depth.

### Sessions and messages are the execution substrate

The OpenCode server provides session lifecycle, parent/child sessions, prompt
submission, asynchronous prompting, commands, shell execution, abort,
summarization, revert/unrevert, diffs, todo state, and permission responses.
It also emits SSE events for server, session, message, tool, permission, and
file-related state.

For the pinned engine, `POST /session/:id/message` blocks and returns the final
message, while `POST /session/:id/prompt_async` returns `204` and progress is
observed through `/event`. Spiritus needs both behaviors behind one `RunHandle`:
callers may iterate events and/or await the final result without issuing raw
HTTP requests.

Spiritus should expose a smaller model:

```text
Session
├── start / resume / close
├── send(UserInput) -> RunHandle
├── events() -> typed stream
├── cancel()
└── history / artifacts / approvals

RunHandle
├── status: queued | running | waiting | completed | failed | cancelled
├── text stream for user-facing conversation
├── tool/progress events for application UI
├── approval requests
└── final result or typed output
```

The UI should consume Spiritus events rather than OpenCode event names. This
keeps the frontend stable if Spiritus later changes engines or combines several
engine events into one product event.

Event normalization is functional, not cosmetic. The pinned engine emits
`message.part.delta` for reasoning parts as well as visible text parts.
Spiritus must first learn each part's type from `message.part.updated`, correlate
by message and part ID, expose only user-visible text on the text stream, and
use the stored message snapshot as the authoritative final result.

Sources: [OpenCode server](https://opencode.ai/docs/server/) and
[OpenCode SDK](https://opencode.ai/docs/sdk/).

### Conversational output and application output differ

OpenCode supports normal text responses and schema-directed structured output.
The current SDK documentation describes JSON Schema output, validation retries,
and a structured output error when valid data cannot be produced.

Spiritus should make the distinction explicit:

```python
# User-facing conversation.
result = await session.send("Help me organize these files")
await result.text_stream()

# Application-facing result.
profile = await session.run(
    "Extract the profile from the selected document",
    output=ProfileSchema,
)
assert isinstance(profile.value, Profile)
```

The first path can stream partial text, tool progress, questions, and
approvals. The second is a task contract that returns validated data or a typed
failure and does not require parsing conversational prose.

The pinned engine's `/doc` schema accepts
`format={"type": "json_schema", "schema": ..., "retryCount": ...}` and stores
the parsed value on the assistant message's `structured` field. Field naming
has evolved across engine versions, so Spiritus should still normalize it in
the adapter and test direct return, history retrieval, and restart/resume as
one contract.

Source: [OpenCode SDK structured output](https://dev.opencode.ai/docs/sdk/).

### Providers and credentials are runtime configuration

OpenCode supports a broad provider/model catalog, local models, API keys,
OAuth, environment variables, custom provider packages, model limits, and
provider/model options. Model IDs use `provider/model-id`. Providers can also
be allowlisted or disabled.

Spiritus should expose provider profiles, connection status, app policy for
allowed models, model aliases such as `fast` or `local`, secure credential
setup, and model capability metadata. Application code should not handle raw
OpenCode credentials or provider-specific authentication flows.

G1 should use an explicit, inexpensive smoke-test model rather than an alias.
The current test default is `opencode/mimo-v2.5-free`, with
`SPIRITUS_TEST_MODEL` able to override it. The test must preflight `/provider`
and fail with an actionable message when that exact model is unavailable.
OpenCode controls the Zen catalog, so this test default is not a durable SDK
default. OpenCode also documents Zen authentication and billing setup; a
shipped application must not promise that Zen is permanently credential-free
just because a free model currently works without a stored key.

The runtime must preserve Spiritus's isolation model: OpenCode credentials,
sessions, and config belong to the application runtime rather than the
developer's global OpenCode installation.

Sources: [OpenCode providers](https://opencode.ai/docs/providers/),
[OpenCode models](https://opencode.ai/docs/models/), and
[OpenCode Zen](https://opencode.ai/docs/zen/).

### Skills, commands, MCP, and plugins are different extension types

They should not become one undifferentiated Spiritus “tool” abstraction.

| Extension | OpenCode meaning | Spiritus meaning |
| --- | --- | --- |
| Tool | Callable action with input and permission behavior | Typed capability with an executor or engine adapter |
| Skill | On-demand reusable instructions in `SKILL.md` | Packaged behavior/instruction bundle with policy and versioning |
| Command | User-invoked prompt template with arguments | Product action with a stable contract |
| MCP server | External local/remote tool, prompt, or resource provider | Managed integration with lifecycle, secrets, timeout, and policy |
| Plugin | JavaScript/TypeScript hooks into OpenCode | Advanced escape hatch outside the stable Python API |

Skills are discovered from project or global locations and loaded on demand;
the `skill` permission can allow, ask, deny, or pattern-match them. MCP can
start local processes or connect to remote servers, use environment variables,
headers, OAuth, and timeouts. Both need app-local discovery and explicit policy
when an application is shipped.

Sources: [OpenCode skills](https://opencode.ai/docs/skills/),
[OpenCode MCP servers](https://opencode.ai/docs/mcp-servers/),
[OpenCode commands](https://opencode.ai/docs/commands/), and
[OpenCode plugins](https://opencode.ai/docs/plugins/).

## Implemented Spiritus developer experience

The developer defines the application once and Spiritus generates the engine
configuration and runtime plumbing. This is the implemented G1 public flow:

```python
from pathlib import Path

from spiritus import Agent, App, TextDelta

app = App(
    id="single-agent-probe",
    title="Single Agent Probe",
    root=Path(__file__).resolve().parent,
    agents=(
        Agent(
            name="assistant",
            description="A minimal persistent assistant",
            prompt="Follow the user's response format exactly.",
            model="opencode/mimo-v2.5-free",
            tools=(),
        ),
    ),
)

# Stream a turn and also await its authoritative final output.
async with app.runtime() as runtime:
    sessions = runtime.require_sessions()
    session = await sessions.create(agent="assistant")
    run = await session.send("Remember COBALT-731 and confirm it is stored")
    async for event in run.events():
        if isinstance(event, TextDelta):
            render(event.text)
    final = await run.result()
    session_id = session.id

# A new runtime process resumes the durable session and retained context.
async with app.runtime() as runtime:
    session = await runtime.require_sessions().resume(session_id)
    answer = await session.run("What codeword did I ask you to remember?")
    assert "COBALT-731" in answer.text
    assert await session.history()
```

The later gates add structured output, named workspaces, approvals, Python
tools, skills, commands, MCP, and delegation without changing this base
lifecycle. `AppConfig` and `run()` remain available for the original desktop
entry point.

The application author should not need to write the equivalent raw
`opencode.json`, manually start `opencode serve`, parse SSE, implement
permission response endpoints, or translate model/provider IDs.

## Implemented application flow

```text
1. Developer defines App, agents, capabilities, policies, and UI routes.
2. Spiritus validates the definition before startup.
3. Spiritus creates an app-local runtime directory and workspace.
4. Spiritus compiles the definition to OpenCode config and extension files.
5. Spiritus starts OpenCode with isolated data paths and an empty agent worktree.
6. UI calls Spiritus bridge methods, never raw OpenCode endpoints.
7. Spiritus creates/resumes a session and starts a conversational or typed run.
8. OpenCode emits messages, tool calls, approvals, and status events.
9. Spiritus translates them into stable UI/application events.
10. Spiritus enforces app policy and forwards approval decisions.
11. The run completes with text, typed data, artifacts, or a structured error.
12. The app persists user data through Spiritus/application storage APIs.
```

## Implementation status and remaining gaps

| Area | Implemented now | Remaining work |
| --- | --- | --- |
| Process lifecycle | Managed pinned server, Windows `HOME`/`USERPROFILE`/XDG isolation, scoped request directory, restart tests | Repeat engine-path contracts on macOS/Linux and eventually inside frozen artifacts |
| App configuration | Validated `App` plus compatible `AppConfig`; atomic generated config and raw escape hatch | Stabilize naming/versioning before declaring the higher-level API stable |
| Agent support | Explicit model, primary/subagent modes, safe defaults, exact delegation graph | Add broader model options, exposure/hidden controls, and deeper orchestration only with new flows |
| Tools | Typed Python `Tool`, JSON Schema validation, generated OpenCode shim, authenticated loopback host, typed lifecycle events | Add richer progress/artifact values and production hardening for long-running/concurrent handlers |
| Skills | Packaged `Skill`, validated `SKILL.md`, per-agent discovery policy, live load/application test | Supporting-asset packaging and skill version metadata |
| Permissions | `Access`, approval request/resolution events, bridge/UI reply path, app-local JSONL audit | Richer non-modal approval UI, saved-decision management, and policy migration across engine versions |
| Subagents | Closed delegation graph, exact `task` rules, children/history APIs, typed cancellation | Multi-level scheduling, coordinated child cancellation, and aggregate child progress |
| Providers | Explicit `Model`, catalog/model preflight, existing auth helpers | Profiles, aliases, provider policy, and credential UX |
| MCP | Managed local `MCPServer`, environment/timeout/policy, connection preflight, real stdio fixture call | Remote/OAuth abstractions, secrets UX, reconnect controls, and richer status events |
| Sessions/events | Typed session/run/result APIs; normalized text/tool/approval lifecycle; bridge/UI no longer consumes raw OpenCode events | More OpenCode operations such as summarize/revert/diff only when product flows require them |
| Structured output | `OutputSchema`, decoding, Python revalidation, typed errors, and async completion capture | Remove compatibility fallbacks after upgrading beyond the pinned history defect |
| Workspace safety | Empty engine worktree plus exact named external-folder policies; allow/deny/ask live proof | Read/write combinations beyond the first safe file flow and OS-level sandboxing where available |
| Regression validation | 263 collected tests, six real-engine contracts, and nine real-model live scenarios | Add scheduled CI credentials and an installed/frozen application layer |
| Packaging | Manifest-driven one-folder PyInstaller bundle and bundle manifest | Native installer, signing, and installed-application parity |

### Baseline findings from a live probe

The following G0 probe was run on Windows on 2026-08-08 against the repository's
pinned OpenCode `1.18.13` binary. It used an isolated temporary project, a raw
`opencode.json`, one primary agent named `parity-probe`, and
`opencode/mimo-v2.5-free`. These observations are evidence for the plan, not a
promise that a service-controlled model catalog will never change.

| Probe | Result | Consequence |
| --- | --- | --- |
| Configured agent/model discovery | Pass: `/agent` returned `parity-probe` with the selected Zen model | Spiritus compilation can be checked against the live resolved config, not only against generated JSON |
| Blocking text result | Pass: `/session/:id/message` returned `200` and `DIRECT_OK` in the final text part | G1 must expose a directly awaitable final result |
| Asynchronous stream | Pass: `prompt_async` returned `204`, followed by non-empty part deltas, busy/idle status, and a stored final message | G1 must expose streaming and final output from the same run |
| Visible-output filtering | Raw deltas included reasoning text as well as the final text | Event normalization must correlate part types and must test that reasoning never leaks into the public text stream |
| Restart, resume, and memory | Pass: after stop/start, the same session ID was listed; a follow-up returned the remembered codeword; history held both turns | G1 must recreate the runtime object, not merely reuse an in-memory client, when testing persistence |
| Structured direct result | Pass: the direct response contained `{"status": "STRUCTURED_OK", "count": 7}` | G2 can normalize the engine's `structured` field |
| Structured history | Engine defect confirmed: after a successful schema result, `GET /session/:id/message` returned `400` with an `OutputFormatJsonSchema` validation error | Async bridge runs capture the structured value in the completion SSE event and never query history to recover it |
| Windows data isolation | Root cause confirmed: setting only `HOME` left `.opencode-home` empty; adding `USERPROFILE` and explicit XDG paths redirected all persistent locations | The runtime now applies and tests all of those variables before starting OpenCode |

### Additional implementation findings

- OpenCode `1.18.13` suppresses native approval behavior when the legacy
  `tools: {"*": false}` rule is present. Spiritus explicitly disables the
  pinned built-in tool IDs instead, leaving declared `ask` flows operational.
- A direct `read: ask` rule can cause a model to refuse the tool before an
  engine event exists. Spiritus gives each session an empty
  `.spiritus/worktree` and keeps named workspace folders outside it, so the
  engine's `external_directory` guard produces an exact, brokerable folder
  request. Allow, deny, and `ask -> once` were all exercised with real files.
- Structured output requires OpenCode's internal `StructuredOutput` transport
  tool even when every application tool is disabled. Spiritus treats that as a
  result transport, keeps it enabled, and still validates the returned value in
  Python.
- Agent-specific skills and MCP tools cannot be conclusively enumerated by the
  pinned generic tool-catalog endpoint because it accepts model/provider but no
  agent. Deterministic gates validate files, config, permission, and MCP
  connection status; real-model gates prove the actual skill/MCP calls.
- The first project-local TypeScript tool catalog load can initialize
  OpenCode's plugin runtime and exceed the normal ten-second request budget.
  Only the tool-catalog calls receive a separate sixty-second cold-start limit.

## Executed implementation order

1. **G0 complete: lock down the oracle and runtime prerequisites.** Added the pinned OpenAPI
   route/schema assertions, fix Windows engine-data isolation, and encode the
   raw single-agent scenario in a reusable live test harness. Keep this harness
   independent of the new public API so it remains a compatibility oracle.
2. **G1 complete: build the minimum public surface.** Added the validated `Agent` and
   explicit model definition, compile them to app-local OpenCode config, and
   implement the adapter operations needed for model preflight, session
   create/resume, blocking send, asynchronous send, history, and restart.
3. **G1 complete: normalize events and results.** Introduced the `Session`,
   `RunHandle`, text/status/error/completion events, and final text result needed
   by the scenario. Move the existing JavaScript SSE correlation behavior into
   tested runtime code, then route the built-in bridge/UI through it.
4. **G1 complete: run the same acceptance scenario through Spiritus.** The public path does not pass
   merely because individual unit tests pass. It must stream
   visible text, return the final output, persist history, restart, resume the
   same session, and answer from earlier context. Compare behavioral invariants
   with the raw OpenCode oracle.
5. **G2 complete: add structured task output.** Normalized JSON Schema input, validated
   values, retries, and typed errors. Resolve the pinned engine's structured
   history failure and require the result to remain retrievable after restart.
6. **G3 complete: add permissions and one safe workspace flow.** Compiled named
   capabilities, broker allow/ask/deny, and test both successful and rejected
   access. This gate is mandatory before shipping a general-purpose app.
7. **G4 and G6 complete: add one extension at a time.** The typed Python tool,
   skill, command, and MCP server each have distinct types, adapters, and live
   vertical-slice tests.
8. **G5 complete: add delegation after the singular-agent contract.** Parent and
   child sessions, cancellation, permissions, events, histories, and results
   are covered.
9. **Future: build a small external reference application at each stable public gate.**
   Consumer applications stay outside this repository. Port larger existing
   applications only after the G1-G4 contracts they require are green.
10. **0.0.3: implement the reusable bundle layer.** Packaging preserves the
    runtime contract already proven unfrozen; each consuming application still
    owns its native installer and installed/frozen acceptance test.

This order intentionally avoids designing `App`, `Agent`, `Policy`,
`Workspace`, `Session`, `Run`, tools, and delegation as one large speculative
model. Each type is introduced when an executable scenario requires it.

## Validation strategy

Pytest component coverage and the overall parity flow serve different
purposes. Both are required.

### Test layers

| Layer | Runs by default | Real engine | Real model | Purpose |
| --- | --- | --- | --- | --- |
| Unit/contract | Yes | No | No | Validate public types, config compilation, model IDs, state transitions, event reduction, error mapping, and serialization deterministically |
| Engine contract | In a dedicated CI job | Pinned binary | No | Validate startup, isolation, `/doc`, required routes/schemas, live agent discovery, provider preflight, and empty-session persistence |
| Live parity | Opt-in locally and scheduled/pre-release in CI | Pinned binary | Yes | Prove the complete raw and Spiritus single-agent behavior with no mocked transport or model output |
| Installed application | Later, per release | Bundled binary | Yes | Prove the same public scenario in the packaged Windows application |

Suggested pytest coverage:

| Test file | Essential cases |
| --- | --- |
| `tests/test_agent_api.py` | Agent validation; explicit model parsing; config compilation; raw override precedence; no undeclared default agent |
| `tests/test_session_api.py` | Create/resume; send and await; history mapping; cancel/error states; completion resolves exactly once |
| `tests/test_event_normalization.py` | Delta-before-snapshot and snapshot-before-delta ordering; multiple text parts; no duplication; reasoning filtered; session isolation; idle/error completion |
| `tests/test_opencode_contract.py` | Pinned `/doc` routes and fields; configured agent discovery; provider/model preflight; cross-platform engine data paths |
| `tests/live/test_single_agent_parity.py` | Reusable G1 scenario against raw OpenCode and the Spiritus public API |
| `tests/live/test_structured_output_parity.py` | Valid value, invalid value/retry, typed error, history retrieval, and restart persistence |
| `tests/live/test_workspace_permission_parity.py` | Named-folder allow/deny/ask, approval reply, final read result, and audit persistence |
| `tests/test_tools.py` and `tests/live/test_typed_tool_parity.py` | Typed Python validation/hosting plus real call, progress, result, transcript, and restart |
| `tests/live/test_delegation_parity.py` | Exact task policy, parent/child histories, tool result, and cancellation |
| `tests/test_extensions.py` and `tests/live/test_packaged_extensions_parity.py` | Skill, command, and MCP compilation plus three independent real execution flows |

Add `engine` and `live_opencode` pytest markers. Normal `uv run pytest` must
remain deterministic, headless, credential-free, and model-free. A live run is
explicitly enabled, for example:

```bash
SPIRITUS_RUN_LIVE=1 \
SPIRITUS_TEST_MODEL=opencode/mimo-v2.5-free \
uv run pytest -m live_opencode -v
```

When live mode is not enabled, those tests skip with the enabling command. Once
enabled, a missing engine, model, credential, or network connection is a clear
failure rather than a silent skip. CI can override `SPIRITUS_TEST_MODEL` and
provide credentials without changing the scenario.

### Verification record (2026-08-09)

The complete worktree was validated on Windows with Python `3.11.15`, pinned
OpenCode `1.18.13`, the default Zen provider, and
`opencode/mimo-v2.5-free` unless overridden by `SPIRITUS_TEST_MODEL`:

| Gate command | Result |
| --- | --- |
| `uv run ruff check .` | Pass |
| `uv run pytest` | Pass; 250 passed and 16 opt-in tests skipped (266 collected) |
| `SPIRITUS_RUN_ENGINE=1 uv run pytest -m engine -v` | Pass; 6/6 real-engine, no-model contracts in 42.93 seconds |
| `SPIRITUS_RUN_LIVE=1 uv run pytest -m live_opencode -v` | Pass; 9/9 real-model scenarios in 175.13 seconds |
| `uv build` | Pass; source distribution and wheel built |

The live run includes the raw OpenCode oracle and the public Spiritus path in
the same aggregate suite. Passing only an isolated new feature test is not
treated as sufficient evidence of backward functional parity.

### G1 black-box acceptance scenario

The raw adapter and public Spiritus implementations must both be usable through
the same small test protocol. The assertions concern lifecycle invariants, not
model prose, so nondeterminism does not make the test meaningless.

1. Create a temporary application and durable temporary runtime-data directory.
2. Define exactly one primary agent through the interface under test, with an
   explicit model, a deterministic prompt, and all tools disabled.
3. Start the pinned engine and assert that the resolved agent and model match
   the definition.
4. Create a session and subscribe to its event stream before sending input.
5. Send an asynchronous prompt containing a unique response marker and a
   randomly generated codeword to remember.
6. Assert that busy, visible text delta, final text, completed, and idle states
   are observable; no reasoning delta may appear in the public text stream.
7. Await the same run and assert that its final text agrees with authoritative
   stored history. The marker must be present, but the test must not compare an
   entire nondeterministic response byte for byte.
8. Stop the engine, discard the runtime/client object, and construct a new one
   using the same application data directory.
9. List and resume the original session, then use the blocking/direct result
   path to ask for the codeword. Assert that the result contains the codeword.
10. Fetch history and assert that both user inputs, both assistant outputs, the
    agent/model identity, and stable session ID remain available.

The G1 public abstraction is accepted only when this entire scenario passes.
The G2 test repeats the persistence half with schema-backed output. Later gates
extend the scenario instead of replacing it, so abstraction work cannot quietly
remove behavior the direct OpenCode flow already provided.

## Decisions to preserve

- OpenCode is an engine adapter, not Spiritus's application-facing API.
- Raw OpenCode configuration remains an escape hatch, not the normal workflow.
- Policy is explicit and secure by default for shipped applications.
- Conversation, typed task results, tool progress, approvals, and diagnostics
  are separate event/result types.
- User-visible agent selection and agent-to-agent delegation are separate.
- Application data storage and agent workspace access are separate concepts.
- The pinned OpenCode server's OpenAPI schema is the compatibility contract.
- Experimental or v2 OpenCode features stay behind adapters until verified.
- A successful direct response is not enough; stream, final result, history,
  restart/resume, and retained context are one acceptance unit.
- No abstraction is complete until its raw-engine oracle and public-path
  black-box scenarios pass with equivalent behavioral invariants.
