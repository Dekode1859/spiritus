"""Session, streaming run, direct result, and history API tests."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from spiritus import (
    Agent,
    ApprovalDecision,
    ApprovalRequested,
    Command,
    OutputSchema,
    OutputValidationError,
    RunCancelledError,
    StructuredOutputError,
)
from spiritus.events import RunCompleted, RunIdle, RunStarted, TextDelta
from spiritus.persistence import SessionStore
from spiritus.runtime.client import OpenCodeError
from spiritus.sessions import SessionInfo, SessionManager


def agent() -> Agent:
    return Agent(
        name="assistant",
        description="Test assistant",
        prompt="Reply clearly.",
        model="opencode/test-model",
    )


def raw_message(role: str, text: str, message_id: str) -> dict:
    if role == "user":
        info = {
            "id": message_id,
            "sessionID": "ses_1",
            "role": "user",
            "agent": "assistant",
            "model": {"providerID": "opencode", "modelID": "test-model"},
        }
    else:
        info = {
            "id": message_id,
            "sessionID": "ses_1",
            "role": "assistant",
            "agent": "assistant",
            "providerID": "opencode",
            "modelID": "test-model",
        }
    return {
        "info": info,
        "parts": [{"id": f"prt_{message_id}", "type": "text", "text": text}],
    }


class FakeClient:
    def __init__(self):
        self.prompted = threading.Event()
        self.permission_replied = threading.Event()
        self.deleted: list[str] = []
        self.last_async_body = None
        self.last_direct_body = None
        self.history_error = False
        self.with_approval = False
        self.last_permission_reply = None
        self.cancel_mode = False
        self.abort_called = threading.Event()
        self.abort_release: threading.Event | None = None
        self.abort_result = True
        self.direct_payload = raw_message("assistant", "direct result", "msg_direct")
        self._messages = [
            raw_message("user", "hello", "msg_user"),
            raw_message("assistant", "streamed result", "msg_assistant"),
        ]

    def create_session(self, body):
        return {"id": "ses_1", "title": body.get("title", "")}

    def session(self, session_id):
        assert session_id == "ses_1"
        return {"id": session_id, "title": "Existing"}

    def sessions(self):
        return [{"id": "ses_1", "title": "Existing"}]

    def delete_session(self, session_id):
        self.deleted.append(session_id)

    def children(self, session_id):
        assert session_id == "ses_1"
        return [{"id": "ses_child", "title": "Worker", "parentID": "ses_1"}]

    def abort(self, session_id):
        assert session_id == "ses_1"
        self.abort_called.set()
        if self.abort_release is not None:
            assert self.abort_release.wait(5)
        return self.abort_result

    def messages(self, session_id):
        assert session_id == "ses_1"
        if self.history_error:
            raise OpenCodeError("pinned history defect", status=400)
        return self._messages

    def prompt_async(self, session_id, body):
        assert session_id == "ses_1"
        self.last_async_body = body
        self.prompted.set()

    def prompt(self, session_id, body):
        assert session_id == "ses_1"
        self.last_direct_body = body
        return self.direct_payload

    def command(self, session_id, body):
        assert session_id == "ses_1"
        self.last_command_body = body
        return self.direct_payload

    def reply_permission(self, request_id, reply, *, message=None):
        self.last_permission_reply = (request_id, reply, message)
        self.permission_replied.set()

    def events(self, *, ready, stop):
        ready.set()
        assert self.prompted.wait(5)
        yield {
            "payload": {
                "type": "session.status",
                "properties": {"sessionID": "ses_1", "status": {"type": "busy"}},
            }
        }
        if self.cancel_mode:
            assert self.abort_called.wait(5)
            yield {
                "payload": {
                    "type": "session.idle",
                    "properties": {"sessionID": "ses_1"},
                }
            }
            return
        if self.with_approval:
            yield {
                "payload": {
                    "type": "permission.asked",
                    "properties": {
                        "id": "per_1",
                        "sessionID": "ses_1",
                        "permission": "external_directory",
                        "patterns": [r"C:\workspace\inbox\*"],
                        "metadata": {"filepath": r"C:\workspace\inbox\note.txt"},
                        "always": [r"C:\workspace\inbox\*"],
                        "tool": {"messageID": "msg_assistant", "callID": "call_1"},
                    },
                }
            }
            assert self.permission_replied.wait(5)
            yield {
                "payload": {
                    "type": "permission.replied",
                    "properties": {
                        "sessionID": "ses_1",
                        "requestID": "per_1",
                        "reply": "once",
                    },
                }
            }
        yield {
            "payload": {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": "msg_assistant",
                        "sessionID": "ses_1",
                        "role": "assistant",
                        "time": {},
                    }
                },
            }
        }
        yield {
            "payload": {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "prt_assistant",
                        "sessionID": "ses_1",
                        "messageID": "msg_assistant",
                        "type": "text",
                        "text": "",
                    }
                },
            }
        }
        yield {
            "payload": {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": "ses_1",
                    "messageID": "msg_assistant",
                    "partID": "prt_assistant",
                    "field": "text",
                    "delta": "streamed result",
                },
            }
        }
        yield {
            "payload": {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": "msg_assistant",
                        "sessionID": "ses_1",
                        "role": "assistant",
                        "time": {"completed": 1},
                    }
                },
            }
        }
        yield {"payload": {"type": "session.idle", "properties": {"sessionID": "ses_1"}}}


def test_stream_and_final_result_share_one_run(tmp_path):
    async def scenario():
        client = FakeClient()
        manager = SessionManager(
            client,
            {"assistant": agent()},
            "assistant",
            SessionStore(tmp_path / ".spiritus"),
        )
        session = await manager.create(title="Test")
        handle = await session.send("hello")
        events = [event async for event in handle.events()]
        result = await handle.result()

        assert events == [
            RunStarted("ses_1"),
            TextDelta("ses_1", "msg_assistant", "prt_assistant", "streamed result"),
            RunCompleted("ses_1", "msg_assistant"),
            RunIdle("ses_1"),
        ]
        assert result.text == "streamed result"
        assert client.last_async_body == {
            "agent": "assistant",
            "model": {"providerID": "opencode", "modelID": "test-model"},
            "parts": [{"type": "text", "text": "hello"}],
        }

    asyncio.run(scenario())


def test_direct_result_history_resume_list_and_delete(tmp_path):
    async def scenario():
        client = FakeClient()
        manager = SessionManager(
            client,
            {"assistant": agent()},
            "assistant",
            SessionStore(tmp_path / ".spiritus"),
        )
        listed = await manager.list()
        assert listed == [SessionInfo("ses_1", "Existing")]

        session = await manager.resume("ses_1")
        result = await session.run("direct please")
        assert result.text == "direct result"
        assert client.last_direct_body["parts"][0]["text"] == "direct please"

        history = await session.history()
        assert [message.text for message in history] == ["hello", "streamed result"]
        assert str(history[-1].model) == "opencode/test-model"

        await session.delete()
        assert client.deleted == ["ses_1"]

    asyncio.run(scenario())


def test_streamed_approval_can_be_resolved_and_is_audited(tmp_path):
    async def scenario():
        client = FakeClient()
        client.with_approval = True
        store = SessionStore(tmp_path / ".spiritus")
        manager = SessionManager(client, {"assistant": agent()}, "assistant", store)
        session = await manager.resume("ses_1")
        handle = await session.send("read the named file")

        observed = []
        async for run_event in handle.events():
            observed.append(run_event)
            if isinstance(run_event, ApprovalRequested):
                await handle.respond(run_event, ApprovalDecision.ONCE)

        await handle.result()
        request = next(item for item in observed if isinstance(item, ApprovalRequested))
        assert request.metadata["filepath"].endswith("note.txt")
        assert client.last_permission_reply == ("per_1", "once", None)

        audit_path = tmp_path / ".spiritus" / "approvals.jsonl"
        records = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [item["kind"] for item in records] == [
            "approval.requested",
            "approval.resolved",
        ]
        assert records[-1]["decision"] == "once"

    asyncio.run(scenario())


def test_children_abort_and_stream_cancellation_are_typed(tmp_path):
    async def scenario():
        client = FakeClient()
        manager = SessionManager(
            client,
            {"assistant": agent()},
            "assistant",
            SessionStore(tmp_path / ".spiritus"),
        )
        session = await manager.resume("ses_1")
        assert await session.children() == [SessionInfo("ses_child", "Worker", "ses_1")]
        assert await session.abort() is True

        client.abort_called.clear()
        client.cancel_mode = True
        run = await session.send("start work")
        assert await run.cancel() is True
        events = [event async for event in run.events()]
        assert isinstance(events[-1], RunIdle)
        with pytest.raises(RunCancelledError):
            await run.result()

    asyncio.run(scenario())


def test_idle_waits_for_abort_response_before_classifying_cancellation(tmp_path):
    async def scenario():
        client = FakeClient()
        client.cancel_mode = True
        client.abort_result = False
        client.abort_release = threading.Event()
        manager = SessionManager(
            client,
            {"assistant": agent()},
            "assistant",
            SessionStore(tmp_path / ".spiritus"),
        )
        session = await manager.resume("ses_1")
        run = await session.send("finish while cancellation races")

        cancellation = asyncio.create_task(run.cancel())
        assert await asyncio.to_thread(client.abort_called.wait, 2)
        await asyncio.sleep(0.05)
        client.abort_release.set()

        assert await cancellation is False
        events = [event async for event in run.events()]
        assert isinstance(events[-1], RunIdle)
        assert (await run.result()).text == "streamed result"

    asyncio.run(scenario())


def test_declared_command_runs_directly_and_is_persisted(tmp_path):
    async def scenario():
        client = FakeClient()
        command = Command(
            "validate",
            "Return a validation marker",
            "Reply with $ARGUMENTS",
            agent="assistant",
        )
        manager = SessionManager(
            client,
            {"assistant": agent()},
            "assistant",
            SessionStore(tmp_path / ".spiritus"),
            commands={"validate": command},
        )
        session = await manager.resume("ses_1")
        result = await session.run_command("validate", "COMMAND_OK")

        assert result.text == "direct result"
        assert client.last_command_body == {
            "command": "validate",
            "arguments": "COMMAND_OK",
        }
        stored = SessionStore(tmp_path / ".spiritus").load("ses_1")
        assert stored[-2]["command"] == "validate"
        assert stored[-2]["command_arguments"] == "COMMAND_OK"
        with pytest.raises(ValueError, match="unknown command"):
            await session.run_command("missing")

    asyncio.run(scenario())


def test_unknown_agent_and_blank_prompt_fail_before_transport(tmp_path):
    async def scenario():
        client = FakeClient()
        manager = SessionManager(
            client,
            {"assistant": agent()},
            "assistant",
            SessionStore(tmp_path / ".spiritus"),
        )
        try:
            await manager.create(agent="missing")
        except ValueError as exc:
            assert "unknown agent" in str(exc)
        else:
            raise AssertionError("unknown agent was accepted")

        session = await manager.resume("ses_1")
        try:
            await session.run("   ")
        except ValueError as exc:
            assert "prompt cannot be empty" in str(exc)
        else:
            raise AssertionError("blank prompt was accepted")

    asyncio.run(scenario())


def structured_message(value=None, error=None) -> dict:
    payload = raw_message("assistant", "", "msg_structured")
    payload["info"]["parentID"] = "msg_structured_user"
    if value is not None:
        payload["info"]["structured"] = value
    if error is not None:
        payload["info"]["error"] = error
    return payload


def test_structured_output_is_validated_decoded_and_survives_history_failure(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["OK"]},
            "count": {"type": "integer"},
        },
        "required": ["status", "count"],
        "additionalProperties": False,
    }

    async def scenario():
        store_path = tmp_path / ".spiritus"
        client = FakeClient()
        client.direct_payload = structured_message({"status": "OK", "count": 7})
        manager = SessionManager(
            client, {"assistant": agent()}, "assistant", SessionStore(store_path)
        )
        session = await manager.resume("ses_1")
        output = OutputSchema(schema, decoder=lambda value: (value["status"], value["count"]))
        result = await session.run("return data", output=output, retry_count=3)

        assert result.value == ("OK", 7)
        assert client.last_direct_body["format"] == {
            "type": "json_schema",
            "schema": schema,
            "retryCount": 3,
        }

        # Recreate both store and manager to model a process restart. The engine
        # history endpoint fails exactly as pinned 1.18.13 does after a schema run.
        client.history_error = True
        restarted = SessionManager(
            client, {"assistant": agent()}, "assistant", SessionStore(store_path)
        )
        resumed = await restarted.resume("ses_1")
        history = await resumed.history()
        assert [message.role for message in history] == ["user", "assistant"]
        assert history[-1].structured == {"status": "OK", "count": 7}

    asyncio.run(scenario())


def test_structured_engine_failure_is_typed_and_persisted(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / ".spiritus")
        client = FakeClient()
        client.direct_payload = structured_message(
            error={
                "name": "StructuredOutputError",
                "data": {"message": "invalid output", "retries": 2},
            }
        )
        manager = SessionManager(client, {"assistant": agent()}, "assistant", store)
        session = await manager.resume("ses_1")

        with pytest.raises(StructuredOutputError) as captured:
            await session.run("return data", output={"type": "object"})
        assert captured.value.retries == 2
        assert captured.value.result is not None

        client.history_error = True
        history = await session.history()
        assert history[-1].error["name"] == "StructuredOutputError"

    asyncio.run(scenario())


def test_spiritus_revalidates_engine_structured_values(tmp_path):
    async def scenario():
        client = FakeClient()
        client.direct_payload = structured_message({"count": "not-an-integer"})
        manager = SessionManager(
            client,
            {"assistant": agent()},
            "assistant",
            SessionStore(tmp_path / ".spiritus"),
        )
        session = await manager.resume("ses_1")
        with pytest.raises(OutputValidationError):
            await session.run(
                "return data",
                output={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            )

    asyncio.run(scenario())
