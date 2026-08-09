"""Packaged skill, command, and MCP configuration contracts."""
from __future__ import annotations

import sys

import pytest

from spiritus import Access, Agent, App, Command, MCPServer, Skill


def agent(**overrides) -> Agent:
    values = {
        "name": "assistant",
        "description": "Extension test agent",
        "prompt": "Use only declared extensions.",
        "model": "opencode/test-model",
    }
    values.update(overrides)
    return Agent(**values)


def test_skill_compiles_valid_frontmatter_and_agent_policy(tmp_path):
    skill = Skill(
        "marker-guide",
        "Return the packaged marker",
        "## Procedure\n\nReturn `SKILL_OK` exactly.",
        metadata={"audience": "tests"},
    )
    app = App(
        "skill-probe",
        "Skill Probe",
        tmp_path,
        (agent(skills=("marker-guide",)),),
        skills=(skill,),
    )
    compiled = app.opencode_config()["agent"]["assistant"]
    assert compiled["tools"]["skill"] is True
    assert compiled["permission"]["skill"] == {
        "*": "deny",
        "marker-guide": "allow",
    }

    app.compile()
    text = (tmp_path / ".opencode/skills/marker-guide/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert text.startswith("---\nname: marker-guide\n")
    assert 'description: "Return the packaged marker"' in text
    assert '  "audience": "tests"' in text
    assert "SKILL_OK" in text


def test_command_and_local_mcp_compile_to_pinned_shapes(tmp_path):
    command = Command(
        "emit-marker",
        "Emit one marker",
        "Return COMMAND:$ARGUMENTS",
        agent="assistant",
        model="opencode/test-model",
    )
    server = MCPServer(
        "fixture",
        (sys.executable, "server.py"),
        environment={"PREFIX": "MCP"},
        timeout_ms=15_000,
        access=Access.ASK,
    )
    app = App(
        "extension-probe",
        "Extension Probe",
        tmp_path,
        (agent(mcp_servers=("fixture",)),),
        commands=(command,),
        mcp_servers=(server,),
    )
    config = app.opencode_config()

    assert config["command"]["emit-marker"] == {
        "description": "Emit one marker",
        "template": "Return COMMAND:$ARGUMENTS",
        "subtask": False,
        "agent": "assistant",
        "model": "opencode/test-model",
    }
    assert config["mcp"]["fixture"] == {
        "type": "local",
        "command": [sys.executable, "server.py"],
        "enabled": True,
        "timeout": 15_000,
        "environment": {"PREFIX": "MCP"},
    }
    compiled = config["agent"]["assistant"]
    assert compiled["tools"]["fixture_*"] is True
    assert compiled["permission"]["fixture_*"] == "ask"


def test_extensions_are_unique_and_agent_references_are_closed(tmp_path):
    skill = Skill("guide", "Guide", "Use the guide.")
    server = MCPServer("fixture", (sys.executable, "server.py"))
    with pytest.raises(ValueError, match="unknown skills"):
        App("probe", "Probe", tmp_path, (agent(skills=("missing",)),))
    with pytest.raises(ValueError, match="unknown MCP"):
        App("probe", "Probe", tmp_path, (agent(mcp_servers=("missing",)),))
    with pytest.raises(ValueError, match="unique"):
        App(
            "probe",
            "Probe",
            tmp_path,
            (agent(),),
            skills=(skill, skill),
        )
    with pytest.raises(ValueError, match="unknown agent"):
        App(
            "probe",
            "Probe",
            tmp_path,
            (agent(),),
            commands=(Command("run", "Run", "Run", agent="missing"),),
            mcp_servers=(server,),
        )
