"""Raw-engine oracle and Spiritus single-agent parity acceptance tests."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections import defaultdict
from pathlib import Path

import pytest
import requests

from spiritus import (
    Agent,
    App,
    RunCompleted,
    RunIdle,
    RunStarted,
    TextDelta,
    TextSnapshot,
    engine,
)
from spiritus.runtime.server import OpenCodeServer
from tests.live.single_agent_contract import (
    AgentIdentity,
    ObservedMessage,
    StreamObservation,
    exercise_single_agent_contract,
)

pytestmark = pytest.mark.live_opencode


def _model() -> str:
    return os.environ.get("SPIRITUS_TEST_MODEL", "opencode/mimo-v2.5-free")


@pytest.fixture(autouse=True)
def require_live_tests():
    if os.environ.get("SPIRITUS_RUN_LIVE") != "1":
        pytest.skip(
            "set SPIRITUS_RUN_LIVE=1 to run model-backed OpenCode parity tests"
        )
    if engine.resolve() is None:
        pytest.fail("OpenCode is not installed; run `uv run spiritus install-engine`")


@pytest.fixture
def raw_application(tmp_path):
    model = _model()
    (tmp_path / "opencode.json").write_text(
        json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "model": model,
            "agent": {
                "parity-probe": {
                    "description": "Raw OpenCode compatibility oracle",
                    "mode": "primary",
                    "model": model,
                    "prompt": (
                        "Remember facts stated by the user in this session. "
                        "Do not use tools. Follow response-format instructions exactly."
                    ),
                    "tools": {"*": False},
                }
            },
        }),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def spiritus_application(tmp_path):
    return App(
        id="spiritus-parity-probe",
        title="Spiritus Parity Probe",
        root=tmp_path,
        agents=(
            Agent(
                name="parity-probe",
                description="Spiritus public API compatibility probe",
                mode="primary",
                model=_model(),
                prompt=(
                    "Remember facts stated by the user in this session. "
                    "Do not use tools. Follow response-format instructions exactly."
                ),
                tools=(),
            ),
        ),
    )


def _message_text(message: dict) -> str:
    return "".join(
        part.get("text", "")
        for part in message.get("parts", [])
        if part.get("type") == "text"
    )


class RawOpenCodeHarness:
    def __init__(self, root: Path):
        self.root = root
        self.server = OpenCodeServer(root)
        self.base_url = ""
        self.provider_id, self.model_id = _model().split("/", 1)

    def start(self) -> None:
        self.base_url = f"http://127.0.0.1:{self.server.start()}"

    def stop(self) -> None:
        self.server.stop()

    def preflight(self) -> AgentIdentity:
        providers = requests.get(self.base_url + "/provider", timeout=10)
        providers.raise_for_status()
        payload = providers.json()
        provider = next(
            (item for item in payload.get("all", []) if item.get("id") == self.provider_id),
            None,
        )
        assert provider is not None, f"provider {self.provider_id!r} is unavailable"
        models = provider.get("models", {})
        model_ids = set(models) if isinstance(models, dict) else {
            model["id"] for model in models
        }
        assert self.model_id in model_ids, (
            f"model {_model()!r} is unavailable; set SPIRITUS_TEST_MODEL to a live model"
        )
        assert self.provider_id in payload.get("connected", []), (
            f"provider {self.provider_id!r} is not connected"
        )

        agents = requests.get(self.base_url + "/agent", timeout=10)
        agents.raise_for_status()
        agent = next(item for item in agents.json() if item["name"] == "parity-probe")
        assert agent["model"] == {
            "providerID": self.provider_id,
            "modelID": self.model_id,
        }
        return AgentIdentity("parity-probe", self.provider_id, self.model_id)

    def create_session(self) -> str:
        response = requests.post(self.base_url + "/session", json={}, timeout=10)
        response.raise_for_status()
        return response.json()["id"]

    def stream_turn(self, session_id: str, prompt: str) -> StreamObservation:
        connected = threading.Event()
        completed = threading.Event()
        errors: list[BaseException] = []
        public_event_types: set[str] = set()
        part_types: dict[str, str] = {}
        pending_deltas: dict[str, list[str]] = defaultdict(list)
        visible_deltas: list[str] = []
        reasoning_delta_count = 0

        def consume() -> None:
            nonlocal reasoning_delta_count
            try:
                with requests.get(
                    self.base_url + "/event",
                    stream=True,
                    timeout=(10, 120),
                ) as response:
                    response.raise_for_status()
                    connected.set()
                    for line in response.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data:"):
                            continue
                        envelope = json.loads(line[5:].strip())
                        event = envelope.get("payload", envelope)
                        properties = event.get("properties", {})
                        event_session = properties.get("sessionID")
                        if event_session is None:
                            event_session = properties.get("info", {}).get("sessionID")
                        if event_session not in (None, session_id):
                            continue

                        event_type = event.get("type")
                        if event_type == "session.status":
                            if properties.get("status", {}).get("type") == "busy":
                                public_event_types.add("running")
                        elif event_type == "message.part.updated":
                            part = properties.get("part", {})
                            part_id = part.get("id")
                            part_type = part.get("type")
                            if part_id and part_type:
                                part_types[part_id] = part_type
                                buffered = pending_deltas.pop(part_id, [])
                                if part_type == "text":
                                    visible_deltas.extend(buffered)
                                    if buffered:
                                        public_event_types.add("text_delta")
                                elif part_type == "reasoning":
                                    reasoning_delta_count += len(buffered)
                        elif event_type == "message.part.delta":
                            part_id = properties.get("partID")
                            delta = properties.get("delta", "")
                            if properties.get("field") != "text" or not part_id or not delta:
                                continue
                            if part_types.get(part_id) == "text":
                                visible_deltas.append(delta)
                                public_event_types.add("text_delta")
                            elif part_types.get(part_id) == "reasoning":
                                reasoning_delta_count += 1
                            else:
                                pending_deltas[part_id].append(delta)
                        elif event_type == "message.updated":
                            info = properties.get("info", {})
                            if info.get("role") == "assistant" and info.get("time", {}).get(
                                "completed"
                            ):
                                public_event_types.add("completed")
                        elif event_type == "session.idle":
                            public_event_types.add("idle")
                            completed.set()
                            return
            except BaseException as exc:  # delivered to the test thread below
                errors.append(exc)
                connected.set()
                completed.set()

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        assert connected.wait(10), "SSE connection did not become ready"

        response = requests.post(
            self.base_url + f"/session/{session_id}/prompt_async",
            json={
                "agent": "parity-probe",
                "model": {"providerID": self.provider_id, "modelID": self.model_id},
                "parts": [{"type": "text", "text": prompt}],
            },
            timeout=10,
        )
        response.raise_for_status()
        assert response.status_code == 204
        assert completed.wait(120), "timed out waiting for session.idle"
        thread.join(timeout=5)
        if errors:
            raise errors[0]

        final = self.history(session_id)[-1].text
        return StreamObservation(
            final_text=final,
            visible_delta_text="".join(visible_deltas),
            event_types=frozenset(public_event_types),
            reasoning_delta_count=reasoning_delta_count,
        )

    def direct_turn(self, session_id: str, prompt: str) -> str:
        response = requests.post(
            self.base_url + f"/session/{session_id}/message",
            json={
                "agent": "parity-probe",
                "model": {"providerID": self.provider_id, "modelID": self.model_id},
                "parts": [{"type": "text", "text": prompt}],
            },
            timeout=120,
        )
        response.raise_for_status()
        return _message_text(response.json())

    def session_ids(self) -> set[str]:
        response = requests.get(self.base_url + "/session", timeout=10)
        response.raise_for_status()
        return {session["id"] for session in response.json()}

    def history(self, session_id: str) -> list[ObservedMessage]:
        response = requests.get(
            self.base_url + f"/session/{session_id}/message", timeout=10
        )
        response.raise_for_status()
        messages = []
        for message in response.json():
            info = message["info"]
            model = info.get("model") or {
                "providerID": info.get("providerID", ""),
                "modelID": info.get("modelID", ""),
            }
            model_name = ""
            if model.get("providerID") and model.get("modelID"):
                model_name = f"{model['providerID']}/{model['modelID']}"
            messages.append(ObservedMessage(
                role=info["role"],
                text=_message_text(message),
                agent=info.get("agent", ""),
                model=model_name,
            ))
        return messages


class SpiritusHarness:
    def __init__(self, app: App):
        self.app = app
        self.runtime = app.runtime()

    def start(self) -> None:
        asyncio.run(self.runtime.start())

    def stop(self) -> None:
        asyncio.run(self.runtime.stop())

    def preflight(self) -> AgentIdentity:
        selected = next(agent for agent in self.app.agents if agent.name == "parity-probe")
        return AgentIdentity(
            selected.name,
            selected.model.provider_id,
            selected.model.model_id,
        )

    def create_session(self) -> str:
        async def create() -> str:
            session = await self.runtime.require_sessions().create(agent="parity-probe")
            return session.id

        return asyncio.run(create())

    def stream_turn(self, session_id: str, prompt: str) -> StreamObservation:
        async def stream() -> StreamObservation:
            session = await self.runtime.require_sessions().resume(
                session_id, agent="parity-probe"
            )
            run = await session.send(prompt)
            part_text: dict[str, str] = {}
            event_types: set[str] = set()
            async for event in run.events():
                if isinstance(event, RunStarted):
                    event_types.add("running")
                elif isinstance(event, TextDelta):
                    part_text[event.part_id] = part_text.get(event.part_id, "") + event.text
                    event_types.add("text_delta")
                elif isinstance(event, TextSnapshot):
                    part_text[event.part_id] = event.text
                    event_types.add("text_delta")
                elif isinstance(event, RunCompleted):
                    event_types.add("completed")
                elif isinstance(event, RunIdle):
                    event_types.add("idle")
            result = await run.result()
            return StreamObservation(
                final_text=result.text,
                visible_delta_text="".join(part_text.values()),
                event_types=frozenset(event_types),
                reasoning_delta_count=0,
            )

        return asyncio.run(stream())

    def direct_turn(self, session_id: str, prompt: str) -> str:
        async def direct() -> str:
            session = await self.runtime.require_sessions().resume(
                session_id, agent="parity-probe"
            )
            return (await session.run(prompt)).text

        return asyncio.run(direct())

    def session_ids(self) -> set[str]:
        async def get_ids() -> set[str]:
            return {item.id for item in await self.runtime.require_sessions().list()}

        return asyncio.run(get_ids())

    def history(self, session_id: str) -> list[ObservedMessage]:
        async def get_history() -> list[ObservedMessage]:
            session = await self.runtime.require_sessions().resume(
                session_id, agent="parity-probe"
            )
            return [
                ObservedMessage(
                    role=message.role,
                    text=message.text,
                    agent=message.agent,
                    model=str(message.model) if message.model else "",
                )
                for message in await session.history()
            ]

        return asyncio.run(get_history())


def test_raw_opencode_single_agent_oracle(raw_application):
    marker = f"MARKER-{uuid.uuid4().hex[:10].upper()}"
    codeword = f"CODE-{uuid.uuid4().hex[:10].upper()}"

    observation = exercise_single_agent_contract(
        lambda: RawOpenCodeHarness(raw_application),
        marker=marker,
        codeword=codeword,
    )

    assert observation.first_turn.reasoning_delta_count > 0
    assert marker in observation.first_turn.final_text


def test_spiritus_single_agent_matches_the_oracle(spiritus_application):
    marker = f"MARKER-{uuid.uuid4().hex[:10].upper()}"
    codeword = f"CODE-{uuid.uuid4().hex[:10].upper()}"

    observation = exercise_single_agent_contract(
        lambda: SpiritusHarness(spiritus_application),
        marker=marker,
        codeword=codeword,
    )

    assert observation.first_turn.reasoning_delta_count == 0
    assert marker in observation.first_turn.final_text
