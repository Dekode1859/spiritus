"""Declarative agents and compatibility access to raw OpenCode config."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Model
from ..permissions import Access
from ..workspace import WorkspaceAccess

_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_AGENT_MODES = frozenset({"primary", "subagent", "all"})
_BUILTIN_TOOLS = (
    "invalid",
    "question",
    "bash",
    "read",
    "glob",
    "grep",
    "edit",
    "write",
    "task",
    "webfetch",
    "todowrite",
    "websearch",
    "skill",
    "apply_patch",
)
_BUILTIN_PERMISSIONS = (
    "read",
    "edit",
    "glob",
    "grep",
    "list",
    "bash",
    "task",
    "external_directory",
    "todowrite",
    "question",
    "webfetch",
    "websearch",
    "lsp",
    "doom_loop",
    "skill",
)


@dataclass(frozen=True, slots=True)
class Agent:
    """One application-defined OpenCode agent.

    The public contract starts secure: pinned built-ins are explicitly disabled
    and denied unless a later capability (such as a named workspace) enables
    them. A wildcard tool denial is intentionally avoided because OpenCode
    1.18.13 suppresses native ``ask`` events when that legacy rule is present.
    """

    name: str
    description: str
    prompt: str
    model: Model | str
    label: str = ""
    mode: str = "primary"
    tools: tuple[str, ...] = field(default_factory=tuple)
    workspace_access: tuple[WorkspaceAccess, ...] = field(default_factory=tuple)
    delegates: tuple[str, ...] = field(default_factory=tuple)
    skills: tuple[str, ...] = field(default_factory=tuple)
    mcp_servers: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not _AGENT_NAME.fullmatch(name):
            raise ValueError(
                "agent name must start with a lowercase letter or digit and contain "
                "only lowercase letters, digits, '-' or '_'"
            )
        description = self.description.strip()
        if not description:
            raise ValueError("agent description cannot be empty")
        prompt = self.prompt.strip()
        if not prompt:
            raise ValueError("agent prompt cannot be empty")
        if self.mode not in _AGENT_MODES:
            raise ValueError(f"agent mode must be one of {sorted(_AGENT_MODES)}")
        tools = tuple(dict.fromkeys(tool.strip() for tool in self.tools if tool.strip()))
        workspace_access = tuple(self.workspace_access)
        if any(not isinstance(item, WorkspaceAccess) for item in workspace_access):
            raise TypeError("workspace_access must contain WorkspaceAccess values")
        delegates = tuple(
            dict.fromkeys(name.strip() for name in self.delegates if name.strip())
        )
        for delegate in delegates:
            if not _AGENT_NAME.fullmatch(delegate):
                raise ValueError(f"invalid delegated agent name {delegate!r}")
        skills = tuple(dict.fromkeys(name.strip() for name in self.skills if name.strip()))
        mcp_servers = tuple(
            dict.fromkeys(name.strip() for name in self.mcp_servers if name.strip())
        )

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "model", Model.parse(self.model))
        object.__setattr__(self, "label", self.label.strip() or _titleize(name))
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "workspace_access", workspace_access)
        object.__setattr__(self, "delegates", delegates)
        object.__setattr__(self, "skills", skills)
        object.__setattr__(self, "mcp_servers", mcp_servers)

    def to_opencode(
        self,
        *,
        resolved_tools: tuple[str, ...] | None = None,
        tool_overrides: Mapping[str, bool] | None = None,
        permission_overrides: Mapping[str, Any] | None = None,
    ) -> dict:
        # OpenCode implements schema-directed results through its internal
        # StructuredOutput tool. It is part of the result transport, not an
        # application capability, so keep it available while denying every
        # undeclared user/tool integration.
        selected_tools = self.tools if resolved_tools is None else resolved_tools
        tool_policy = dict.fromkeys(_BUILTIN_TOOLS, False)
        tool_policy["StructuredOutput"] = True
        tool_policy.update(dict.fromkeys(selected_tools, True))
        tool_policy.update(tool_overrides or {})
        permission: dict[str, Any] = dict.fromkeys(
            _BUILTIN_PERMISSIONS,
            Access.DENY.value,
        )
        permission.update(dict.fromkeys(selected_tools, Access.ALLOW.value))
        permission.update(permission_overrides or {})
        return {
            "description": self.description,
            "mode": self.mode,
            "prompt": self.prompt,
            "model": str(self.model),
            "tools": tool_policy,
            "permission": permission,
        }


def _titleize(name: str) -> str:
    return " ".join(w.capitalize() for w in name.replace("-", " ").split())


def load_agents(project_root: Path) -> list[dict]:
    """Read agent definitions from the application's opencode.json.

    Returns ``[{name, label, description}]``. If the file is missing/invalid,
    returns an empty list.
    """
    oc_path = Path(project_root) / "opencode.json"
    try:
        cfg = json.loads(oc_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    agents = cfg.get("agent", {}) or {}
    out = []
    for name, spec in agents.items():
        label = (spec or {}).get("label") or _titleize(name)
        out.append({
            "name": name,
            "label": label,
            "description": (spec or {}).get("description", ""),
        })
    return out


def default_model(project_root: Path) -> str:
    """Return the application's configured default model string, or ''."""
    oc_path = Path(project_root) / "opencode.json"
    try:
        return json.loads(oc_path.read_text(encoding="utf-8")).get("model", "")
    except Exception:
        return ""
