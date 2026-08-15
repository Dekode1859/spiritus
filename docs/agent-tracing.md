# Agent tracing

Spiritus traces are durable diagnostic timelines for agent behaviour. They are
not replacements for an application's normal structured logs: each record is
connected to a run and session, and says what the agent, model, tools, and
approval system actually did.

Every managed runtime writes append-only JSONL to:

```text
<app project root>/.spiritus/traces.jsonl
```

The file is intentionally readable offline, so a developer can inspect a
failed resume-formatting call after the engine has stopped or a session has been
resumed.

`runtime.diagnostics` is the app-scoped service behind both the session API and
desktop bridge. Its `traces` and `runs` properties expose the focused stores,
while `diagnostics.get/list/events/artifact` provide one query surface for
either execution path.

Every `Session.run` and `Session.send` also creates one durable `RunRecord` in
`.spiritus/runs/<run_id>.json`. The record is the diagnostic answer; its linked
trace events are the supporting evidence. Give an operation name rather than
assembling log messages:

```python
result = await session.run(
    "Convert this resume to the application profile format",
    operation="profile.import",
    output=profile_schema,
)

record = runtime.runs.get(result.run_id)
assert record.operation == "profile.import"
assert record.status == RunStatus.COMPLETED
```

`runtime.runs.list(operation="profile.import", status="failed")` finds prior
incidents; `runtime.runs.events(run_id)` replays their normalized timeline;
and `runtime.runs.artifact(run_id, "agent.output")` reads the local final
output. Application code may add a domain checkpoint with
`runtime.runs.checkpoint(run_id, "output.normalized")` when it has performed a
meaningful non-agent step.

Completed run artifacts are also written under
`.spiritus/artifacts/<run_id>/`, while the run record keeps the queryable
metadata and redacted value. Artifact writes use the same diagnostic output
policy as the run record.

When checkpoints must precede the prompt, start the operation first:

```python
run = await runtime.runs.start(operation="profile.import", agent="profile-pdf")
run.checkpoint("pdf.text_extracted")
result = await run.execute(document_text, output=profile_schema)
```

The desktop bridge exposes the equivalent `agent_run(session_id, agent, model,
text, operation=..., output_schema=...)` call. Existing `send_message` calls
now use it with `operation="chat.message"`, so a UI-originated prompt receives
a run ID and writes the same local diagnostic evidence.

For a schema-bound bridge operation, Spiritus captures the final structured
value from the assistant completion event, validates it, and retains it as
`agent.structured`. `bridge.run_artifact(run_id, "agent.structured")` reads
that result directly. If the pinned OpenCode history endpoint rejects a later
history request for the completed schema run, `bridge.session_history(...)`
returns the locally retained assistant result instead.

```python
from spiritus import FailureLayer, TraceFilter, TraceRenderer

async with app.runtime() as runtime:
    session = await runtime.require_sessions().create(agent="resume-parser")
    result = await session.run("Convert this resume to the application format")

    failures = await session.traces(
        TraceFilter(failure_layers=frozenset({FailureLayer.MODEL, FailureLayer.TOOL}))
    )
    print(TraceRenderer().render_many(failures))
```

The timeline uses stable terminology:

- `run.*` records the lifetime and terminal outcome of one invocation.
- `model.*` records the configured agent/model request and its completed reply.
- `tool.*` records tool input, progress, output, and tool-owned failure.
- `file.written` records a file path only when the normalized tool identity
  proves a write (`write`, `edit`, `patch`, or `apply_patch`).
- `approval.*` records permission asks and the decision made.

Terminal failures are classified by the owning layer—`model`, `tool`,
`permission`, `transport`, `output`, `persistence`, `observability`, `runtime`,
or `cancelled`. A classification states where Spiritus observed the failure; it
does not pretend to diagnose an unproven model or provider root cause. A local
trace-write failure never changes an agent result; inspect
`runtime.traces.last_error` to surface that observability condition.

The durable failure taxonomy is: `input_invalid`, `artifact_read_failed`,
`engine_unavailable`, `model_failed`, `timeout`, `output_parse_failed`,
`output_schema_invalid`, `policy_rejected`, `storage_failed`, and
`runtime_failed`. Schema-validation failures belong to
`application_contract` and expose field paths such as
`skill_buckets[0].category`; Spiritus does not mislabel them as model failures.

`TraceRenderer()` produces an ANSI-coloured, aligned terminal timeline.
Pass `color=False` for CI, saved reports, or terminals without colour support.
Use `TraceFilter` by session, run id, event kind, failure layer, and result
limit; the same filter works on `runtime.traces.entries(...)` and
`session.traces(...)`.

Trace records include prompts and normalized tool arguments so that a developer
can reproduce an agent's decision context. Treat `.spiritus/traces.jsonl` as
application diagnostic data: keep it out of source control and apply the same
retention/access policy as the application data it may contain.

Spiritus records visible final output only. Model reasoning is not added to the
developer UI, trace file, or `agent.output` artifact.

Use `DiagnosticPolicy(capture_inputs=False, capture_outputs=False,
redactions=("secret",))` on `App` or `AppConfig` to omit retained prompts and
final output, and to replace configured literal values in retained trace data.

## Live development terminal

Use the same journal while developing an application:

```powershell
spiritus dev run.py
```

The command launches the entrypoint as a child process, writes a durable
`.spiritus/launches/<launch_id>.json` record, and forwards the child process's
standard output and error. It creates a one-time loopback endpoint and token
for the child; after each trace event is fsynced to `traces.jsonl`, Spiritus
also delivers it to the terminal renderer. Production starts no terminal
subscriber, but writes identical run, trace, and artifact records.

`--level normal` shows run outcomes, checkpoints, and bounded input/output
summaries (character counts, operation, and duration) without raw payload data;
`--level verbose`
includes summarized tool and model metadata, and `--level trace` renders all
payloads permitted by the application's `DiagnosticPolicy`. Model reasoning is
never captured or displayed at any level. Use `--no-color` for CI.

For a failed run, normal output keeps the diagnostic essentials inline: its
failure `kind`, owning layer, and quoted error message. For example,
`RUN.FAILED  kind=model_failed  owner=model  error="Streaming response failed: [502] Upstream error"`.

If the child exits unsuccessfully, Spiritus preserves its stderr tail in the
launch record and appends a `runtime.process_crashed` event to the same journal.
The first version deliberately does not watch or restart files.
