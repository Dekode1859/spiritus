"""Model-backed typed Python tool execution and persistence gate."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from spiritus import (
    Agent,
    App,
    Tool,
    ToolCompleted,
    ToolProgress,
    ToolStarted,
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


async def _tool_scenario(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    marker = f"PYTHON-TOOL-{uuid.uuid4().hex[:12].upper()}"

    def lookup(arguments, context):
        assert context.session_id
        return {"key": arguments["key"], "marker": marker}

    tool = Tool(
        name="marker-lookup",
        description=(
            "Look up the required validation marker for a key. Always use this tool "
            "when the user asks for a validation marker."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The lookup key supplied by the user",
                }
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        handler=lookup,
        output_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "marker": {"type": "string"},
            },
            "required": ["key", "marker"],
            "additionalProperties": False,
        },
    )
    app = App(
        "typed-tool-probe",
        "Typed Tool Probe",
        tmp_path,
        (
            Agent(
                name="tool-probe",
                description="Calls one typed Spiritus tool",
                model=model,
                prompt=(
                    "When asked for a validation marker, immediately call the "
                    "spiritus_marker_lookup tool with the exact key. Include the "
                    "tool's marker in the final reply."
                ),
                tools=("marker-lookup",),
            ),
        ),
        tools=(tool,),
    )

    runtime = app.runtime()
    await runtime.start()
    try:
        session = await runtime.require_sessions().create(agent="tool-probe")
        session_id = session.id
        run = await session.send(
            "Use marker-lookup with key alpha. Return the marker from its result."
        )
        events = [event async for event in run.events()]
        result = await run.result()

        assert marker in result.text
        assert any(isinstance(event, ToolStarted) for event in events)
        assert any(isinstance(event, ToolProgress) for event in events)
        completed = [event for event in events if isinstance(event, ToolCompleted)]
        assert len(completed) == 1
        assert marker in completed[0].output
        assert runtime.tool_server is not None
        assert runtime.tool_server.calls[-1]["arguments"] == {"key": "alpha"}

        history = await session.history()
        tool_parts = [
            part
            for message in history
            for part in message.parts
            if part.get("type") == "tool"
            and part.get("tool") == "spiritus_marker_lookup"
        ]
        assert any(part.get("state", {}).get("status") == "completed" for part in tool_parts)
    finally:
        await runtime.stop()

    restarted = app.runtime()
    await restarted.start()
    try:
        resumed = await restarted.require_sessions().resume(session_id, agent="tool-probe")
        history = await resumed.history()
        persisted_parts = [
            part
            for message in history
            for part in message.parts
            if part.get("type") == "tool"
        ]
        assert any(marker in part.get("state", {}).get("output", "") for part in persisted_parts)
    finally:
        await restarted.stop()


def test_typed_python_tool_runs_and_persists(tmp_path):
    asyncio.run(_tool_scenario(tmp_path))
