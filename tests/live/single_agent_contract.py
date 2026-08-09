"""Reusable black-box contract for the minimum single-agent lifecycle."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    provider_id: str
    model_id: str

    @property
    def model(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


@dataclass(frozen=True)
class ObservedMessage:
    role: str
    text: str
    agent: str = ""
    model: str = ""


@dataclass(frozen=True)
class StreamObservation:
    final_text: str
    visible_delta_text: str
    event_types: frozenset[str]
    reasoning_delta_count: int = 0


@dataclass(frozen=True)
class ContractObservation:
    session_id: str
    first_turn: StreamObservation
    resumed_text: str
    history: tuple[ObservedMessage, ...]


class SingleAgentHarness(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def preflight(self) -> AgentIdentity: ...

    def create_session(self) -> str: ...

    def stream_turn(self, session_id: str, prompt: str) -> StreamObservation: ...

    def direct_turn(self, session_id: str, prompt: str) -> str: ...

    def session_ids(self) -> set[str]: ...

    def history(self, session_id: str) -> list[ObservedMessage]: ...


def exercise_single_agent_contract(
    harness_factory: Callable[[], SingleAgentHarness],
    *,
    marker: str,
    codeword: str,
) -> ContractObservation:
    """Exercise streaming, final output, persistence, resume, and memory."""
    first = harness_factory()
    first.start()
    try:
        identity = first.preflight()
        assert identity.name == "parity-probe"
        session_id = first.create_session()
        first_turn = first.stream_turn(
            session_id,
            f"Remember the codeword {codeword}. Include the marker {marker} in your reply.",
        )
        assert marker in first_turn.visible_delta_text
        assert marker in first_turn.final_text
        assert {"running", "text_delta", "completed", "idle"} <= first_turn.event_types

        initial_history = first.history(session_id)
        assert [message.role for message in initial_history] == ["user", "assistant"]
        assert initial_history[-1].text == first_turn.final_text
        assert initial_history[-1].agent == identity.name
        assert initial_history[-1].model == identity.model
    finally:
        first.stop()

    # A fresh harness is essential: reusing an in-memory client does not prove
    # durable storage or engine restart behavior.
    second = harness_factory()
    second.start()
    try:
        assert session_id in second.session_ids()
        resumed_text = second.direct_turn(
            session_id,
            "What codeword did I ask you to remember? Reply with only that codeword.",
        )
        assert codeword in resumed_text

        history = second.history(session_id)
        assert [message.role for message in history] == [
            "user", "assistant", "user", "assistant",
        ]
        assert history[-1].text == resumed_text
        assert history[-1].agent == identity.name
        assert history[-1].model == identity.model
    finally:
        second.stop()

    return ContractObservation(
        session_id=session_id,
        first_turn=first_turn,
        resumed_text=resumed_text,
        history=tuple(history),
    )
