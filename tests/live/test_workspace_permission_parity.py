"""Model-backed named workspace allow, deny, and approval acceptance gate."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from spiritus import (
    Access,
    Agent,
    App,
    ApprovalDecision,
    ApprovalRequested,
    ApprovalResolved,
    Workspace,
    WorkspaceAccess,
    WorkspaceFolder,
    engine,
)

pytestmark = pytest.mark.live_opencode


@pytest.fixture(autouse=True)
def require_live_tests():
    if os.environ.get("SPIRITUS_RUN_LIVE") != "1":
        pytest.skip(
            "set SPIRITUS_RUN_LIVE=1 to run model-backed OpenCode parity tests"
        )
    if engine.resolve() is None:
        pytest.fail("OpenCode is not installed; run `uv run spiritus install-engine`")


async def _workspace_scenario(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    app = App(
        id="workspace-permission-probe",
        title="Workspace Permission Probe",
        root=tmp_path,
        agents=(
            Agent(
                name="workspace-probe",
                description="Exercises named workspace permissions",
                model=model,
                prompt=(
                    "When given an exact file path, immediately use the read tool. "
                    "Spiritus handles permission prompts, so never ask for approval in "
                    "prose. Return the file contents exactly and do not guess."
                ),
                workspace_access=(
                    WorkspaceAccess("allowed", access=Access.ALLOW),
                    WorkspaceAccess("approval", access=Access.ASK),
                ),
            ),
        ),
        workspace=Workspace(
            (
                WorkspaceFolder("allowed"),
                WorkspaceFolder("approval"),
                WorkspaceFolder("denied"),
            )
        ),
    )
    app.compile()
    allowed_marker = f"ALLOW-{uuid.uuid4().hex[:12].upper()}"
    approval_marker = f"ASK-{uuid.uuid4().hex[:12].upper()}"
    denied_marker = f"DENY-{uuid.uuid4().hex[:12].upper()}"
    assert app.workspace_root is not None
    allowed_file = app.workspace_root / "allowed" / "marker.txt"
    approval_file = app.workspace_root / "approval" / "marker.txt"
    denied_file = app.workspace_root / "denied" / "marker.txt"
    allowed_file.write_text(allowed_marker, encoding="utf-8")
    approval_file.write_text(approval_marker, encoding="utf-8")
    denied_file.write_text(denied_marker, encoding="utf-8")

    async with app.runtime() as runtime:
        sessions = runtime.require_sessions()

        allowed = await sessions.create(agent="workspace-probe")
        allowed_result = await allowed.run(
            f"Read exactly {allowed_file} and return its exact contents only."
        )
        assert allowed_marker in allowed_result.text

        denied = await sessions.create(agent="workspace-probe")
        denied_result = await denied.run(
            f"Read exactly {denied_file}. Do not invent its contents."
        )
        denied_history = await denied.history()
        denied_transcript = "\n".join(message.text for message in denied_history)
        denied_tool_parts = [
            part
            for message in denied_history
            for part in message.parts
            if part.get("type") == "tool" and part.get("tool") == "read"
        ]
        assert denied_marker not in denied_result.text
        assert denied_marker not in denied_transcript
        assert not any(
            part.get("state", {}).get("status") == "completed"
            for part in denied_tool_parts
        )

        approval = await sessions.create(agent="workspace-probe")
        run = await approval.send(
            f"Read exactly {approval_file} and return its exact contents only."
        )
        events = []
        async for event in run.events():
            events.append(event)
            if isinstance(event, ApprovalRequested):
                assert event.permission == "external_directory"
                assert event.metadata.get("filepath") == str(approval_file)
                await run.respond(event, ApprovalDecision.ONCE)
        approval_result = await run.result()
        assert approval_marker in approval_result.text
        assert sum(isinstance(event, ApprovalRequested) for event in events) == 1
        assert any(isinstance(event, ApprovalResolved) for event in events)

        assert runtime.audit is not None
        audit = runtime.audit.entries(session_id=approval.id)
        assert [item["kind"] for item in audit] == [
            "approval.requested",
            "approval.resolved",
        ]
        assert audit[-1]["decision"] == "once"

    compiled = app.opencode_config()["agent"]["workspace-probe"]
    assert compiled["permission"]["external_directory"]["*"] == "deny"
    assert str((app.workspace_root / "denied").resolve()) not in compiled[
        "permission"
    ]["external_directory"]


def test_named_workspace_allow_deny_and_approval_are_enforced(tmp_path):
    asyncio.run(_workspace_scenario(tmp_path))
