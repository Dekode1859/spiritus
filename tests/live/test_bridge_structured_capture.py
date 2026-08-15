"""Live contract for structured results delivered through the desktop bridge."""
from __future__ import annotations

import os

import pytest

from spiritus import Agent, App, engine
from spiritus.bridge import Bridge
from spiritus.runtime.server import OpenCodeServer
from spiritus.tracing import RunStatus

pytestmark = pytest.mark.live_opencode


@pytest.fixture(autouse=True)
def require_live_tests():
    if os.environ.get("SPIRITUS_RUN_LIVE") != "1":
        pytest.skip("set SPIRITUS_RUN_LIVE=1 to run model-backed OpenCode parity tests")
    if engine.resolve() is None:
        pytest.fail("OpenCode is not installed; run `uv run spiritus install-engine`")


class _HistoryForbiddenClient:
    """Proves async completion does not depend on the broken history endpoint."""

    def __init__(self, client):
        self._client = client

    def messages(self, session_id: str):
        raise AssertionError("structured bridge completion must not read session history")

    def __getattr__(self, name):
        return getattr(self._client, name)


def test_bridge_captures_structured_completion_without_history(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    app = App(
        id="bridge-structured-capture",
        title="Bridge Structured Capture",
        root=tmp_path,
        agents=(
            Agent(
                name="schema-probe",
                description="Returns one schema-bound diagnostic result",
                model=model,
                prompt="Use the required structured-output mechanism. Do not use tools.",
                tools=(),
            ),
        ),
    )
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "const": "BRIDGE_OK"}},
        "required": ["status"],
        "additionalProperties": False,
    }
    app.compile()
    server = OpenCodeServer(app.project_root)
    bridge = Bridge(app.to_config(), server)
    server.start()
    try:
        original_client = bridge._opencode()
        bridge._opencode = lambda: _HistoryForbiddenClient(original_client)
        session_id = bridge.create_session()["id"]
        started = bridge.agent_run(
            session_id,
            "schema-probe",
            None,
            "Return status BRIDGE_OK.",
            operation="diagnostics.structured_capture",
            output_schema=schema,
        )

        events = list(bridge.session_events(session_id))
        record = bridge._runs.get(started["run_id"])

        assert any(event["type"] == "run.completed" for event in events)
        assert events[-1]["type"] == "run.idle"
        assert record.status is RunStatus.COMPLETED
        assert next(stage for stage in record.stages if stage.name == "output.parsed").detail == {
            "source": "completion_stream"
        }
        assert bridge._runs.artifact(started["run_id"], "agent.output")
        history = bridge.session_history(session_id)
        assert history[-1]["info"]["structured"] == {"status": "BRIDGE_OK"}
    finally:
        server.stop()
