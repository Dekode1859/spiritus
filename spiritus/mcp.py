"""Managed local MCP server definitions for the pinned OpenCode adapter."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .permissions import Access

_MCP_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class MCPServer:
    name: str
    command: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    timeout_ms: int = 10_000
    enabled: bool = True
    access: Access | str = Access.ALLOW

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not _MCP_NAME.fullmatch(name):
            raise ValueError("MCP server name must use lowercase letters, digits, '-' or '_'")
        command = tuple(str(item).strip() for item in self.command)
        if not command or any(not item for item in command):
            raise ValueError("local MCP command must contain an executable and arguments")
        if self.timeout_ms <= 0:
            raise ValueError("MCP timeout_ms must be positive")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "command", command)
        object.__setattr__(
            self,
            "environment",
            {str(key): str(value) for key, value in self.environment.items()},
        )
        object.__setattr__(self, "access", Access.parse(self.access))

    @property
    def tool_pattern(self) -> str:
        return f"{self.name}_*"

    def to_opencode(self) -> dict:
        payload = {
            "type": "local",
            "command": list(self.command),
            "enabled": self.enabled,
            "timeout": self.timeout_ms,
        }
        if self.environment:
            payload["environment"] = dict(self.environment)
        return payload


__all__ = ["MCPServer"]
