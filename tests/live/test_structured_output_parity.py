"""Model-backed schema result, validation, and restart persistence gate."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest

from spiritus import Agent, App, OutputSchema, engine

pytestmark = pytest.mark.live_opencode


@dataclass(frozen=True)
class ProbeOutput:
    status: str
    count: int


@pytest.fixture(autouse=True)
def require_live_tests():
    if os.environ.get("SPIRITUS_RUN_LIVE") != "1":
        pytest.skip(
            "set SPIRITUS_RUN_LIVE=1 to run model-backed OpenCode parity tests"
        )
    if engine.resolve() is None:
        pytest.fail("OpenCode is not installed; run `uv run spiritus install-engine`")


def test_structured_result_is_typed_and_persists_across_restart(tmp_path):
    model = os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")
    app = App(
        id="structured-output-probe",
        title="Structured Output Probe",
        root=tmp_path,
        agents=(
            Agent(
                name="parity-probe",
                description="Returns deterministic schema-backed test data",
                model=model,
                prompt=(
                    "Always use the required structured output mechanism. "
                    "Return only values requested by the user and do not use other tools."
                ),
            ),
        ),
    )
    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["STRUCTURED_OK"]},
            "count": {"type": "integer", "enum": [7]},
        },
        "required": ["status", "count"],
        "additionalProperties": False,
    }
    output = OutputSchema(
        schema,
        decoder=lambda value: ProbeOutput(value["status"], value["count"]),
    )

    async def first_process() -> str:
        async with app.runtime() as runtime:
            session = await runtime.require_sessions().create(agent="parity-probe")
            result = await session.run(
                "Return status STRUCTURED_OK and count 7.",
                output=output,
            )
            assert result.value == ProbeOutput("STRUCTURED_OK", 7)
            history = await session.history()
            assert history[-1].structured == {"status": "STRUCTURED_OK", "count": 7}
            return session.id

    session_id = asyncio.run(first_process())
    assert (tmp_path / ".spiritus" / "sessions" / f"{session_id}.json").is_file()

    async def second_process() -> None:
        async with app.runtime() as runtime:
            session = await runtime.require_sessions().resume(
                session_id, agent="parity-probe"
            )
            history = await session.history()
            assert [message.role for message in history] == ["user", "assistant"]
            assert history[-1].structured == {"status": "STRUCTURED_OK", "count": 7}

    asyncio.run(second_process())
