"""OpenCode SSE to stable Spiritus event normalization."""
from __future__ import annotations

from spiritus.events import (
    ApprovalRequested,
    ApprovalResolved,
    EventNormalizer,
    RunCompleted,
    RunIdle,
    RunStarted,
    TextDelta,
    TextSnapshot,
    ToolCompleted,
    ToolFailed,
    ToolProgress,
    ToolStarted,
)
from spiritus.permissions import ApprovalDecision

SESSION = "ses_test"
MESSAGE = "msg_test"
PART = "prt_test"


def event(event_type: str, **properties) -> dict:
    return {"payload": {"type": event_type, "properties": properties}}


def assistant_role(*, completed: bool = False) -> dict:
    info = {"id": MESSAGE, "sessionID": SESSION, "role": "assistant", "time": {}}
    if completed:
        info["time"]["completed"] = 1
    return event("message.updated", info=info)


def part_updated(part_type: str, text: str = "", *, message_id: str = MESSAGE) -> dict:
    return event(
        "message.part.updated",
        part={
            "id": PART,
            "sessionID": SESSION,
            "messageID": message_id,
            "type": part_type,
            "text": text,
        },
    )


def delta(text: str, *, message_id: str = MESSAGE) -> dict:
    return event(
        "message.part.delta",
        sessionID=SESSION,
        messageID=message_id,
        partID=PART,
        field="text",
        delta=text,
    )


def test_emits_lifecycle_and_visible_text_without_reasoning():
    reducer = EventNormalizer(SESSION)
    assert reducer.feed(event("session.status", sessionID=SESSION, status={"type": "busy"})) == [
        RunStarted(SESSION)
    ]
    reducer.feed(assistant_role())
    assert reducer.feed(part_updated("reasoning", "private")) == []
    assert reducer.feed(delta(" hidden")) == []

    # A separate visible part uses its own identity.
    visible_part = event(
        "message.part.updated",
        part={
            "id": "prt_visible",
            "sessionID": SESSION,
            "messageID": MESSAGE,
            "type": "text",
            "text": "",
        },
    )
    reducer.feed(visible_part)
    visible_delta = event(
        "message.part.delta",
        sessionID=SESSION,
        messageID=MESSAGE,
        partID="prt_visible",
        field="text",
        delta="hello",
    )
    assert reducer.feed(visible_delta) == [
        TextDelta(SESSION, MESSAGE, "prt_visible", "hello")
    ]
    assert reducer.feed(assistant_role(completed=True)) == [RunCompleted(SESSION, MESSAGE)]
    assert reducer.feed(event("session.idle", sessionID=SESSION)) == [RunIdle(SESSION)]


def test_authoritative_snapshots_do_not_duplicate_seen_deltas():
    reducer = EventNormalizer(SESSION)
    reducer.feed(assistant_role())
    reducer.feed(part_updated("text"))
    assert reducer.feed(delta("hel")) == [TextDelta(SESSION, MESSAGE, PART, "hel")]
    assert reducer.feed(part_updated("text", "hello")) == [
        TextDelta(SESSION, MESSAGE, PART, "lo")
    ]
    assert reducer.feed(part_updated("text", "hello")) == []


def test_divergent_snapshot_is_an_explicit_replacement():
    reducer = EventNormalizer(SESSION)
    reducer.feed(assistant_role())
    reducer.feed(part_updated("text"))
    reducer.feed(delta("draft"))
    assert reducer.feed(part_updated("text", "final")) == [
        TextSnapshot(SESSION, MESSAGE, PART, "final")
    ]


def test_part_events_wait_for_the_message_role_and_user_text_is_hidden():
    reducer = EventNormalizer(SESSION)
    assert reducer.feed(part_updated("text", "assistant text")) == []
    assert reducer.feed(assistant_role()) == [
        TextDelta(SESSION, MESSAGE, PART, "assistant text")
    ]

    user_message = "msg_user"
    user_info = {
        "id": user_message,
        "sessionID": SESSION,
        "role": "user",
        "time": {},
    }
    reducer.feed(event("message.updated", info=user_info))
    assert reducer.feed(part_updated("text", "secret prompt", message_id=user_message)) == []


def test_other_sessions_are_ignored():
    reducer = EventNormalizer(SESSION)
    assert reducer.feed(
        event("session.status", sessionID="ses_other", status={"type": "busy"})
    ) == []


def test_permission_events_are_typed_and_session_scoped():
    reducer = EventNormalizer(SESSION)
    asked = event(
        "permission.asked",
        id="per_1",
        sessionID=SESSION,
        permission="external_directory",
        patterns=[r"C:\workspace\inbox\*"],
        metadata={"filepath": r"C:\workspace\inbox\note.txt"},
        always=[r"C:\workspace\inbox\*"],
        tool={"messageID": MESSAGE, "callID": "call_1"},
    )
    assert reducer.feed(asked) == [
        ApprovalRequested(
            SESSION,
            "per_1",
            "external_directory",
            (r"C:\workspace\inbox\*",),
            {"filepath": r"C:\workspace\inbox\note.txt"},
            (r"C:\workspace\inbox\*",),
            MESSAGE,
            "call_1",
        )
    ]
    assert reducer.feed(
        event(
            "permission.replied",
            sessionID=SESSION,
            requestID="per_1",
            reply="once",
        )
    ) == [ApprovalResolved(SESSION, "per_1", ApprovalDecision.ONCE)]


def test_tool_parts_emit_stable_lifecycle_events_without_duplication():
    reducer = EventNormalizer(SESSION)
    reducer.feed(assistant_role())

    def tool_part(status, **state):
        return event(
            "message.part.updated",
            part={
                "id": "prt_tool",
                "sessionID": SESSION,
                "messageID": MESSAGE,
                "type": "tool",
                "callID": "call_1",
                "tool": "spiritus_adder",
                "state": {"status": status, "input": {"left": 2, "right": 4}, **state},
            },
        )

    assert reducer.feed(tool_part("pending", raw="")) == [
        ToolStarted(
            SESSION,
            MESSAGE,
            "prt_tool",
            "call_1",
            "spiritus_adder",
            {"left": 2, "right": 4},
        )
    ]
    assert reducer.feed(
        tool_part("running", title="Adding", metadata={"phase": "execute"})
    ) == [
        ToolProgress(
            SESSION,
            MESSAGE,
            "prt_tool",
            "call_1",
            "spiritus_adder",
            "Adding",
            {"phase": "execute"},
        )
    ]
    assert reducer.feed(
        tool_part("completed", output='{"total":6}', metadata={"ok": True})
    ) == [
        ToolCompleted(
            SESSION,
            MESSAGE,
            "prt_tool",
            "call_1",
            "spiritus_adder",
            '{"total":6}',
            {"ok": True},
        )
    ]
    assert reducer.feed(
        tool_part("completed", output='{"total":6}', metadata={"ok": True})
    ) == []

    failed = EventNormalizer(SESSION)
    failed.feed(assistant_role())
    events = failed.feed(tool_part("error", error="bad input", metadata={}))
    assert isinstance(events[0], ToolStarted)
    assert events[1] == ToolFailed(
        SESSION,
        MESSAGE,
        "prt_tool",
        "call_1",
        "spiritus_adder",
        "bad input",
        {},
    )
