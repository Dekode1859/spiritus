"""Pinned OpenCode engine contract tests.

These tests launch the real engine but never call a model. They are opt-in so
the normal test suite remains deterministic and works without an installed
OpenCode binary.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests

from spiritus import Agent, App, Command, MCPServer, Skill, Tool, engine
from spiritus.runtime.server import OpenCodeServer

pytestmark = pytest.mark.engine


@pytest.fixture(autouse=True)
def require_engine_tests():
    if os.environ.get("SPIRITUS_RUN_ENGINE") != "1":
        pytest.skip("set SPIRITUS_RUN_ENGINE=1 to run pinned-engine contract tests")
    if engine.resolve() is None:
        pytest.fail("OpenCode is not installed; run `uv run spiritus install-engine`")


@pytest.fixture
def configured_root(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    (tmp_path / "opencode.json").write_text(
        json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "model": model,
            "agent": {
                "parity-probe": {
                    "description": "Pinned engine contract probe",
                    "mode": "primary",
                    "model": model,
                    "prompt": "Do not use tools.",
                    "tools": {"*": False},
                }
            },
        }),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def running_server(configured_root):
    server = OpenCodeServer(configured_root)
    port = server.start()
    try:
        yield server, f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def test_engine_paths_are_inside_the_application_home(configured_root):
    server = OpenCodeServer(configured_root)
    completed = subprocess.run(
        [str(engine.resolve()), "debug", "paths"],
        cwd=configured_root,
        env=server._engine_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    home = server.home_dir.resolve()
    persistent = {"home", "data", "bin", "log", "repos", "cache", "config", "state"}
    resolved: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, _, value = line.partition(" ")
        if name in persistent and value.strip():
            resolved[name] = value.strip()

    assert persistent <= resolved.keys(), completed.stdout
    for name in persistent:
        candidate = os.path.commonpath([str(home), os.path.abspath(resolved[name])])
        assert candidate == str(home), f"{name} escaped app home: {resolved[name]}"


def test_pinned_openapi_contains_the_g1_and_g2_contract(running_server):
    _, base_url = running_server
    response = requests.get(base_url + "/doc", timeout=10)
    response.raise_for_status()
    spec = response.json()
    paths = spec["paths"]

    assert "/agent" in paths
    assert "/provider" in paths
    assert "/event" in paths
    assert "/session" in paths
    assert "/session/{sessionID}/message" in paths
    assert "/session/{sessionID}/prompt_async" in paths

    body_schema = paths["/session/{sessionID}/message"]["post"]["requestBody"]
    properties = body_schema["content"]["application/json"]["schema"]["properties"]
    assert {"agent", "model", "format", "parts"} <= properties.keys()
    assert "structured" in spec["components"]["schemas"]["AssistantMessage"]["properties"]


def test_configured_agent_is_discoverable_with_its_model(running_server):
    _, base_url = running_server
    response = requests.get(base_url + "/agent", timeout=10)
    response.raise_for_status()
    agent = next(item for item in response.json() if item["name"] == "parity-probe")

    provider_id, model_id = os.environ.get(
        "SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free"
    ).split("/", 1)
    assert agent["model"] == {"providerID": provider_id, "modelID": model_id}


def test_empty_session_survives_a_full_engine_restart(configured_root):
    first = OpenCodeServer(configured_root)
    try:
        base_url = f"http://127.0.0.1:{first.start()}"
        response = requests.post(base_url + "/session", json={}, timeout=10)
        response.raise_for_status()
        session_id = response.json()["id"]
    finally:
        first.stop()

    assert any(first.home_dir.rglob("*")), "engine wrote no app-local session data"

    second = OpenCodeServer(configured_root)
    try:
        base_url = f"http://127.0.0.1:{second.start()}"
        response = requests.get(base_url + "/session", timeout=10)
        response.raise_for_status()
        assert session_id in {session["id"] for session in response.json()}
    finally:
        second.stop()


def test_generated_python_tool_is_loaded_without_calling_a_model(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    tool = Tool(
        name="engine-probe",
        description="Return an engine contract marker",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=lambda arguments: arguments["value"],
    )
    app = App(
        "tool-engine-probe",
        "Tool Engine Probe",
        tmp_path,
        (
            Agent(
                name="probe",
                description="Pinned tool loader probe",
                prompt="Use only declared tools.",
                model=model,
                tools=("engine-probe",),
            ),
        ),
        tools=(tool,),
    )

    async def scenario():
        runtime = app.runtime()
        await runtime.start()
        try:
            assert runtime.client is not None
            assert tool.engine_name in runtime.client.tool_ids()
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_packaged_skill_command_and_mcp_are_discoverable_without_a_model(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    skill = Skill(
        "engine-marker",
        "Return the engine skill marker",
        "Return ENGINE_SKILL_OK exactly.",
    )
    command = Command(
        "engine-command",
        "Return the engine command marker",
        "Return ENGINE_COMMAND_OK:$ARGUMENTS",
        agent="probe",
    )
    mcp = MCPServer(
        "fixture",
        (sys.executable, str(fixture)),
        environment={"SPIRITUS_MCP_PREFIX": "ENGINE_MCP"},
    )
    app = App(
        "extension-engine-probe",
        "Extension Engine Probe",
        tmp_path,
        (
            Agent(
                name="probe",
                description="Pinned extension discovery probe",
                prompt="Use only declared extensions.",
                model=model,
                skills=("engine-marker",),
                mcp_servers=("fixture",),
            ),
        ),
        skills=(skill,),
        commands=(command,),
        mcp_servers=(mcp,),
    )

    async def scenario():
        runtime = app.runtime()
        await runtime.start()
        try:
            assert runtime.client is not None
            assert runtime.client.mcp_status()["fixture"]["status"] == "connected"
            assert "engine-command" in {
                item["name"] for item in runtime.client.commands()
            }
            loaded_agent = next(
                item for item in runtime.client.agents() if item["name"] == "probe"
            )
            assert any(
                rule.get("permission") == "skill"
                and rule.get("pattern") == "engine-marker"
                and rule.get("action") == "allow"
                for rule in loaded_agent["permission"]
            )
        finally:
            await runtime.stop()

    asyncio.run(scenario())
