"""Normalized app-local transcript persistence."""
from __future__ import annotations

import json

import pytest

from spiritus.persistence import ApprovalAuditLog, SessionStore


def test_transcript_round_trip_and_upsert_are_atomic(tmp_path):
    store = SessionStore(tmp_path / ".spiritus")
    store.append("ses_one", [{"id": "msg_1", "text": "draft"}])
    store.append("ses_one", [
        {"id": "msg_1", "text": "final"},
        {"id": "msg_2", "text": "next"},
    ])

    assert store.load("ses_one") == [
        {"id": "msg_1", "text": "final"},
        {"id": "msg_2", "text": "next"},
    ]
    assert not list(store.root.glob(".session-*.tmp"))
    assert json.loads((store.root / "ses_one.json").read_text(encoding="utf-8"))[0][
        "text"
    ] == "final"


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "", "white space"])
def test_session_id_cannot_escape_the_store(session_id, tmp_path):
    store = SessionStore(tmp_path / ".spiritus")
    with pytest.raises(ValueError):
        store.load(session_id)


def test_approval_audit_is_append_only_and_filterable(tmp_path):
    log = ApprovalAuditLog(tmp_path / ".spiritus")
    log.append("approval.requested", session_id="ses_1", request_id="per_1")
    log.append(
        "approval.resolved",
        session_id="ses_2",
        request_id="per_2",
        decision="reject",
    )

    assert [item["kind"] for item in log.entries()] == [
        "approval.requested",
        "approval.resolved",
    ]
    assert [item["request_id"] for item in log.entries(session_id="ses_1")] == [
        "per_1"
    ]
