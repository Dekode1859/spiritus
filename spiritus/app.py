"""High-level application definition and managed agent runtime."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents import Agent
from .commands import Command
from .config import AppConfig
from .mcp import MCPServer
from .persistence import ApprovalAuditLog, SessionStore
from .runtime import paths
from .runtime.client import OpenCodeClient
from .runtime.server import OpenCodeServer
from .sessions import RunManager, SessionManager
from .skills import Skill
from .tools import Tool, ToolServer
from .tracing import DiagnosticPolicy, Diagnostics
from .workspace import Workspace

_APP_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _deep_merge(base: dict, override: Mapping[str, Any]) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass(slots=True)
class App:
    """A declarative Spiritus application.

    ``raw_config`` is an explicit advanced escape hatch applied after generated
    values. It is never populated implicitly from a developer's global config.
    """

    id: str
    title: str
    root: Path
    agents: tuple[Agent, ...]
    default_agent: str = ""
    workspace: Workspace | None = None
    tools: tuple[Tool, ...] = field(default_factory=tuple)
    skills: tuple[Skill, ...] = field(default_factory=tuple)
    commands: tuple[Command, ...] = field(default_factory=tuple)
    mcp_servers: tuple[MCPServer, ...] = field(default_factory=tuple)
    diagnostic_policy: DiagnosticPolicy = field(default_factory=DiagnosticPolicy)
    raw_config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = self.id.strip()
        if not _APP_ID.fullmatch(self.id):
            raise ValueError(
                "app id must start with a lowercase letter or digit and contain only "
                "lowercase letters, digits, '.', '_' or '-'"
            )
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("app title cannot be empty")
        self.root = Path(self.root)
        self.agents = tuple(self.agents)
        if not self.agents:
            raise ValueError("an app must declare at least one agent")
        names = [agent.name for agent in self.agents]
        if len(names) != len(set(names)):
            raise ValueError("agent names must be unique within an app")
        by_name = {agent.name: agent for agent in self.agents}
        for agent in self.agents:
            for delegate in agent.delegates:
                if delegate == agent.name:
                    raise ValueError(f"agent {agent.name!r} cannot delegate to itself")
                target = by_name.get(delegate)
                if target is None:
                    raise ValueError(
                        f"agent {agent.name!r} delegates to unknown agent {delegate!r}"
                    )
                if target.mode not in {"subagent", "all"}:
                    raise ValueError(
                        f"delegated agent {delegate!r} must use mode 'subagent' or 'all'"
                    )
        self._validate_delegation_graph(by_name)
        self.default_agent = self.default_agent.strip() or names[0]
        if self.default_agent not in names:
            raise ValueError(f"default agent {self.default_agent!r} is not declared")
        if self.workspace is not None and not isinstance(self.workspace, Workspace):
            raise TypeError("workspace must be a Workspace value")
        if self.workspace is None:
            for agent in self.agents:
                if agent.workspace_access:
                    raise ValueError(
                        f"agent {agent.name!r} declares workspace access but the app has "
                        "no workspace"
                    )
        else:
            known = set(self.workspace.folder_names)
            for agent in self.agents:
                unknown = {
                    grant.folder for grant in agent.workspace_access
                    if grant.folder not in known
                }
                if unknown:
                    raise ValueError(
                        f"agent {agent.name!r} references unknown workspace folders "
                        f"{sorted(unknown)!r}"
                    )
        self.tools = tuple(self.tools)
        if any(not isinstance(tool, Tool) for tool in self.tools):
            raise TypeError("tools must contain Tool values")
        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool names must be unique within an app")
        self.skills = tuple(self.skills)
        self.commands = tuple(self.commands)
        self.mcp_servers = tuple(self.mcp_servers)
        self._validate_extensions(by_name)
        if not isinstance(self.raw_config, Mapping):
            raise TypeError("raw_config must be a mapping")
        if not isinstance(self.diagnostic_policy, DiagnosticPolicy):
            raise TypeError("diagnostic_policy must be a DiagnosticPolicy value")

    @property
    def project_root(self) -> Path:
        return paths.project_root(self.root, self.id)

    def apply_bundle_environment(self) -> None:
        """Apply identity and workspace overrides emitted by a bundle variant."""
        app_id = os.environ.get("SPIRITUS_APP_ID", "").strip()
        if app_id:
            if not _APP_ID.fullmatch(app_id):
                raise ValueError("SPIRITUS_APP_ID is not a valid application id")
            self.id = app_id
        title = os.environ.get("SPIRITUS_APP_TITLE", "").strip()
        if title:
            self.title = title
        workspace_dirname = os.environ.get("SPIRITUS_WORKSPACE_DIRNAME", "").strip()
        if workspace_dirname and self.workspace is not None:
            self.workspace = Workspace(self.workspace.folders, dirname=workspace_dirname)

    @property
    def engine_directory(self) -> Path:
        """Empty worktree used to keep app/config files outside agent scope."""
        return self.project_root / ".spiritus" / "worktree"

    @property
    def workspace_root(self) -> Path | None:
        return self.workspace.root(self.project_root) if self.workspace else None

    def _agent_config(self, agent: Agent) -> dict:
        tools: dict[str, bool] = {}
        permissions: dict[str, Any] = {}
        declared_tools = {tool.name: tool for tool in self.tools}
        resolved_tools: list[str] = []
        for name in agent.tools:
            definition = declared_tools.get(name)
            if definition is None:
                resolved_tools.append(name)
                continue
            resolved_tools.append(definition.engine_name)
            permissions[definition.engine_name] = definition.access.value
        if agent.delegates:
            tools["task"] = True
            permissions["task"] = {
                "*": "deny",
                **dict.fromkeys(agent.delegates, "allow"),
            }
        declared_skills = {skill.name: skill for skill in self.skills}
        if agent.skills:
            tools["skill"] = True
            permissions["skill"] = {
                "*": "deny",
                **{
                    name: declared_skills[name].access.value
                    for name in agent.skills
                },
            }
        selected_mcp = set(agent.mcp_servers)
        for server in self.mcp_servers:
            enabled = server.name in selected_mcp and server.enabled
            tools[server.tool_pattern] = enabled
            permissions[server.tool_pattern] = (
                server.access.value if enabled else "deny"
            )
        if self.workspace is not None:
            workspace_tools, workspace_permissions = self.workspace.compile_policy(
                self.project_root,
                agent.workspace_access,
            )
            tools.update(workspace_tools)
            permissions.update(workspace_permissions)
        return agent.to_opencode(
            resolved_tools=tuple(resolved_tools),
            tool_overrides=tools,
            permission_overrides=permissions,
        )

    @staticmethod
    def _validate_delegation_graph(agents: Mapping[str, Agent]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError("agent delegation graph cannot contain a cycle")
            visiting.add(name)
            for child in agents[name].delegates:
                visit(child)
            visiting.remove(name)
            visited.add(name)

        for name in agents:
            visit(name)

    def _validate_extensions(self, agents: Mapping[str, Agent]) -> None:
        groups = (
            (self.skills, Skill, "skill"),
            (self.commands, Command, "command"),
            (self.mcp_servers, MCPServer, "MCP server"),
        )
        for values, expected_type, label in groups:
            if any(not isinstance(item, expected_type) for item in values):
                raise TypeError(f"{label}s must contain {expected_type.__name__} values")
            names = [item.name for item in values]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} names must be unique within an app")

        skill_names = {skill.name for skill in self.skills}
        mcp_names = {server.name for server in self.mcp_servers}
        for agent in agents.values():
            missing_skills = set(agent.skills) - skill_names
            if missing_skills:
                raise ValueError(
                    f"agent {agent.name!r} references unknown skills "
                    f"{sorted(missing_skills)!r}"
                )
            missing_mcp = set(agent.mcp_servers) - mcp_names
            if missing_mcp:
                raise ValueError(
                    f"agent {agent.name!r} references unknown MCP servers "
                    f"{sorted(missing_mcp)!r}"
                )
        for command in self.commands:
            if command.agent and command.agent not in agents:
                raise ValueError(
                    f"command {command.name!r} references unknown agent "
                    f"{command.agent!r}"
                )

    def opencode_config(self) -> dict:
        default = next(agent for agent in self.agents if agent.name == self.default_agent)
        generated = {
            "$schema": "https://opencode.ai/config.json",
            "model": str(default.model),
            "agent": {agent.name: self._agent_config(agent) for agent in self.agents},
        }
        if self.commands:
            generated["command"] = {
                command.name: command.to_opencode() for command in self.commands
            }
        if self.mcp_servers:
            generated["mcp"] = {
                server.name: server.to_opencode() for server in self.mcp_servers
            }
        return _deep_merge(generated, self.raw_config)

    def compile(self) -> Path:
        """Atomically write the app-local engine configuration."""
        root = self.project_root
        root.mkdir(parents=True, exist_ok=True)
        self.engine_directory.mkdir(parents=True, exist_ok=True)
        if self.workspace is not None:
            self.workspace.ensure(root)
        for tool in self.tools:
            tool.compile(root)
        for skill in self.skills:
            skill.compile(root)
        target = root / "opencode.json"
        payload = json.dumps(self.opencode_config(), indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".opencode-", suffix=".tmp", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            os.replace(temporary, target)
        except BaseException:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
            raise
        return target

    def runtime(self) -> AgentRuntime:
        return AgentRuntime(self)

    def to_config(self) -> AppConfig:
        """Adapt the high-level definition to the existing desktop shell."""
        return AppConfig(
            app_id=self.id,
            app_title=self.title,
            app_root=self.root,
            default_agent=self.default_agent,
            engine_directory=self.engine_directory,
            workspace_dirname=self.workspace.dirname if self.workspace else "workspace",
            workspace_folders=self.workspace.folders if self.workspace else (),
            diagnostic_policy=self.diagnostic_policy,
        )

    def run(self) -> None:
        """Compile and launch the existing desktop shell for this app."""
        from .runtime import run

        run(self)


class AgentRuntime:
    """One managed OpenCode process implementing an ``App`` definition."""

    def __init__(self, app: App):
        app.apply_bundle_environment()
        self.app = app
        self.server = OpenCodeServer(app.project_root)
        self.client: OpenCodeClient | None = None
        self.sessions: SessionManager | None = None
        self.audit: ApprovalAuditLog | None = None
        self.diagnostics: Diagnostics | None = None
        self.traces = None
        self.runs: RunManager | None = None
        self.tool_server = ToolServer(app.tools) if app.tools else None

    @property
    def started(self) -> bool:
        return self.client is not None

    async def start(self) -> AgentRuntime:
        if self.started:
            return self
        await asyncio.to_thread(self.app.compile)
        try:
            if self.tool_server is not None:
                environment = await asyncio.to_thread(self.tool_server.start)
                self.server.set_environment(environment)
            port = await asyncio.to_thread(self.server.start)
            client = OpenCodeClient(port, directory=self.app.engine_directory)
            await asyncio.to_thread(self._preflight, client)
        except BaseException:
            await asyncio.to_thread(self.server.stop)
            if self.tool_server is not None:
                await asyncio.to_thread(self.tool_server.stop)
            raise
        agents = {agent.name: agent for agent in self.app.agents}
        store = SessionStore(self.app.project_root / ".spiritus")
        audit = ApprovalAuditLog(self.app.project_root / ".spiritus")
        diagnostics = Diagnostics(
            self.app.project_root / ".spiritus", self.app.diagnostic_policy
        )
        self.client = client
        self.audit = audit
        self.diagnostics = diagnostics
        self.traces = diagnostics.traces
        self.sessions = SessionManager(
            client,
            agents,
            self.app.default_agent,
            store,
            audit,
            {command.name: command for command in self.app.commands},
            diagnostics=diagnostics,
        )
        self.runs = RunManager(self.sessions)
        return self

    def _preflight(self, client: OpenCodeClient) -> None:
        live_agents = {item["name"]: item for item in client.agents()}
        providers = client.providers()
        provider_map = {item["id"]: item for item in providers.get("all", [])}

        for agent in self.app.agents:
            try:
                loaded = live_agents[agent.name]
            except KeyError as exc:
                raise RuntimeError(f"OpenCode did not load agent {agent.name!r}") from exc
            if loaded.get("model") != agent.model.as_request():
                raise RuntimeError(
                    f"OpenCode resolved {agent.name!r} to {loaded.get('model')!r}, "
                    f"expected {agent.model.as_request()!r}"
                )
            provider = provider_map.get(agent.model.provider_id)
            if provider is None:
                raise RuntimeError(f"provider {agent.model.provider_id!r} is unavailable")
            models = provider.get("models", {})
            model_ids = set(models) if isinstance(models, dict) else {
                item["id"] for item in models
            }
            if agent.model.model_id not in model_ids:
                raise RuntimeError(f"model {str(agent.model)!r} is unavailable")
        if self.app.tools:
            available_tools = set(client.tool_ids())
            missing = {
                tool.engine_name for tool in self.app.tools
                if tool.engine_name not in available_tools
            }
            if missing:
                raise RuntimeError(f"OpenCode did not load tools {sorted(missing)!r}")
        if self.app.commands:
            loaded_commands = {item["name"] for item in client.commands()}
            missing_commands = {
                command.name for command in self.app.commands
                if command.name not in loaded_commands
            }
            if missing_commands:
                raise RuntimeError(
                    f"OpenCode did not load commands {sorted(missing_commands)!r}"
                )
        if self.app.mcp_servers:
            statuses = client.mcp_status()
            failed = {
                server.name: statuses.get(server.name)
                for server in self.app.mcp_servers
                if server.enabled
                and statuses.get(server.name, {}).get("status") != "connected"
            }
            if failed:
                raise RuntimeError(f"OpenCode MCP preflight failed: {failed!r}")

    async def stop(self) -> None:
        self.sessions = None
        self.audit = None
        self.traces = None
        self.runs = None
        self.client = None
        await asyncio.to_thread(self.server.stop)
        if self.tool_server is not None:
            await asyncio.to_thread(self.tool_server.stop)

    async def __aenter__(self) -> AgentRuntime:
        return await self.start()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.stop()

    def require_sessions(self) -> SessionManager:
        if self.sessions is None:
            raise RuntimeError("agent runtime has not been started")
        return self.sessions


__all__ = ["AgentRuntime", "App"]
