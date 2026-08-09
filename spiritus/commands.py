"""Reusable application commands compiled to the pinned OpenCode contract."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Model

_COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    template: str
    agent: str = ""
    model: Model | str | None = None
    subtask: bool = False

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not _COMMAND_NAME.fullmatch(name):
            raise ValueError("command name must use lowercase letters, digits, '-' or '_'")
        description = self.description.strip()
        template = self.template.strip()
        if not description:
            raise ValueError("command description cannot be empty")
        if not template:
            raise ValueError("command template cannot be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "agent", self.agent.strip())
        if self.model is not None:
            object.__setattr__(self, "model", Model.parse(self.model))

    def to_opencode(self) -> dict:
        payload = {
            "description": self.description,
            "template": self.template,
            "subtask": self.subtask,
        }
        if self.agent:
            payload["agent"] = self.agent
        if self.model is not None:
            payload["model"] = str(self.model)
        return payload


__all__ = ["Command"]
