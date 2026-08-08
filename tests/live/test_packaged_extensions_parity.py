"""Model-backed skill, command, and MCP extension acceptance gates."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

from spiritus import Agent, App, Command, MCPServer, Skill, ToolCompleted, engine

pytestmark = pytest.mark.live_opencode


@pytest.fixture(autouse=True)
def require_live_tests():
    if os.environ.get("SPIRITUS_RUN_LIVE") != "1":
        pytest.skip(
            "set SPIRITUS_RUN_LIVE=1 to run model-backed OpenCode parity tests"
        )
    if engine.resolve() is None:
        pytest.fail("OpenCode is not installed; run `uv run spiritus install-engine`")


def model() -> str:
    return os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")


async def _skill_scenario(tmp_path):
    marker = f"SKILL-{uuid.uuid4().hex[:12].upper()}"
    skill = Skill(
        "marker-guide",
        "Use this guide whenever the user asks for the packaged skill marker",
        (
            "## Required response\n\n"
            f"Return the exact marker `{marker}`. Do not alter or explain it."
        ),
    )
    app = App(
        "skill-live-probe",
        "Skill Live Probe",
        tmp_path,
        (
            Agent(
                name="skill-probe",
                description="Loads one packaged Spiritus skill",
                model=model(),
                prompt=(
                    "When asked for the packaged skill marker, immediately call the "
                    "skill tool with name marker-guide, then follow its instructions."
                ),
                skills=("marker-guide",),
            ),
        ),
        skills=(skill,),
    )

    async with app.runtime() as runtime:
        session = await runtime.require_sessions().create(agent="skill-probe")
        run = await session.send("Use the packaged guide and return its marker.")
        events = [event async for event in run.events()]
        result = await run.result()

        completed = [
            event
            for event in events
            if isinstance(event, ToolCompleted) and event.tool == "skill"
        ]
        assert len(completed) == 1
        assert marker in completed[0].output
        assert marker in result.text
        history = await session.history()
        assert any(
            part.get("type") == "tool"
            and part.get("tool") == "skill"
            and part.get("state", {}).get("status") == "completed"
            for message in history
            for part in message.parts
        )


def test_packaged_skill_is_discovered_loaded_and_applied(tmp_path):
    asyncio.run(_skill_scenario(tmp_path))


async def _command_scenario(tmp_path):
    marker = f"COMMAND-{uuid.uuid4().hex[:12].upper()}"
    command = Command(
        "emit-marker",
        "Emit the supplied command marker",
        "Reply with exactly COMMAND_OK:$ARGUMENTS and no other text.",
        agent="command-probe",
    )
    app = App(
        "command-live-probe",
        "Command Live Probe",
        tmp_path,
        (
            Agent(
                name="command-probe",
                description="Runs one packaged Spiritus command",
                model=model(),
                prompt="Follow command response formats exactly. Do not use tools.",
            ),
        ),
        commands=(command,),
    )

    async with app.runtime() as runtime:
        session = await runtime.require_sessions().create(agent="command-probe")
        result = await session.run_command("emit-marker", marker)
        assert f"COMMAND_OK:{marker}" in result.text
        history = await session.history()
        assert history[-1].text == result.text
        invocation = next(message for message in history if message.command == "emit-marker")
        assert invocation.command_arguments == marker
        assert runtime.client is not None
        assert "emit-marker" in {item["name"] for item in runtime.client.commands()}


def test_packaged_command_executes_through_the_public_session_api(tmp_path):
    asyncio.run(_command_scenario(tmp_path))


async def _mcp_scenario(tmp_path):
    value = f"VALUE-{uuid.uuid4().hex[:12].upper()}"
    prefix = f"MCP-{uuid.uuid4().hex[:8].upper()}"
    fixture = Path(__file__).parents[1] / "fixtures" / "mcp_echo_server.py"
    server = MCPServer(
        "fixture",
        (sys.executable, str(fixture)),
        environment={"SPIRITUS_MCP_PREFIX": prefix},
    )
    app = App(
        "mcp-live-probe",
        "MCP Live Probe",
        tmp_path,
        (
            Agent(
                name="mcp-probe",
                description="Calls one managed local MCP tool",
                model=model(),
                prompt=(
                    "When asked to echo an MCP value, immediately call fixture_echo "
                    "with the exact value. Return the tool output exactly."
                ),
                mcp_servers=("fixture",),
            ),
        ),
        mcp_servers=(server,),
    )

    async with app.runtime() as runtime:
        assert runtime.client is not None
        assert runtime.client.mcp_status()["fixture"]["status"] == "connected"
        session = await runtime.require_sessions().create(agent="mcp-probe")
        run = await session.send(
            f"Call fixture_echo with value {value}. Return its exact output."
        )
        events = [event async for event in run.events()]
        result = await run.result()
        expected = f"{prefix}:{value}"

        completed = [
            event
            for event in events
            if isinstance(event, ToolCompleted) and event.tool == "fixture_echo"
        ]
        assert len(completed) == 1
        assert expected in completed[0].output
        assert expected in result.text
        history = await session.history()
        assert any(
            part.get("type") == "tool"
            and part.get("tool") == "fixture_echo"
            and expected in part.get("state", {}).get("output", "")
            for message in history
            for part in message.parts
        )


def test_managed_local_mcp_tool_connects_calls_and_persists(tmp_path):
    asyncio.run(_mcp_scenario(tmp_path))
