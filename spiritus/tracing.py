"""Persistent, queryable diagnostic timelines for agent runs.

This is deliberately an *agent trace*, not an application logger.  A trace
preserves the causal sequence that a developer needs to explain a probabilistic
run: which model was asked, approvals, tool activity, terminal outcome, and the
layer that failed.  The JSONL file is app-local so it can be inspected without
the OpenCode process that produced it.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DiagnosticPolicy:
    """Local diagnostic-data capture and literal-redaction policy.

    Applications opt out of retaining prompts or final output explicitly; raw
    model reasoning never enters this policy because Spiritus does not receive
    it from the normalized event layer.
    """

    capture_inputs: bool = True
    capture_outputs: bool = True
    redactions: tuple[str, ...] = ()

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.redactions:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, Mapping):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact(item) for item in value]
        return value

    def trace_data(self, data: Mapping[str, Any]) -> Mapping[str, Any]:
        filtered = dict(data)
        if not self.capture_inputs:
            for key in ("prompt", "arguments", "input"):
                filtered.pop(key, None)
        if not self.capture_outputs:
            for key in ("output", "result"):
                filtered.pop(key, None)
        return self.redact(filtered)

    def artifacts(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        filtered = dict(values)
        if not self.capture_outputs:
            filtered.pop("agent.output", None)
            filtered.pop("agent.structured", None)
        return self.redact(filtered)


class TraceKind(StrEnum):
    RUN_STARTED = "run.started"
    RUN_CHECKPOINT = "run.checkpoint"
    MODEL_REQUESTED = "model.requested"
    MODEL_COMPLETED = "model.completed"
    RUN_COMPLETED = "run.completed"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    FILE_WRITTEN = "file.written"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUNTIME_PROCESS_CRASHED = "runtime.process_crashed"


class FailureLayer(StrEnum):
    """The owning layer for a terminal failure, never a guessed root cause."""

    MODEL = "model"
    TOOL = "tool"
    PERMISSION = "permission"
    TRANSPORT = "transport"
    OUTPUT = "output"
    PERSISTENCE = "persistence"
    OBSERVABILITY = "observability"
    CANCELLED = "cancelled"
    RUNTIME = "runtime"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class FailureKind(StrEnum):
    INPUT_INVALID = "input_invalid"
    ARTIFACT_READ_FAILED = "artifact_read_failed"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    MODEL_FAILED = "model_failed"
    TIMEOUT = "timeout"
    OUTPUT_PARSE_FAILED = "output_parse_failed"
    OUTPUT_SCHEMA_INVALID = "output_schema_invalid"
    POLICY_REJECTED = "policy_rejected"
    STORAGE_FAILED = "storage_failed"
    RUNTIME_FAILED = "runtime_failed"


@dataclass(frozen=True, slots=True)
class RunFailure:
    kind: FailureKind
    owner: str
    message: str
    field_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunStage:
    name: str
    status: StageStatus
    time: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The one durable diagnostic result for an application operation."""

    run_id: str
    operation: str
    status: RunStatus
    session_id: str
    agent: str
    model: str
    started_at: str
    completed_at: str | None = None
    failure: RunFailure | None = None
    stages: tuple[RunStage, ...] = ()
    artifacts: Mapping[str, Any] = field(default_factory=dict)

    def to_store(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "operation": self.operation,
            "status": self.status.value,
            "session_id": self.session_id,
            "agent": self.agent,
            "model": self.model,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "failure": asdict(self.failure) if self.failure else None,
            "stages": [
                {"name": item.name, "status": item.status.value, "time": item.time, "detail": item.detail}
                for item in self.stages
            ],
            "artifacts": dict(self.artifacts),
        }

    @classmethod
    def from_store(cls, payload: Mapping[str, Any]) -> RunRecord:
        failure = payload.get("failure")
        return cls(
            run_id=str(payload["run_id"]),
            operation=str(payload["operation"]),
            status=RunStatus(payload["status"]),
            session_id=str(payload["session_id"]),
            agent=str(payload["agent"]),
            model=str(payload["model"]),
            started_at=str(payload["started_at"]),
            completed_at=payload.get("completed_at"),
            failure=RunFailure(
                FailureKind(failure["kind"]),
                str(failure["owner"]),
                str(failure["message"]),
                tuple(failure.get("field_paths") or ()),
            ) if failure else None,
            stages=tuple(
                RunStage(
                    str(item["name"]),
                    StageStatus(item["status"]),
                    str(item["time"]),
                    dict(item.get("detail") or {}),
                )
                for item in payload.get("stages", [])
            ),
            artifacts=dict(payload.get("artifacts") or {}),
        )


@dataclass(frozen=True, slots=True)
class TraceRecord:
    id: str
    time: str
    kind: TraceKind
    run_id: str
    session_id: str
    launch_id: str = ""
    agent: str = ""
    model: str = ""
    message_id: str = ""
    call_id: str = ""
    failure_layer: FailureLayer | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_store(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["failure_layer"] = (
            self.failure_layer.value if self.failure_layer is not None else None
        )
        return payload

    @classmethod
    def from_store(cls, payload: Mapping[str, Any]) -> TraceRecord:
        layer = payload.get("failure_layer")
        return cls(
            id=str(payload["id"]),
            time=str(payload["time"]),
            kind=TraceKind(payload["kind"]),
            run_id=str(payload["run_id"]),
            session_id=str(payload["session_id"]),
            launch_id=str(payload.get("launch_id", "")),
            agent=str(payload.get("agent", "")),
            model=str(payload.get("model", "")),
            message_id=str(payload.get("message_id", "")),
            call_id=str(payload.get("call_id", "")),
            failure_layer=FailureLayer(layer) if layer else None,
            data=dict(payload.get("data") or {}),
        )


@dataclass(frozen=True, slots=True)
class TraceFilter:
    """Portable filters for reviewing one session or a class of agent activity."""

    session_id: str | None = None
    launch_id: str | None = None
    run_id: str | None = None
    kinds: frozenset[TraceKind] | None = None
    failure_layers: frozenset[FailureLayer] | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 1:
            raise ValueError("trace filter limit must be positive")
        if self.kinds is not None:
            object.__setattr__(
                self, "kinds", frozenset(TraceKind(kind) for kind in self.kinds)
            )
        if self.failure_layers is not None:
            object.__setattr__(
                self,
                "failure_layers",
                frozenset(FailureLayer(layer) for layer in self.failure_layers),
            )

    def matches(self, record: TraceRecord) -> bool:
        return (
            (self.session_id is None or record.session_id == self.session_id)
            and (self.launch_id is None or record.launch_id == self.launch_id)
            and (self.run_id is None or record.run_id == self.run_id)
            and (self.kinds is None or record.kind in self.kinds)
            and (
                self.failure_layers is None
                or record.failure_layer in self.failure_layers
            )
        )


class TraceStore:
    """Append-only agent trace storage at ``.spiritus/traces.jsonl``."""

    def __init__(self, root: Path, policy: DiagnosticPolicy | None = None):
        self.path = Path(root) / "traces.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._last_error: str | None = None
        self.policy = policy or DiagnosticPolicy()

    @property
    def last_error(self) -> str | None:
        """The most recent local trace-write failure, if any.

        Observability must not change an agent result, so a disk write failure
        is retained in memory instead of being re-raised into the run.
        """
        with self._lock:
            return self._last_error

    def append(
        self,
        kind: TraceKind | str,
        *,
        run_id: str,
        session_id: str,
        launch_id: str | None = None,
        agent: str = "",
        model: str = "",
        message_id: str = "",
        call_id: str = "",
        failure_layer: FailureLayer | str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> TraceRecord:
        record = TraceRecord(
            id=f"trc_{uuid.uuid4().hex}",
            time=datetime.now(UTC).isoformat(),
            kind=TraceKind(kind),
            run_id=run_id,
            session_id=session_id,
            launch_id=launch_id if launch_id is not None else os.environ.get("SPIRITUS_DIAGNOSTICS_LAUNCH_ID", ""),
            agent=agent,
            model=model,
            message_id=message_id,
            call_id=call_id,
            failure_layer=FailureLayer(failure_layer) if failure_layer else None,
            data=self.policy.trace_data(data or {}),
        )
        line = json.dumps(record.to_store(), ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
            else:
                self._publish(record)
        return record

    @staticmethod
    def _publish(record: TraceRecord) -> None:
        """Best-effort live delivery for a ``spiritus dev`` child process.

        The journal remains the source of truth: delivery happens only after
        the append is fsynced, and an unavailable local listener never changes
        the application's result or trace persistence semantics.
        """
        endpoint = os.environ.get("SPIRITUS_DIAGNOSTICS_ENDPOINT", "")
        token = os.environ.get("SPIRITUS_DIAGNOSTICS_TOKEN", "")
        if not endpoint or not token:
            return
        host, separator, port_text = endpoint.rpartition(":")
        if not separator or not host:
            return
        try:
            payload = json.dumps({"token": token, "record": record.to_store()}).encode("utf-8")
            with socket.create_connection((host, int(port_text)), timeout=0.2) as connection:
                connection.sendall(payload)
        except (OSError, ValueError):
            # A developer terminal can close before an application exits.
            # Observability delivery must remain non-fatal in that case.
            return

    def entries(self, trace_filter: TraceFilter | None = None) -> list[TraceRecord]:
        trace_filter = trace_filter or TraceFilter()
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        records = [TraceRecord.from_store(json.loads(line)) for line in lines if line.strip()]
        records = [record for record in records if trace_filter.matches(record)]
        return records[-trace_filter.limit :] if trace_filter.limit is not None else records


class RunStore:
    """Atomic app-local records for querying completed or failed agent runs."""

    def __init__(self, root: Path, policy: DiagnosticPolicy | None = None):
        self.root = Path(root)
        self.path = self.root / "runs"
        self.artifacts_root = self.root / "artifacts"
        self.path.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.policy = policy or DiagnosticPolicy()

    def _path(self, run_id: str) -> Path:
        if not run_id.startswith("run_") or not run_id.replace("_", "").isalnum():
            raise ValueError("invalid run id")
        return self.path / f"{run_id}.json"

    @staticmethod
    def _time() -> str:
        return datetime.now(UTC).isoformat()

    def create(
        self, *, run_id: str, operation: str, session_id: str, agent: str, model: str
    ) -> RunRecord:
        operation = operation.strip()
        if not operation:
            raise ValueError("operation cannot be empty")
        record = RunRecord(
            run_id=run_id,
            operation=operation,
            status=RunStatus.RUNNING,
            session_id=session_id,
            agent=agent,
            model=model,
            started_at=self._time(),
        )
        self.save(record)
        return record

    def save(self, record: RunRecord) -> None:
        target = self._path(record.run_id)
        body = json.dumps(record.to_store(), ensure_ascii=False, indent=2) + "\n"
        with self._lock:
            temporary = target.with_suffix(".tmp")
            try:
                temporary.write_text(body, encoding="utf-8", newline="\n")
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink(missing_ok=True)

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            try:
                payload = json.loads(self._path(run_id).read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise KeyError(f"unknown run {run_id!r}") from exc
        return RunRecord.from_store(payload)

    def list(
        self, *, operation: str | None = None, status: RunStatus | str | None = None
    ) -> list[RunRecord]:
        selected_status = RunStatus(status) if status is not None else None
        with self._lock:
            paths = sorted(self.path.glob("run_*.json"))
        records = [RunRecord.from_store(json.loads(item.read_text(encoding="utf-8"))) for item in paths]
        return [
            record for record in records
            if (operation is None or record.operation == operation)
            and (selected_status is None or record.status is selected_status)
        ]

    def checkpoint(
        self, run_id: str, name: str, *, status: StageStatus | str = StageStatus.COMPLETED,
        detail: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        record = self.get(run_id)
        stage = RunStage(name, StageStatus(status), self._time(), dict(detail or {}))
        updated = replace(record, stages=(*record.stages, stage))
        self.save(updated)
        return updated

    def complete(self, run_id: str, *, artifacts: Mapping[str, Any] | None = None) -> RunRecord:
        record = self.get(run_id)
        updated = replace(
            record,
            status=RunStatus.COMPLETED,
            completed_at=self._time(),
            artifacts=self.policy.artifacts({**record.artifacts, **dict(artifacts or {})}),
        )
        self.save(updated)
        self._save_artifacts(run_id, updated.artifacts)
        return updated

    def _save_artifacts(self, run_id: str, artifacts: Mapping[str, Any]) -> None:
        if not artifacts:
            return
        target_dir = self.artifacts_root / run_id
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, value in artifacts.items():
            safe_name = str(name).replace("\\", "_").replace("/", "_")
            if not safe_name or safe_name in {".", ".."}:
                continue
            target = target_dir / f"{safe_name}.json"
            temporary = target.with_suffix(".tmp")
            try:
                temporary.write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

    def cancel(self, run_id: str) -> RunRecord:
        record = self.get(run_id)
        updated = replace(record, status=RunStatus.CANCELLED, completed_at=self._time())
        self.save(updated)
        return updated

    def fail(self, run_id: str, failure: RunFailure, *, stage: str | None = None) -> RunRecord:
        record = self.get(run_id)
        stages = record.stages
        if stage:
            stages = (*stages, RunStage(stage, StageStatus.FAILED, self._time()))
        updated = replace(
            record, status=RunStatus.FAILED, completed_at=self._time(), failure=failure, stages=stages
        )
        self.save(updated)
        return updated

    def events(self, run_id: str) -> list[TraceRecord]:
        self.get(run_id)
        return TraceStore(self.root).entries(TraceFilter(run_id=run_id))

    def artifact(self, run_id: str, name: str) -> Any:
        try:
            return self.get(run_id).artifacts[name]
        except KeyError as exc:
            raise KeyError(f"run {run_id!r} has no artifact {name!r}") from exc


class Diagnostics:
    """One app-scoped diagnostics service shared by runtime consumers.

    ``TraceStore`` and ``RunStore`` remain available as focused primitives, but
    applications should inject this facade into sessions and bridges so both
    paths use one policy, journal, run index, and artifact directory.
    """

    def __init__(
        self,
        root: Path,
        policy: DiagnosticPolicy | None = None,
        *,
        traces: TraceStore | None = None,
        runs: RunStore | None = None,
    ):
        self.root = Path(root)
        self.policy = policy or DiagnosticPolicy()
        self.traces = traces or TraceStore(self.root, self.policy)
        self.runs = runs or RunStore(self.root, self.policy)

    def get(self, run_id: str) -> RunRecord:
        return self.runs.get(run_id)

    def list(self, **filters) -> list[RunRecord]:
        return self.runs.list(**filters)

    def events(self, run_id: str) -> list[TraceRecord]:
        return self.runs.events(run_id)

    def artifact(self, run_id: str, name: str) -> Any:
        return self.runs.artifact(run_id, name)

    def checkpoint(self, run_id: str, name: str, **kwargs) -> RunRecord:
        return self.runs.checkpoint(run_id, name, **kwargs)

class TraceRenderer:
    """Render concise terminal timelines without turning traces into log spam."""

    _COLORS = {
        "run": "36",
        "model": "35",
        "tool": "33",
        "approval": "34",
        "file": "32",
        "failure": "31",
        "cancelled": "33",
    }

    def __init__(self, *, color: bool = True):
        self.color = color

    def render(self, record: TraceRecord) -> str:
        timestamp = record.time.replace("+00:00", "Z")
        label = record.kind.value.upper()
        detail = self._detail(record)
        line = f"{timestamp}  {label:<20}  {detail}".rstrip()
        return self._color(line, self._category(record))

    def render_many(self, records: Iterable[TraceRecord]) -> str:
        return "\n".join(self.render(record) for record in records)

    @staticmethod
    def _detail(record: TraceRecord) -> str:
        parts = [f"session={record.session_id}", f"run={record.run_id}"]
        if record.agent:
            parts.append(f"agent={record.agent}")
        if record.model:
            parts.append(f"model={record.model}")
        if record.call_id:
            parts.append(f"call={record.call_id}")
        if record.failure_layer:
            parts.append(f"layer={record.failure_layer.value}")
        for key, value in record.data.items():
            if key in {"kind", "owner"}:
                parts.append(f"{key}={value}")
            elif key == "error":
                parts.append(f"error={json.dumps(str(value), ensure_ascii=False)}")
            else:
                parts.append(f"{key}={value!r}")
        return "  ".join(parts)

    @staticmethod
    def _category(record: TraceRecord) -> str:
        if record.kind in {
            TraceKind.RUN_FAILED,
            TraceKind.TOOL_FAILED,
            TraceKind.RUNTIME_PROCESS_CRASHED,
        }:
            return "failure"
        if record.kind is TraceKind.RUN_CANCELLED:
            return "cancelled"
        return record.kind.value.split(".", 1)[0]

    def _color(self, line: str, category: str) -> str:
        if not self.color:
            return line
        color = self._COLORS.get(category)
        return f"\x1b[{color}m{line}\x1b[0m" if color else line


__all__ = [
    "FailureLayer",
    "DiagnosticPolicy",
    "Diagnostics",
    "FailureKind",
    "RunFailure",
    "RunRecord",
    "RunStatus",
    "RunStage",
    "RunStore",
    "StageStatus",
    "TraceFilter",
    "TraceKind",
    "TraceRecord",
    "TraceRenderer",
    "TraceStore",
]
