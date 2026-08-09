"""Model-backed declared subagent, child session, and cancellation gate."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from spiritus import (
    Agent,
    App,
    RunCancelledError,
    ToolCompleted,
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


async def _delegation_scenario(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    marker = f"DELEGATE-{uuid.uuid4().hex[:12].upper()}"
    app = App(
        "delegation-probe",
        "Delegation Probe",
        tmp_path,
        (
            Agent(
                name="coordinator",
                description="Delegates validation tasks to one worker",
                model=model,
                prompt=(
                    "For every validation task, immediately invoke the task tool with "
                    "subagent_type worker. Return the worker's exact result. Never do "
                    "the task yourself."
                ),
                delegates=("worker",),
            ),
            Agent(
                name="worker",
                description="Returns exact validation markers to its coordinator",
                mode="subagent",
                model=model,
                prompt=(
                    "Return the exact validation marker requested by the coordinator. "
                    "Do not use tools and do not alter the marker."
                ),
            ),
        ),
    )
    compiled = app.opencode_config()["agent"]
    assert compiled["coordinator"]["permission"]["task"] == {
        "*": "deny",
        "worker": "allow",
    }
    assert compiled["worker"]["permission"]["task"] == "deny"

    async with app.runtime() as runtime:
        sessions = runtime.require_sessions()
        parent = await sessions.create(agent="coordinator")
        run = await parent.send(
            f"Delegate to worker: return this exact marker only: {marker}"
        )
        events = [event async for event in run.events()]
        result = await run.result()

        assert marker in result.text
        task_events = [
            event
            for event in events
            if isinstance(event, ToolCompleted) and event.tool == "task"
        ]
        assert len(task_events) == 1
        assert marker in task_events[0].output

        children = await parent.children()
        assert len(children) == 1
        assert children[0].parent_id == parent.id
        child = await sessions.resume(children[0].id, agent="worker")
        child_history = await child.history()
        assert any(marker in message.text for message in child_history)

        parent_history = await parent.history()
        assert any(
            part.get("type") == "tool"
            and part.get("tool") == "task"
            and part.get("state", {}).get("status") == "completed"
            for message in parent_history
            for part in message.parts
        )

        cancelled_session = await sessions.create(agent="coordinator")
        cancelled_run = await cancelled_session.send(
            "Start a delegated validation task, but produce a long detailed analysis."
        )
        assert await cancelled_run.cancel() is True

        async def drain():
            return [event async for event in cancelled_run.events()]

        await asyncio.wait_for(drain(), timeout=30)
        with pytest.raises(RunCancelledError):
            await cancelled_run.result()
        assert cancelled_session.id in {item.id for item in await sessions.list()}


def test_declared_subagent_is_traceable_and_parent_can_be_cancelled(tmp_path):
    asyncio.run(_delegation_scenario(tmp_path))
