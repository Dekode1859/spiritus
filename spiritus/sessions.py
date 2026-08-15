"""Public session, streaming run, message, and result abstractions."""
from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar

import jsonschema

from .agents import Agent
from .commands import Command
from .events import (
    ApprovalRequested,
    ApprovalResolved,
    EventNormalizer,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunIdle,
    RunStarted,
    ToolCompleted,
    ToolFailed,
    ToolProgress,
    ToolStarted,
)
from .models import Model
from .permissions import ApprovalDecision
from .persistence import ApprovalAuditLog, SessionStore
from .runtime.client import OpenCodeClient, OpenCodeError
from .tracing import (
    Diagnostics,
    FailureKind,
    FailureLayer,
    RunFailure,
    RunStore,
    TraceFilter,
    TraceKind,
    TraceStore,
)

T = TypeVar("T")


def _with_run_id(error: BaseException, run_id: str) -> BaseException:
    """Preserve an exception's established type while making its record findable."""
    try:
        error.run_id = run_id
    except (AttributeError, TypeError):
        pass
    return error


class RunExecutionError(RuntimeError):
    def __init__(self, message: str, *, result: RunResult | None = None):
        super().__init__(message)
        self.result = result
        self.run_id = result.run_id if result is not None else ""


class StructuredOutputError(RunExecutionError):
    def __init__(self, message: str, *, retries: int = 0, result: RunResult | None = None):
        super().__init__(message, result=result)
        self.retries = retries


class OutputValidationError(RunExecutionError):
    pass


class RunCancelledError(RunExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class OutputSchema(Generic[T]):
    schema: Mapping[str, Any]
    decoder: Callable[[Any], T] | None = None

    def decode(self, value: Any) -> T | Any:
        jsonschema.validate(value, dict(self.schema))
        return self.decoder(value) if self.decoder else value


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    session_id: str
    role: str
    parts: tuple[dict, ...]
    agent: str = ""
    model: Model | None = None
    structured: Any = None
    error: Any = None
    parent_id: str = ""
    command: str = ""
    command_arguments: str = ""

    @property
    def text(self) -> str:
        return "".join(
            part.get("text", "")
            for part in self.parts
            if part.get("type") == "text"
        )

    @classmethod
    def from_opencode(cls, payload: dict) -> Message:
        info = payload.get("info", {})
        raw_model = info.get("model") or {
            "providerID": info.get("providerID"),
            "modelID": info.get("modelID"),
        }
        model = None
        if raw_model.get("providerID") and raw_model.get("modelID"):
            model = Model(raw_model["providerID"], raw_model["modelID"])
        return cls(
            id=info.get("id", ""),
            session_id=info.get("sessionID", ""),
            role=info.get("role", ""),
            parts=tuple(payload.get("parts", [])),
            agent=info.get("agent", ""),
            model=model,
            structured=info.get("structured"),
            error=info.get("error"),
            parent_id=info.get("parentID", ""),
        )

    def to_store(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "parts": list(self.parts),
            "agent": self.agent,
            "model": str(self.model) if self.model else "",
            "structured": self.structured,
            "error": self.error,
            "parent_id": self.parent_id,
            "command": self.command,
            "command_arguments": self.command_arguments,
        }

    @classmethod
    def from_store(cls, payload: dict) -> Message:
        return cls(
            id=payload.get("id", ""),
            session_id=payload.get("session_id", ""),
            role=payload.get("role", ""),
            parts=tuple(payload.get("parts", [])),
            agent=payload.get("agent", ""),
            model=Model.parse(payload["model"]) if payload.get("model") else None,
            structured=payload.get("structured"),
            error=payload.get("error"),
            parent_id=payload.get("parent_id", ""),
            command=payload.get("command", ""),
            command_arguments=payload.get("command_arguments", ""),
        )


@dataclass(frozen=True, slots=True)
class SessionInfo:
    id: str
    title: str = ""
    parent_id: str | None = None

    @classmethod
    def from_opencode(cls, payload: dict) -> SessionInfo:
        return cls(
            id=payload["id"],
            title=payload.get("title", ""),
            parent_id=payload.get("parentID"),
        )


@dataclass(frozen=True, slots=True)
class RunResult(Generic[T]):
    message: Message
    output: T | None = None
    run_id: str = ""

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def value(self) -> Any:
        return self.output if self.output is not None else self.message.structured


class ApplicationRun:
    """A named application operation that exists before agent work begins."""

    def __init__(self, session: Session, run_id: str, operation: str):
        self._session = session
        self.run_id = run_id
        self.operation = operation

    def checkpoint(self, name: str, *, detail: Mapping[str, Any] | None = None) -> None:
        self._session._runs.checkpoint(self.run_id, name, detail=detail)
        self._session._traces.append(
            TraceKind.RUN_CHECKPOINT,
            run_id=self.run_id,
            session_id=self._session.id,
            agent=self._session.agent.name,
            model=str(self._session.agent.model),
            data={"name": name, "detail": dict(detail or {})},
        )

    async def execute(
        self,
        prompt: str,
        *,
        output: Mapping[str, Any] | OutputSchema | None = None,
        retry_count: int = 2,
        **extra: Any,
    ) -> RunResult:
        return await self._session.run(
            prompt,
            output=output,
            retry_count=retry_count,
            operation=self.operation,
            _run_id=self.run_id,
            **extra,
        )

    async def send(self, prompt: str) -> RunHandle:
        return await self._session.send(
            prompt, operation=self.operation, _run_id=self.run_id
        )


class RunHandle:
    """One asynchronous prompt with an event stream and final result."""

    _END = object()

    def __init__(
        self,
        client: OpenCodeClient,
        store: SessionStore,
        audit: ApprovalAuditLog,
        traces: TraceStore,
        runs: RunStore,
        session_id: str,
        agent: Agent,
        run_id: str,
    ):
        self._client = client
        self._store = store
        self._audit = audit
        self._traces = traces
        self._runs = runs
        self.session_id = session_id
        self.agent = agent
        self.run_id = run_id
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[RunEvent | object] = asyncio.Queue()
        self._future: asyncio.Future[RunResult] = self._loop.create_future()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._completed_message_id = ""
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._events_claimed = False
        self._approvals: dict[str, ApprovalRequested] = {}
        self._tool_arguments: dict[str, dict[str, Any]] = {}
        self._cancelled = False
        self._cancel_settled = threading.Event()
        self._cancel_settled.set()

    async def _start(self) -> None:
        self._thread.start()
        connected = await asyncio.to_thread(self._ready.wait, 10)
        if not connected:
            self._fail(TimeoutError("OpenCode event stream did not become ready"))
            raise TimeoutError("OpenCode event stream did not become ready")

    def _publish(self, event: RunEvent) -> None:
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _close_events(self) -> None:
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, self._END)

    def _resolve(self, result: RunResult) -> None:
        def finish() -> None:
            if not self._future.done():
                self._future.set_result(result)

        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(finish)

    def _fail(self, error: BaseException, *, record: bool = True) -> None:
        error = _with_run_id(error, self.run_id)
        self._stop.set()
        if not record:
            pass
        elif isinstance(error, RunCancelledError):
            self._runs.cancel(self.run_id)
            self._traces.append(
                TraceKind.RUN_CANCELLED,
                run_id=self.run_id,
                session_id=self.session_id,
                agent=self.agent.name,
                model=str(self.agent.model),
                failure_layer=FailureLayer.CANCELLED,
                data={"message": str(error)},
            )
        else:
            layer = FailureLayer.TRANSPORT if isinstance(error, OpenCodeError) else FailureLayer.RUNTIME
            kind = (
                FailureKind.ENGINE_UNAVAILABLE
                if isinstance(error, OpenCodeError)
                else FailureKind.RUNTIME_FAILED
            )
            self._runs.fail(
                self.run_id,
                RunFailure(kind, "spiritus_runtime", str(error)),
                stage="runtime.failed",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                run_id=self.run_id,
                session_id=self.session_id,
                agent=self.agent.name,
                model=str(self.agent.model),
                failure_layer=layer,
                data={"message": str(error), "exception": type(error).__name__},
            )

        def finish() -> None:
            if not self._future.done():
                self._future.set_exception(error)

        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(finish)
        self._close_events()

    def _listen(self) -> None:
        normalizer = EventNormalizer(self.session_id)
        try:
            for envelope in self._client.events(ready=self._ready, stop=self._stop):
                should_finish = False
                for event in normalizer.feed(envelope):
                    self._trace_event(event)
                    if isinstance(event, ApprovalRequested):
                        self._approvals[event.request_id] = event
                        self._audit.append(
                            "approval.requested",
                            session_id=self.session_id,
                            request_id=event.request_id,
                            permission=event.permission,
                            patterns=list(event.patterns),
                            metadata=event.metadata,
                            always=list(event.always),
                        )
                    elif isinstance(event, ApprovalResolved):
                        self._audit.append(
                            "approval.resolved",
                            session_id=self.session_id,
                            request_id=event.request_id,
                            decision=event.decision.value,
                        )
                    self._publish(event)
                    if isinstance(event, RunCompleted):
                        self._completed_message_id = event.message_id
                    elif isinstance(event, RunFailed):
                        self._fail(RuntimeError(event.message), record=False)
                        return
                    elif isinstance(event, RunIdle):
                        should_finish = True
                if should_finish:
                    # OpenCode can publish session.idle while the abort HTTP
                    # response is still in flight. Wait for that response so a
                    # failed/no-op abort does not turn a completed run into a
                    # false cancellation.
                    if not self._cancel_settled.is_set():
                        timeout = float(getattr(self._client, "request_timeout", 30)) + 1
                        self._cancel_settled.wait(timeout)
                    if self._cancelled:
                        self._fail(RunCancelledError("run was cancelled"))
                        return
                    result = self._load_result()
                    self._resolve(result)
                    self._runs.complete(
                        self.run_id, artifacts={"agent.output": result.text}
                    )
                    self._traces.append(
                        TraceKind.RUN_COMPLETED,
                        run_id=self.run_id,
                        session_id=self.session_id,
                        agent=self.agent.name,
                        model=str(self.agent.model),
                        message_id=result.message.id,
                        data={"text_length": len(result.text)},
                    )
                    self._close_events()
                    return
            if not self._stop.is_set():
                self._fail(RuntimeError("OpenCode event stream ended before session.idle"))
        except BaseException as exc:
            self._ready.set()
            self._fail(exc)

    def _trace_event(self, event: RunEvent) -> None:
        common = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "agent": self.agent.name,
            "model": str(self.agent.model),
        }
        if isinstance(event, RunStarted):
            self._runs.checkpoint(self.run_id, "agent.started")
            self._traces.append(TraceKind.RUN_STARTED, **common)
        elif isinstance(event, RunCompleted):
            self._runs.checkpoint(self.run_id, "agent.completed")
            self._traces.append(
                TraceKind.MODEL_COMPLETED, message_id=event.message_id, **common
            )
        elif isinstance(event, ToolStarted):
            self._tool_arguments[event.call_id] = event.arguments
            self._traces.append(
                TraceKind.TOOL_STARTED,
                message_id=event.message_id,
                call_id=event.call_id,
                data={"tool": event.tool, "arguments": event.arguments},
                **common,
            )
        elif isinstance(event, ToolProgress):
            self._traces.append(
                TraceKind.TOOL_PROGRESS,
                message_id=event.message_id,
                call_id=event.call_id,
                data={"tool": event.tool, "title": event.title, "metadata": event.metadata},
                **common,
            )
        elif isinstance(event, ToolCompleted):
            self._traces.append(
                TraceKind.TOOL_COMPLETED,
                message_id=event.message_id,
                call_id=event.call_id,
                data={"tool": event.tool, "output": event.output, "metadata": event.metadata},
                **common,
            )
            for path in _written_paths(
                event.tool, self._tool_arguments.get(event.call_id, {})
            ):
                self._traces.append(
                    TraceKind.FILE_WRITTEN,
                    call_id=event.call_id,
                    data={"tool": event.tool, "path": path},
                    **common,
                )
        elif isinstance(event, ToolFailed):
            self._traces.append(
                TraceKind.TOOL_FAILED,
                message_id=event.message_id,
                call_id=event.call_id,
                failure_layer=FailureLayer.TOOL,
                data={"tool": event.tool, "error": event.error, "metadata": event.metadata},
                **common,
            )
        elif isinstance(event, ApprovalRequested):
            self._traces.append(
                TraceKind.APPROVAL_REQUESTED,
                message_id=event.message_id,
                call_id=event.call_id,
                data={"permission": event.permission, "patterns": event.patterns},
                **common,
            )
        elif isinstance(event, ApprovalResolved):
            self._traces.append(
                TraceKind.APPROVAL_RESOLVED,
                data={"request_id": event.request_id, "decision": event.decision.value},
                **common,
            )
        elif isinstance(event, RunFailed):
            self._runs.fail(
                self.run_id,
                RunFailure(FailureKind.MODEL_FAILED, "model", event.message),
                stage="agent.completed",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                failure_layer=FailureLayer.MODEL,
                data={"message": event.message, "data": event.data},
                **common,
            )
    def _load_result(self) -> RunResult:
        messages = [Message.from_opencode(item) for item in self._client.messages(self.session_id)]
        self._store.replace(self.session_id, [message.to_store() for message in messages])
        if self._completed_message_id:
            for message in messages:
                if message.id == self._completed_message_id:
                    return RunResult(message, run_id=self.run_id)
        for message in reversed(messages):
            if message.role == "assistant":
                return RunResult(message, run_id=self.run_id)
        raise RuntimeError("OpenCode completed without an assistant message")

    async def events(self) -> AsyncIterator[RunEvent]:
        if self._events_claimed:
            raise RuntimeError("a run's event stream can only be consumed once")
        self._events_claimed = True
        while True:
            event = await self._queue.get()
            if event is self._END:
                return
            yield event  # type: ignore[misc]

    async def result(self) -> RunResult:
        return await asyncio.shield(self._future)

    async def wait(self) -> RunResult:
        return await self.result()

    async def respond(
        self,
        request: ApprovalRequested | str,
        decision: ApprovalDecision | str,
        *,
        message: str | None = None,
    ) -> None:
        """Resolve one approval emitted by this run."""
        request_id = request.request_id if isinstance(request, ApprovalRequested) else request
        if request_id not in self._approvals:
            raise ValueError(f"unknown approval request {request_id!r} for this run")
        parsed = ApprovalDecision.parse(decision)
        await asyncio.to_thread(
            self._client.reply_permission,
            request_id,
            parsed.value,
            message=message,
        )

    async def cancel(self) -> bool:
        """Abort this run and resolve its result with ``RunCancelledError``."""
        self._cancel_settled.clear()
        self._cancelled = True
        try:
            aborted = await asyncio.to_thread(self._client.abort, self.session_id)
        except BaseException:
            self._cancelled = False
            raise
        else:
            if not aborted:
                self._cancelled = False
            return aborted
        finally:
            self._cancel_settled.set()


def _written_paths(tool: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Report paths only when the normalized tool identity proves a write."""
    if tool.lower() not in {"write", "edit", "patch", "apply_patch"}:
        return ()
    paths: list[str] = []
    for key in ("path", "file", "filepath"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    return tuple(paths)


def _field_path(path: Any) -> str:
    """Use schema paths developers recognize, such as ``items[0].name``."""
    result = ""
    for item in path:
        result += f"[{item}]" if isinstance(item, int) else f".{item}"
    return result.lstrip(".") or "<root>"


class Session:
    def __init__(
        self,
        client: OpenCodeClient,
        store: SessionStore,
        audit: ApprovalAuditLog,
        traces: TraceStore,
        runs: RunStore,
        info: SessionInfo,
        agent: Agent,
        commands: Mapping[str, Command],
        diagnostics: Diagnostics | None = None,
    ):
        self._client = client
        self._store = store
        self._audit = audit
        self._traces = traces
        self._runs = runs
        self.diagnostics = diagnostics or Diagnostics(
            store.root.parent, traces=traces, runs=runs
        )
        self.info = info
        self.agent = agent
        self._commands = commands

    @property
    def id(self) -> str:
        return self.info.id

    def _prompt_body(self, prompt: str, **extra: Any) -> dict:
        text = prompt.strip()
        if not text:
            raise ValueError("prompt cannot be empty")
        return {
            "agent": self.agent.name,
            "model": self.agent.model.as_request(),
            "parts": [{"type": "text", "text": text}],
            **extra,
        }

    def start_run(self, operation: str) -> ApplicationRun:
        run_id = f"run_{uuid.uuid4().hex}"
        self._runs.create(
            run_id=run_id,
            operation=operation,
            session_id=self.id,
            agent=self.agent.name,
            model=str(self.agent.model),
        )
        return ApplicationRun(self, run_id, operation)

    async def send(
        self,
        prompt: str,
        *,
        operation: str = "agent.stream",
        _run_id: str | None = None,
    ) -> RunHandle:
        body = self._prompt_body(prompt)
        run_id = _run_id or f"run_{uuid.uuid4().hex}"
        if _run_id is None:
            self._runs.create(
                run_id=run_id, operation=operation, session_id=self.id,
                agent=self.agent.name, model=str(self.agent.model),
            )
        self._traces.append(
            TraceKind.MODEL_REQUESTED,
            run_id=run_id,
            session_id=self.id,
            agent=self.agent.name,
            model=str(self.agent.model),
            data={"mode": "stream", "prompt": prompt.strip()},
        )
        handle = RunHandle(
            self._client, self._store, self._audit, self._traces, self._runs,
            self.id, self.agent, run_id,
        )
        await handle._start()
        try:
            await asyncio.to_thread(
                self._client.prompt_async,
                self.id,
                body,
            )
        except BaseException as exc:
            handle._fail(exc)
            raise
        return handle

    async def run(
        self,
        prompt: str,
        *,
        output: Mapping[str, Any] | OutputSchema | None = None,
        retry_count: int = 2,
        operation: str = "agent.run",
        _run_id: str | None = None,
        **extra: Any,
    ) -> RunResult:
        output_schema: OutputSchema | None
        if output is None:
            output_schema = None
        elif isinstance(output, OutputSchema):
            output_schema = output
        elif isinstance(output, Mapping):
            output_schema = OutputSchema(output)
        else:
            raise TypeError("output must be a JSON Schema mapping or OutputSchema")
        if retry_count < 0:
            raise ValueError("retry_count cannot be negative")
        if output_schema is not None:
            if "format" in extra:
                raise ValueError("format cannot be supplied together with output")
            extra["format"] = {
                "type": "json_schema",
                "schema": dict(output_schema.schema),
                "retryCount": retry_count,
            }
        text = prompt.strip()
        body = self._prompt_body(text, **extra)
        run_id = _run_id or f"run_{uuid.uuid4().hex}"
        if _run_id is None:
            self._runs.create(
                run_id=run_id, operation=operation, session_id=self.id,
                agent=self.agent.name, model=str(self.agent.model),
            )
        self._traces.append(
            TraceKind.RUN_STARTED,
            run_id=run_id,
            session_id=self.id,
            agent=self.agent.name,
            model=str(self.agent.model),
        )
        self._traces.append(
            TraceKind.MODEL_REQUESTED,
            run_id=run_id,
            session_id=self.id,
            agent=self.agent.name,
            model=str(self.agent.model),
            data={"mode": "direct", "prompt": text},
        )
        try:
            payload = await asyncio.to_thread(
                self._client.prompt,
                self.id,
                body,
            )
        except OpenCodeError as exc:
            self._runs.fail(
                run_id,
                RunFailure(FailureKind.ENGINE_UNAVAILABLE, "engine", str(exc)),
                stage="agent.requested",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                run_id=run_id,
                session_id=self.id,
                agent=self.agent.name,
                model=str(self.agent.model),
                failure_layer=FailureLayer.TRANSPORT,
                data={"message": str(exc), "status": exc.status, "data": exc.data},
            )
            raise _with_run_id(exc, run_id) from None
        except BaseException as exc:
            self._runs.fail(
                run_id,
                RunFailure(FailureKind.RUNTIME_FAILED, "spiritus_runtime", str(exc)),
                stage="runtime.failed",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                run_id=run_id,
                session_id=self.id,
                agent=self.agent.name,
                model=str(self.agent.model),
                failure_layer=FailureLayer.RUNTIME,
                data={"message": str(exc), "exception": type(exc).__name__},
            )
            raise _with_run_id(exc, run_id) from None
        message = Message.from_opencode(payload)
        user = Message(
            id=message.parent_id or f"spiritus_user_{uuid.uuid4().hex}",
            session_id=self.id,
            role="user",
            parts=({"type": "text", "text": text},),
            agent=self.agent.name,
            model=self.agent.model,
        )
        self._store.append(self.id, [user.to_store(), message.to_store()])
        result = RunResult(message, run_id=run_id)
        if message.error:
            name = message.error.get("name", "") if isinstance(message.error, dict) else ""
            data = message.error.get("data", {}) if isinstance(message.error, dict) else {}
            error_message = data.get("message") or "OpenCode run failed"
            if name == "StructuredOutputError":
                self._runs.fail(
                    run_id,
                    RunFailure(FailureKind.OUTPUT_SCHEMA_INVALID, "application_contract", error_message),
                    stage="output.validated",
                )
                self._traces.append(
                    TraceKind.RUN_FAILED,
                    run_id=run_id,
                    session_id=self.id,
                    agent=self.agent.name,
                    model=str(self.agent.model),
                    failure_layer=FailureLayer.OUTPUT,
                    data={"message": error_message, "error": message.error},
                )
                raise StructuredOutputError(
                    error_message,
                    retries=data.get("retries", 0),
                    result=result,
                )
            self._runs.fail(
                run_id,
                RunFailure(FailureKind.MODEL_FAILED, "model", error_message),
                stage="agent.completed",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                run_id=run_id,
                session_id=self.id,
                agent=self.agent.name,
                model=str(self.agent.model),
                failure_layer=FailureLayer.MODEL,
                data={"message": error_message, "error": message.error},
            )
            raise RunExecutionError(error_message, result=result)
        self._runs.checkpoint(run_id, "agent.completed")
        if output_schema is not None:
            if message.structured is None:
                self._runs.fail(
                    run_id,
                    RunFailure(
                        FailureKind.OUTPUT_PARSE_FAILED,
                        "application_contract",
                        "OpenCode returned no structured output",
                    ),
                    stage="output.parsed",
                )
                self._traces.append(
                    TraceKind.RUN_FAILED,
                    run_id=run_id,
                    session_id=self.id,
                    agent=self.agent.name,
                    model=str(self.agent.model),
                    failure_layer=FailureLayer.OUTPUT,
                    data={"message": "OpenCode returned no structured output"},
                )
                raise StructuredOutputError(
                    "OpenCode returned no structured output",
                    result=result,
                )
            self._runs.checkpoint(run_id, "output.parsed")
            try:
                value = output_schema.decode(message.structured)
            except (jsonschema.ValidationError, jsonschema.SchemaError, ValueError) as exc:
                paths = (_field_path(exc.absolute_path),) if isinstance(
                    exc, jsonschema.ValidationError
                ) else ()
                self._runs.fail(
                    run_id,
                    RunFailure(
                        FailureKind.OUTPUT_SCHEMA_INVALID,
                        "application_contract",
                        str(exc),
                        paths,
                    ),
                    stage="output.validated",
                )
                self._traces.append(
                    TraceKind.RUN_FAILED,
                    run_id=run_id,
                    session_id=self.id,
                    agent=self.agent.name,
                    model=str(self.agent.model),
                    failure_layer=FailureLayer.OUTPUT,
                    data={"message": str(exc), "exception": type(exc).__name__},
                )
                raise OutputValidationError(str(exc), result=result) from exc
            self._runs.checkpoint(run_id, "output.validated")
            result = RunResult(message, value, run_id)
        self._runs.complete(run_id, artifacts={"agent.output": result.text})
        self._traces.append(
            TraceKind.MODEL_COMPLETED,
            run_id=run_id,
            session_id=self.id,
            agent=self.agent.name,
            model=str(self.agent.model),
            message_id=message.id,
            data={"text_length": len(result.text), "structured": result.value is not None},
        )
        self._traces.append(
            TraceKind.RUN_COMPLETED,
            run_id=run_id,
            session_id=self.id,
            agent=self.agent.name,
            model=str(self.agent.model),
            message_id=message.id,
        )
        return result

    async def traces(self, trace_filter: TraceFilter | None = None):
        """Read this session's durable agent timeline, optionally filtered."""
        if trace_filter is not None and trace_filter.session_id not in {None, self.id}:
            raise ValueError("session trace filter targets a different session")
        selected = trace_filter or TraceFilter()
        selected = TraceFilter(
            session_id=self.id,
            launch_id=selected.launch_id,
            run_id=selected.run_id,
            kinds=selected.kinds,
            failure_layers=selected.failure_layers,
            limit=selected.limit,
        )
        return await asyncio.to_thread(self._traces.entries, selected)

    async def history(self) -> list[Message]:
        stored = await asyncio.to_thread(self._store.load, self.id)
        try:
            payload = await asyncio.to_thread(self._client.messages, self.id)
        except OpenCodeError:
            if stored:
                return [Message.from_store(item) for item in stored]
            raise
        messages = [Message.from_opencode(item) for item in payload]
        stored_commands = {
            item.get("id"): item
            for item in stored
            if item.get("id") and item.get("command")
        }
        messages = [
            replace(
                message,
                command=stored_commands[message.id].get("command", ""),
                command_arguments=stored_commands[message.id].get(
                    "command_arguments", ""
                ),
            )
            if message.id in stored_commands
            else message
            for message in messages
        ]
        await asyncio.to_thread(
            self._store.replace,
            self.id,
            [message.to_store() for message in messages],
        )
        return messages

    async def run_command(self, name: str, arguments: str = "") -> RunResult:
        """Execute one app-declared command and persist its normalized result."""
        try:
            command = self._commands[name]
        except KeyError as exc:
            raise ValueError(f"unknown command {name!r}") from exc
        body = {"command": command.name, "arguments": arguments}
        selected_agent = command.agent or self.agent.name
        selected_model = command.model or self.agent.model
        run_id = f"run_{uuid.uuid4().hex}"
        self._runs.create(
            run_id=run_id, operation=f"command.{command.name}", session_id=self.id,
            agent=selected_agent, model=str(selected_model),
        )
        self._traces.append(
            TraceKind.RUN_STARTED,
            run_id=run_id,
            session_id=self.id,
            agent=selected_agent,
            model=str(selected_model),
        )
        self._traces.append(
            TraceKind.MODEL_REQUESTED,
            run_id=run_id,
            session_id=self.id,
            agent=selected_agent,
            model=str(selected_model),
            data={"mode": "command", "command": command.name, "arguments": arguments},
        )
        try:
            payload = await asyncio.to_thread(self._client.command, self.id, body)
        except OpenCodeError as exc:
            self._runs.fail(
                run_id,
                RunFailure(FailureKind.ENGINE_UNAVAILABLE, "engine", str(exc)),
                stage="agent.requested",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                run_id=run_id,
                session_id=self.id,
                agent=selected_agent,
                model=str(selected_model),
                failure_layer=FailureLayer.TRANSPORT,
                data={"message": str(exc), "status": exc.status, "data": exc.data},
            )
            raise _with_run_id(exc, run_id) from None
        except BaseException as exc:
            self._runs.fail(
                run_id,
                RunFailure(FailureKind.RUNTIME_FAILED, "spiritus_runtime", str(exc)),
                stage="runtime.failed",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                run_id=run_id,
                session_id=self.id,
                agent=selected_agent,
                model=str(selected_model),
                failure_layer=FailureLayer.RUNTIME,
                data={"message": str(exc), "exception": type(exc).__name__},
            )
            raise _with_run_id(exc, run_id) from None
        message = Message.from_opencode(payload)
        user = Message(
            id=message.parent_id or f"spiritus_user_{uuid.uuid4().hex}",
            session_id=self.id,
            role="user",
            parts=({"type": "text", "text": f"/{name} {arguments}".rstrip()},),
            agent=command.agent or self.agent.name,
            model=command.model or self.agent.model,
            command=command.name,
            command_arguments=arguments,
        )
        self._store.append(self.id, [user.to_store(), message.to_store()])
        result = RunResult(message, run_id=run_id)
        if message.error:
            data = message.error.get("data", {}) if isinstance(message.error, dict) else {}
            self._runs.fail(
                run_id,
                RunFailure(
                    FailureKind.MODEL_FAILED,
                    "model",
                    data.get("message") or "OpenCode command failed",
                ),
                stage="agent.completed",
            )
            self._traces.append(
                TraceKind.RUN_FAILED,
                run_id=run_id,
                session_id=self.id,
                agent=selected_agent,
                model=str(selected_model),
                failure_layer=FailureLayer.MODEL,
                data={"message": data.get("message") or "OpenCode command failed"},
            )
            raise RunExecutionError(
                data.get("message") or "OpenCode command failed",
                result=result,
            )
        self._runs.checkpoint(run_id, "agent.completed")
        self._runs.complete(run_id, artifacts={"agent.output": result.text})
        self._traces.append(
            TraceKind.MODEL_COMPLETED,
            run_id=run_id,
            session_id=self.id,
            agent=selected_agent,
            model=str(selected_model),
            message_id=message.id,
            data={"text_length": len(result.text), "command": command.name},
        )
        self._traces.append(
            TraceKind.RUN_COMPLETED,
            run_id=run_id,
            session_id=self.id,
            agent=selected_agent,
            model=str(selected_model),
            message_id=message.id,
        )
        return result

    async def delete(self) -> None:
        await asyncio.to_thread(self._client.delete_session, self.id)

    async def children(self) -> list[SessionInfo]:
        payload = await asyncio.to_thread(self._client.children, self.id)
        return [SessionInfo.from_opencode(item) for item in payload]

    async def abort(self) -> bool:
        return await asyncio.to_thread(self._client.abort, self.id)


class SessionManager:
    def __init__(
        self,
        client: OpenCodeClient,
        agents: dict[str, Agent],
        default_agent: str,
        store: SessionStore,
        audit: ApprovalAuditLog | None = None,
        commands: Mapping[str, Command] | None = None,
        traces: TraceStore | None = None,
        runs: RunStore | None = None,
        diagnostics: Diagnostics | None = None,
    ):
        self._client = client
        self._agents = agents
        self._default_agent = default_agent
        self._store = store
        self._audit = audit or ApprovalAuditLog(store.root.parent)
        self.diagnostics = diagnostics or Diagnostics(
            store.root.parent, traces=traces, runs=runs
        )
        self.traces = self.diagnostics.traces
        self.runs = self.diagnostics.runs
        self._commands = dict(commands or {})

    def _agent(self, name: str | None) -> Agent:
        selected = name or self._default_agent
        try:
            return self._agents[selected]
        except KeyError as exc:
            raise ValueError(f"unknown agent {selected!r}") from exc

    async def create(self, *, agent: str | None = None, title: str = "") -> Session:
        body = {"title": title} if title else {}
        payload = await asyncio.to_thread(self._client.create_session, body)
        return Session(
            self._client,
            self._store,
            self._audit,
            self.traces,
            self.runs,
            SessionInfo.from_opencode(payload),
            self._agent(agent),
            self._commands,
            self.diagnostics,
        )

    async def resume(self, session_id: str, *, agent: str | None = None) -> Session:
        payload = await asyncio.to_thread(self._client.session, session_id)
        return Session(
            self._client,
            self._store,
            self._audit,
            self.traces,
            self.runs,
            SessionInfo.from_opencode(payload),
            self._agent(agent),
            self._commands,
            self.diagnostics,
        )

    async def list(self) -> list[SessionInfo]:
        payload = await asyncio.to_thread(self._client.sessions)
        return [SessionInfo.from_opencode(item) for item in payload]


class RunManager:
    """Runtime-level entry point for named, checkpointed application runs."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions
        self.diagnostics = sessions.diagnostics
        self._store = self.diagnostics.runs

    async def start(
        self,
        *,
        operation: str,
        agent: str | None = None,
        session_id: str | None = None,
        title: str = "",
    ) -> ApplicationRun:
        session = (
            await self._sessions.resume(session_id, agent=agent)
            if session_id
            else await self._sessions.create(agent=agent, title=title)
        )
        return session.start_run(operation)

    def get(self, run_id: str):
        return self._store.get(run_id)

    def list(self, **filters):
        return self._store.list(**filters)

    def events(self, run_id: str):
        return self._store.events(run_id)

    def artifact(self, run_id: str, name: str):
        return self._store.artifact(run_id, name)

    def checkpoint(self, run_id: str, name: str, **kwargs):
        return self._store.checkpoint(run_id, name, **kwargs)


__all__ = [
    "Message",
    "OutputSchema",
    "OutputValidationError",
    "RunExecutionError",
    "RunCancelledError",
    "RunHandle",
    "RunManager",
    "ApplicationRun",
    "RunResult",
    "Session",
    "SessionInfo",
    "SessionManager",
    "StructuredOutputError",
]
