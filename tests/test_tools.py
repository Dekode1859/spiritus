"""Typed Python tool definition, compilation, and execution tests."""
from __future__ import annotations

import http.client
import json
from urllib.parse import urlsplit

import pytest
import requests

from spiritus import Access, Agent, App, Tool, ToolContext
from spiritus.tools import ToolServer


def schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "left": {"type": "integer", "description": "First number"},
            "right": {"type": "integer", "description": "Second number"},
            "style": {"type": "string", "enum": ["short", "long"]},
        },
        "required": ["left", "right"],
        "additionalProperties": False,
    }


def make_tool(**overrides) -> Tool:
    values = {
        "name": "adder",
        "description": "Add two integers",
        "input_schema": schema(),
        "handler": lambda args: {"total": args["left"] + args["right"]},
        "output_schema": {
            "type": "object",
            "properties": {"total": {"type": "integer"}},
            "required": ["total"],
            "additionalProperties": False,
        },
    }
    values.update(overrides)
    return Tool(**values)


def make_agent(**overrides) -> Agent:
    values = {
        "name": "assistant",
        "description": "Tool test agent",
        "prompt": "Use the declared tool.",
        "model": "opencode/test-model",
        "tools": ("adder",),
    }
    values.update(overrides)
    return Agent(**values)


def test_tool_validates_input_output_and_context():
    observed = {}

    def handler(arguments, context):
        observed["context"] = context
        return {"total": arguments["left"] + arguments["right"]}

    tool = make_tool(handler=handler)
    context = ToolContext(session_id="ses_1", agent="assistant")
    assert tool.invoke({"left": 2, "right": 5}, context) == {"total": 7}
    assert observed["context"] == context
    with pytest.raises(Exception, match="integer"):
        tool.invoke({"left": "2", "right": 5}, context)


@pytest.mark.parametrize("name", ["", "Upper", "has spaces", "-leading"])
def test_tool_rejects_unstable_names(name):
    with pytest.raises(ValueError):
        make_tool(name=name)


def test_tool_compiles_a_typed_shim_without_embedding_runtime_secrets(tmp_path):
    tool = make_tool()
    path = tool.compile(tmp_path)
    source = path.read_text(encoding="utf-8")

    assert path == tmp_path / ".opencode" / "tools" / "spiritus_adder.ts"
    assert '"left": tool.schema.number().int().describe("First number")' in source
    assert '"style": tool.schema.enum(["short", "long"]).optional()' in source
    assert "SPIRITUS_TOOL_TOKEN" in source
    assert "http://127.0.0.1:" not in source


def test_tool_server_is_loopback_authenticated_and_revalidates_inputs():
    tool = make_tool()
    server = ToolServer((tool,))
    environment = server.start()
    try:
        url = environment["SPIRITUS_TOOL_URL"] + "/tools/spiritus_adder"
        unauthorized = requests.post(url, json={"args": {}}, timeout=5)
        assert unauthorized.status_code == 401

        headers = {"authorization": f"Bearer {environment['SPIRITUS_TOOL_TOKEN']}"}
        invalid = requests.post(
            url,
            headers=headers,
            json={"args": {"left": "2", "right": 4}},
            timeout=5,
        )
        assert invalid.status_code == 400

        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        connection.request(
            "POST",
            parsed.path,
            headers={
                **headers,
                "content-length": str(1024 * 1024 + 1),
            },
        )
        oversized = connection.getresponse()
        oversized.read()
        connection.close()
        assert oversized.status == 413

        valid = requests.post(
            url,
            headers=headers,
            json={
                "args": {"left": 2, "right": 4},
                "context": {"sessionID": "ses_1", "agent": "assistant"},
            },
            timeout=5,
        )
        assert valid.json() == {"result": {"total": 6}}
        assert server.calls[-1]["context"]["session_id"] == "ses_1"
    finally:
        server.stop()


def test_app_resolves_logical_tool_name_and_permission(tmp_path):
    tool = make_tool(access=Access.ASK)
    app = App(
        "tool-probe",
        "Tool Probe",
        tmp_path,
        (make_agent(),),
        tools=(tool,),
    )
    compiled = app.opencode_config()["agent"]["assistant"]

    assert compiled["tools"]["spiritus_adder"] is True
    assert "adder" not in compiled["tools"]
    assert compiled["permission"]["spiritus_adder"] == "ask"
    app.compile()
    assert json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
