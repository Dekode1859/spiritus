"""Stable Spiritus events normalized from OpenCode's raw SSE stream."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..permissions import ApprovalDecision


@dataclass(frozen=True, slots=True)
class RunEvent:
    session_id: str


@dataclass(frozen=True, slots=True)
class RunStarted(RunEvent):
    pass


@dataclass(frozen=True, slots=True)
class TextDelta(RunEvent):
    message_id: str
    part_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TextSnapshot(RunEvent):
    """Authoritative replacement when a raw snapshot diverges from deltas."""

    message_id: str
    part_id: str
    text: str


@dataclass(frozen=True, slots=True)
class RunCompleted(RunEvent):
    message_id: str
    structured: Any = None


@dataclass(frozen=True, slots=True)
class RunIdle(RunEvent):
    pass


@dataclass(frozen=True, slots=True)
class RunFailed(RunEvent):
    message: str
    data: Any = None


@dataclass(frozen=True, slots=True)
class ApprovalRequested(RunEvent):
    request_id: str
    permission: str
    patterns: tuple[str, ...]
    metadata: dict[str, Any]
    always: tuple[str, ...] = ()
    message_id: str = ""
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalResolved(RunEvent):
    request_id: str
    decision: ApprovalDecision


@dataclass(frozen=True, slots=True)
class ToolStarted(RunEvent):
    message_id: str
    part_id: str
    call_id: str
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolProgress(RunEvent):
    message_id: str
    part_id: str
    call_id: str
    tool: str
    title: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCompleted(RunEvent):
    message_id: str
    part_id: str
    call_id: str
    tool: str
    output: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolFailed(RunEvent):
    message_id: str
    part_id: str
    call_id: str
    tool: str
    error: str
    metadata: dict[str, Any]


class EventNormalizer:
    """Reduce OpenCode event ordering into stable, visible run events.

    OpenCode sends text-shaped deltas for both reasoning and visible output.
    It can also interleave deltas with authoritative part snapshots. This
    reducer correlates message roles and part types, hides reasoning, and emits
    a replacement event if a snapshot cannot be represented as a suffix.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._roles: dict[str, str] = {}
        self._part_types: dict[str, str] = {}
        self._part_text: dict[str, str] = {}
        self._deferred: dict[str, list[dict]] = defaultdict(list)
        self._tool_status: dict[str, str] = {}
        self._started = False

    @staticmethod
    def _payload(envelope: dict) -> dict:
        return envelope.get("payload", envelope)

    @staticmethod
    def _event_session(payload: dict) -> str | None:
        properties = payload.get("properties", {})
        return (
            properties.get("sessionID")
            or properties.get("part", {}).get("sessionID")
            or properties.get("info", {}).get("sessionID")
        )

    def feed(self, envelope: dict) -> list[RunEvent]:
        payload = self._payload(envelope)
        event_type = payload.get("type")
        properties = payload.get("properties", {})
        event_session = self._event_session(payload)
        if event_session is not None and event_session != self.session_id:
            return []

        if event_type == "message.updated":
            info = properties.get("info", {})
            message_id = info.get("id", "")
            role = info.get("role", "")
            if message_id and role:
                self._roles[message_id] = role
            events: list[RunEvent] = []
            if message_id and role:
                deferred = self._deferred.pop(message_id, [])
                for event in deferred:
                    events.extend(self._feed_message_part(event))
            if role == "assistant" and info.get("time", {}).get("completed"):
                # The completion update is the authoritative result carried by
                # the async transport. Keeping it here avoids a follow-up
                # history request, which is not reliable for structured
                # OpenCode results on the pinned engine.
                events.append(
                    RunCompleted(self.session_id, message_id, info.get("structured"))
                )
            return events

        if event_type in {"message.part.updated", "message.part.delta"}:
            message_id = (
                properties.get("messageID")
                or properties.get("part", {}).get("messageID")
            )
            if not message_id:
                return []
            if message_id not in self._roles:
                self._deferred[message_id].append(payload)
                return []
            return self._feed_message_part(payload)

        if event_type == "permission.asked":
            tool = properties.get("tool") or {}
            request_id = properties.get("id", "")
            if not request_id:
                return []
            return [
                ApprovalRequested(
                    self.session_id,
                    request_id,
                    properties.get("permission", ""),
                    tuple(properties.get("patterns", [])),
                    dict(properties.get("metadata") or {}),
                    tuple(properties.get("always", [])),
                    tool.get("messageID", ""),
                    tool.get("callID", ""),
                )
            ]

        if event_type == "permission.replied":
            request_id = properties.get("requestID", "")
            reply = properties.get("reply", "")
            if not request_id or not reply:
                return []
            return [
                ApprovalResolved(
                    self.session_id,
                    request_id,
                    ApprovalDecision.parse(reply),
                )
            ]

        if event_type == "session.status":
            if properties.get("status", {}).get("type") == "busy" and not self._started:
                self._started = True
                return [RunStarted(self.session_id)]
            return []

        if event_type == "session.error":
            error = properties.get("error") or {}
            message = error.get("data", {}).get("message") or error.get("message")
            return [RunFailed(self.session_id, message or "OpenCode run failed", error)]

        if event_type == "session.idle":
            return [RunIdle(self.session_id)]

        return []

    def _feed_message_part(self, payload: dict) -> list[RunEvent]:
        event_type = payload.get("type")
        properties = payload.get("properties", {})
        part = properties.get("part", {})
        message_id = properties.get("messageID") or part.get("messageID", "")
        if self._roles.get(message_id) != "assistant":
            return []

        if event_type == "message.part.updated":
            part_id = part.get("id", "")
            part_type = part.get("type", "")
            if not part_id or not part_type:
                return []
            self._part_types[part_id] = part_type
            if part_type == "tool":
                return self._feed_tool_part(message_id, part)
            if part_type != "text":
                return []
            snapshot = part.get("text", "")
            previous = self._part_text.get(part_id, "")
            self._part_text[part_id] = snapshot
            if not snapshot or snapshot == previous:
                return []
            if snapshot.startswith(previous):
                return [
                    TextDelta(
                        self.session_id,
                        message_id,
                        part_id,
                        snapshot[len(previous):],
                    )
                ]
            return [TextSnapshot(self.session_id, message_id, part_id, snapshot)]

        part_id = properties.get("partID", "")
        delta = properties.get("delta", "")
        if (
            properties.get("field") != "text"
            or not part_id
            or not delta
            or self._part_types.get(part_id) != "text"
        ):
            return []
        self._part_text[part_id] = self._part_text.get(part_id, "") + delta
        return [TextDelta(self.session_id, message_id, part_id, delta)]

    def _feed_tool_part(self, message_id: str, part: dict) -> list[RunEvent]:
        part_id = part.get("id", "")
        call_id = part.get("callID", "")
        tool = part.get("tool", "")
        state = part.get("state") or {}
        status = state.get("status", "")
        if not part_id or not call_id or not tool or not status:
            return []
        previous = self._tool_status.get(part_id)
        if previous == status and status != "running":
            return []
        self._tool_status[part_id] = status
        events: list[RunEvent] = []
        if previous is None:
            events.append(
                ToolStarted(
                    self.session_id,
                    message_id,
                    part_id,
                    call_id,
                    tool,
                    dict(state.get("input") or {}),
                )
            )
        if status == "running":
            events.append(
                ToolProgress(
                    self.session_id,
                    message_id,
                    part_id,
                    call_id,
                    tool,
                    state.get("title", ""),
                    dict(state.get("metadata") or {}),
                )
            )
        elif status == "completed":
            events.append(
                ToolCompleted(
                    self.session_id,
                    message_id,
                    part_id,
                    call_id,
                    tool,
                    str(state.get("output", "")),
                    dict(state.get("metadata") or {}),
                )
            )
        elif status == "error":
            events.append(
                ToolFailed(
                    self.session_id,
                    message_id,
                    part_id,
                    call_id,
                    tool,
                    str(state.get("error", "Tool failed")),
                    dict(state.get("metadata") or {}),
                )
            )
        return events


__all__ = [
    "ApprovalRequested",
    "ApprovalResolved",
    "EventNormalizer",
    "RunCompleted",
    "RunEvent",
    "RunFailed",
    "RunIdle",
    "RunStarted",
    "TextDelta",
    "TextSnapshot",
    "ToolCompleted",
    "ToolFailed",
    "ToolProgress",
    "ToolStarted",
]
