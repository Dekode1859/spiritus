"""
UI Bridge — the JS↔Python API exposed via PyWebView.

Generic plumbing only. Every method either:
  - relays app-supplied configuration to the UI (get_config), or
  - performs a generic operation (storage CRUD, provider auth, dialogs).

The bridge holds an ``AppConfig`` but treats its domain fields as opaque data
to forward — it never branches on what the app *is*.
"""
from __future__ import annotations

import json
import threading
import uuid

import jsonschema
import webview

from . import agents as agents_mod
from . import providers as providers_mod
from . import storage
from .config import AppConfig
from .events import (
    ApprovalRequested,
    ApprovalResolved,
    EventNormalizer,
    RunCompleted,
    RunFailed,
    RunIdle,
    RunStarted,
    TextDelta,
    TextSnapshot,
    ToolCompleted,
    ToolFailed,
    ToolProgress,
    ToolStarted,
)
from .integrations.browser_agent import SCRIPT as _BROWSER_AGENT
from .permissions import ApprovalDecision
from .persistence import ApprovalAuditLog
from .runtime import paths
from .runtime.client import OpenCodeClient
from .runtime.server import OpenCodeServer
from .runtime.subproc import python_c
from .runtime.window import WindowController
from .runtime.windows import hidden_console_kwargs
from .tracing import (
    Diagnostics,
    FailureKind,
    FailureLayer,
    RunFailure,
    TraceKind,
)


class Bridge:
    def __init__(
        self,
        config: AppConfig,
        server: OpenCodeServer,
        diagnostics: Diagnostics | None = None,
    ):
        self._config = config
        self._server = server
        self._window: WindowController | None = None
        self._project_root = paths.project_root(config.app_root, config.app_id)
        self._workspace = paths.workspace_path(
            config.app_root, config.app_id, config.workspace_dirname
        )
        # Ensure the app's declared folders exist (names come from the app).
        storage.ensure_dirs(self._workspace, config.folder_names())
        self._approval_audit = ApprovalAuditLog(self._project_root / ".spiritus")
        self.diagnostics = diagnostics or Diagnostics(
            self._project_root / ".spiritus", config.diagnostic_policy
        )
        self._traces = self.diagnostics.traces
        self._runs = self.diagnostics.runs
        self._ui_runs: dict[str, dict] = {}
        self._ui_runs_lock = threading.RLock()

    def attach_window(self, window: WindowController) -> None:
        """Attach the runtime-owned window after PyWebView creates it."""
        self._window = window

    @property
    def window(self) -> WindowController | None:
        """The application window, available after runtime startup."""
        return self._window

    # ── Config ───────────────────────────────────────────────────────────────
    def get_config(self) -> dict:
        """Everything the UI needs, including app-supplied branding/taxonomy."""
        return {
            "opencode_port": self._server.port,
            "workspace_path": str(self._workspace),
            "project_path": str(self._project_root),
            "app_title": self._config.app_title,
            "app_id": self._config.app_id,
            "workspace_folders": self._config.folders_payload(),
            "agents": agents_mod.load_agents(self._project_root),
            "default_model": agents_mod.default_model(self._project_root),
            "default_agent": self._config.default_agent,
            "default_capture_folder": self._config.default_capture_folder,
        }

    # ── Agents / Sessions ───────────────────────────────────────────────────
    def _opencode(self) -> OpenCodeClient:
        if not self._server.port:
            raise RuntimeError("OpenCode server is not running")
        directory = self._config.engine_directory or self._project_root
        return OpenCodeClient(self._server.port, directory=directory)

    def list_agents(self) -> list[dict]:
        """Return only live agents explicitly declared by this application."""
        declared = {agent["name"] for agent in agents_mod.load_agents(self._project_root)}
        return [
            agent for agent in self._opencode().agents()
            if agent.get("name") in declared
        ]

    def list_sessions(self) -> list[dict]:
        return self._opencode().sessions()

    def create_session(self, title: str = "") -> dict:
        body = {"title": title.strip()} if title.strip() else {}
        return self._opencode().create_session(body)

    def session_history(self, session_id: str) -> list[dict]:
        try:
            return self._opencode().messages(session_id)
        except Exception:
            structured = self._latest_structured_result(session_id)
            if structured is None:
                raise
            # OpenCode 1.18.13 rejects a history read after a json_schema
            # completion. Preserve the established bridge contract with the
            # locally retained final result rather than making an application
            # interpret an engine transport defect.
            return [{
                "info": {
                    "id": "local_structured_result",
                    "sessionID": session_id,
                    "role": "assistant",
                    "structured": structured,
                },
                "parts": [{"type": "text", "text": json.dumps(structured)}],
            }]

    def run_artifact(self, run_id: str, name: str) -> object:
        """Return one locally retained result artifact for a durable bridge run."""
        return self._runs.artifact(run_id, name)

    def run_checkpoint(
        self, run_id: str, name: str, detail: dict | None = None
    ) -> dict:
        """Record an application-owned checkpoint on a bridge run."""
        record = self._runs.get(run_id)
        updated = self._runs.checkpoint(run_id, name, detail=detail or {})
        self._traces.append(
            TraceKind.RUN_CHECKPOINT,
            run_id=run_id,
            session_id=record.session_id,
            agent=record.agent,
            model=record.model,
            data={"name": name, "detail": detail or {}},
        )
        return {
            "ok": True,
            "run_id": run_id,
            "name": name,
            "status": updated.status.value,
        }

    def run_failure(
        self,
        run_id: str,
        kind: str,
        owner: str,
        message: str,
        stage: str,
        field_paths: list[str] | None = None,
    ) -> dict:
        """Record a failure discovered by application post-processing."""
        record = self._runs.get(run_id)
        if record.status.value != "completed":
            return {
                "ok": False,
                "run_id": run_id,
                "status": record.status.value,
                "error": "Run already has a terminal status",
            }
        failure = RunFailure(
            FailureKind(kind), owner, message, tuple(field_paths or ())
        )
        self._runs.fail(run_id, failure, stage=stage)
        self._traces.append(
            TraceKind.RUN_FAILED,
            run_id=run_id,
            session_id=record.session_id,
            agent=record.agent,
            model=record.model,
            failure_layer=(
                FailureLayer.OUTPUT
                if "output" in failure.kind.value
                else FailureLayer.RUNTIME
            ),
            data={
                "message": failure.message,
                "owner": failure.owner,
                "kind": failure.kind.value,
                "field_paths": list(failure.field_paths),
                "stage": stage,
            },
        )
        return {"ok": True, "run_id": run_id, "status": "failed"}

    def delete_session(self, session_id: str) -> dict:
        self._opencode().delete_session(session_id)
        return {"ok": True}

    def send_message(
        self,
        session_id: str,
        agent: str,
        model: dict | None,
        text: str,
    ) -> dict:
        return self.agent_run(
            session_id,
            agent,
            model,
            text,
            operation="chat.message",
        )

    def agent_run(
        self,
        session_id: str,
        agent: str,
        model: dict | None,
        text: str,
        *,
        operation: str,
        output_schema: dict | None = None,
    ) -> dict:
        """Start one bridge-originated agent operation with a durable run ID."""
        prompt = text.strip()
        if not prompt:
            raise ValueError("message cannot be empty")
        selected_agent = agent or self._config.default_agent
        raw_model = model or {}
        model_name = "/".join(
            filter(None, [raw_model.get("providerID"), raw_model.get("modelID")])
        )
        run_id = f"run_{uuid.uuid4().hex}"
        self._runs.create(
            run_id=run_id,
            operation=operation,
            session_id=session_id,
            agent=selected_agent,
            model=model_name,
        )
        self._traces.append(
            TraceKind.RUN_STARTED,
            run_id=run_id,
            session_id=session_id,
            agent=selected_agent,
            model=model_name,
        )
        self._traces.append(
            TraceKind.MODEL_REQUESTED,
            run_id=run_id,
            session_id=session_id,
            agent=selected_agent,
            model=model_name,
            data={"mode": "bridge", "prompt": prompt},
        )
        body = {"parts": [{"type": "text", "text": prompt}]}
        if selected_agent:
            body["agent"] = selected_agent
        if model:
            body["model"] = model
        if output_schema is not None:
            body["format"] = {"type": "json_schema", "schema": output_schema}
        with self._ui_runs_lock:
            if session_id in self._ui_runs:
                raise RuntimeError(f"session {session_id!r} already has an active bridge run")
            self._ui_runs[session_id] = {
                "run_id": run_id,
                "session_id": session_id,
                "agent": selected_agent,
                "model": model_name,
                "output_schema": output_schema,
                "structured": None,
                "text_parts": {},
                "completed_message_ids": set(),
            }
        try:
            self._opencode().prompt_async(session_id, body)
        except BaseException as exc:
            self._finish_bridge_failure(
                session_id,
                RunFailure(FailureKind.ENGINE_UNAVAILABLE, "engine", str(exc)),
                "agent.requested",
            )
            try:
                exc.run_id = run_id
            except (AttributeError, TypeError):
                pass
            raise
        return {"ok": True, "run_id": run_id, "session_id": session_id}

    def session_events(self, session_id: str):
        """Yield normalized, JSON-safe events for the same-origin UI SSE route."""
        normalizer = EventNormalizer(session_id)
        for envelope in self._opencode().events():
            for event in normalizer.feed(envelope):
                state = self._bridge_run(session_id)
                if isinstance(event, RunStarted):
                    if state:
                        self._bridge_checkpoint(state, "agent.started")
                    yield self._with_run_id({"type": "run.started", "session_id": session_id}, state)
                elif isinstance(event, TextDelta):
                    self._record_bridge_text(session_id, event.part_id, event.text)
                    yield {
                        "type": "text.delta",
                        "session_id": session_id,
                        "message_id": event.message_id,
                        "part_id": event.part_id,
                        "text": event.text,
                    }
                elif isinstance(event, ApprovalRequested):
                    self._approval_audit.append(
                        "approval.requested",
                        session_id=session_id,
                        request_id=event.request_id,
                        permission=event.permission,
                        patterns=list(event.patterns),
                        metadata=event.metadata,
                        always=list(event.always),
                    )
                    if state:
                        self._traces.append(
                            TraceKind.APPROVAL_REQUESTED,
                            run_id=state["run_id"],
                            session_id=session_id,
                            agent=state["agent"],
                            model=state["model"],
                            message_id=event.message_id,
                            call_id=event.call_id,
                            data={"permission": event.permission, "patterns": event.patterns},
                        )
                    yield {
                        "type": "approval.requested",
                        "session_id": session_id,
                        "request_id": event.request_id,
                        "permission": event.permission,
                        "patterns": list(event.patterns),
                        "metadata": event.metadata,
                        "always": list(event.always),
                    }
                elif isinstance(event, ApprovalResolved):
                    self._approval_audit.append(
                        "approval.resolved",
                        session_id=session_id,
                        request_id=event.request_id,
                        decision=event.decision.value,
                    )
                    if state:
                        self._traces.append(
                            TraceKind.APPROVAL_RESOLVED,
                            run_id=state["run_id"],
                            session_id=session_id,
                            agent=state["agent"],
                            model=state["model"],
                            data={"request_id": event.request_id, "decision": event.decision.value},
                        )
                    yield {
                        "type": "approval.resolved",
                        "session_id": session_id,
                        "request_id": event.request_id,
                        "decision": event.decision.value,
                    }
                elif isinstance(event, TextSnapshot):
                    self._replace_bridge_text(session_id, event.part_id, event.text)
                    yield {
                        "type": "text.snapshot",
                        "session_id": session_id,
                        "message_id": event.message_id,
                        "part_id": event.part_id,
                        "text": event.text,
                    }
                elif isinstance(event, ToolStarted):
                    self._trace_bridge_tool(state, TraceKind.TOOL_STARTED, event)
                    yield {
                        "type": "tool.started",
                        "session_id": session_id,
                        "message_id": event.message_id,
                        "part_id": event.part_id,
                        "call_id": event.call_id,
                        "tool": event.tool,
                        "arguments": event.arguments,
                    }
                elif isinstance(event, ToolProgress):
                    self._trace_bridge_tool(state, TraceKind.TOOL_PROGRESS, event)
                    yield {
                        "type": "tool.progress",
                        "session_id": session_id,
                        "message_id": event.message_id,
                        "part_id": event.part_id,
                        "call_id": event.call_id,
                        "tool": event.tool,
                        "title": event.title,
                        "metadata": event.metadata,
                    }
                elif isinstance(event, ToolCompleted):
                    self._trace_bridge_tool(state, TraceKind.TOOL_COMPLETED, event)
                    yield {
                        "type": "tool.completed",
                        "session_id": session_id,
                        "message_id": event.message_id,
                        "part_id": event.part_id,
                        "call_id": event.call_id,
                        "tool": event.tool,
                        "output": event.output,
                    }
                elif isinstance(event, ToolFailed):
                    self._trace_bridge_tool(state, TraceKind.TOOL_FAILED, event)
                    yield {
                        "type": "tool.failed",
                        "session_id": session_id,
                        "message_id": event.message_id,
                        "part_id": event.part_id,
                        "call_id": event.call_id,
                        "tool": event.tool,
                        "error": event.error,
                    }
                elif isinstance(event, RunCompleted):
                    state, first_completion = self._record_bridge_completion(session_id, event)
                    if state and first_completion:
                        self._bridge_checkpoint(state, "agent.completed")
                        self._traces.append(
                            TraceKind.MODEL_COMPLETED,
                            run_id=state["run_id"],
                            session_id=session_id,
                            agent=state["agent"],
                            model=state["model"],
                            message_id=event.message_id,
                        )
                    yield {
                        "type": "run.completed",
                        "session_id": session_id,
                        "message_id": event.message_id,
                        **({"run_id": state["run_id"]} if state else {}),
                    }
                elif isinstance(event, RunFailed):
                    if state:
                        self._finish_bridge_failure(
                            session_id,
                            RunFailure(FailureKind.MODEL_FAILED, "model", event.message),
                            "agent.completed",
                        )
                    yield {
                        "type": "run.failed",
                        "session_id": session_id,
                        "message": event.message,
                        **({"run_id": state["run_id"]} if state else {}),
                    }
                    return
                elif isinstance(event, RunIdle):
                    if state:
                        self._finish_bridge_success(session_id)
                    yield self._with_run_id({"type": "run.idle", "session_id": session_id}, state)
                    return

    def _bridge_run(self, session_id: str) -> dict | None:
        with self._ui_runs_lock:
            state = self._ui_runs.get(session_id)
            return dict(state) if state else None

    def _record_bridge_text(self, session_id: str, part_id: str, text: str) -> None:
        with self._ui_runs_lock:
            state = self._ui_runs.get(session_id)
            if state is not None:
                parts = state["text_parts"]
                parts[part_id] = parts.get(part_id, "") + text

    def _replace_bridge_text(self, session_id: str, part_id: str, text: str) -> None:
        with self._ui_runs_lock:
            state = self._ui_runs.get(session_id)
            if state is not None:
                state["text_parts"][part_id] = text

    def _record_bridge_completion(
        self, session_id: str, event: RunCompleted
    ) -> tuple[dict | None, bool]:
        with self._ui_runs_lock:
            state = self._ui_runs.get(session_id)
            if state is None:
                return None, False
            if event.structured is not None:
                state["structured"] = event.structured
            completed = state["completed_message_ids"]
            first_completion = event.message_id not in completed
            completed.add(event.message_id)
            return dict(state), first_completion

    @staticmethod
    def _with_run_id(payload: dict, state: dict | None) -> dict:
        return {**payload, **({"run_id": state["run_id"]} if state else {})}

    def _trace_bridge_tool(self, state: dict | None, kind: TraceKind, event) -> None:
        if state is None:
            return
        data = {"tool": event.tool}
        if isinstance(event, ToolStarted):
            data["arguments"] = event.arguments
        elif isinstance(event, ToolProgress):
            data.update({"title": event.title, "metadata": event.metadata})
        elif isinstance(event, ToolCompleted):
            data.update({"output": event.output, "metadata": event.metadata})
        else:
            data.update({"error": event.error, "metadata": event.metadata})
        self._traces.append(
            kind,
            run_id=state["run_id"],
            session_id=event.session_id,
            agent=state["agent"],
            model=state["model"],
            message_id=event.message_id,
            call_id=event.call_id,
            failure_layer=FailureLayer.TOOL if kind is TraceKind.TOOL_FAILED else None,
            data=data,
        )

    def _bridge_checkpoint(
        self, state: dict, name: str, *, detail: dict | None = None
    ) -> None:
        checkpoint_detail = detail or {}
        self._runs.checkpoint(state["run_id"], name, detail=checkpoint_detail)
        self._traces.append(
            TraceKind.RUN_CHECKPOINT,
            run_id=state["run_id"],
            session_id=state["session_id"],
            agent=state["agent"],
            model=state["model"],
            data={"name": name, "detail": checkpoint_detail},
        )

    def _finish_bridge_success(self, session_id: str) -> None:
        state = self._bridge_run(session_id)
        if state is None:
            return
        run_id = state["run_id"]
        try:
            output = "".join(state["text_parts"].values())
            schema = state["output_schema"]
            structured = None
            if schema is not None:
                structured = state["structured"]
                source = "completion_stream"
                if structured is None:
                    try:
                        structured = json.loads(output)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ValueError(
                            "OpenCode returned no structured output in the completion stream"
                        ) from exc
                    source = "visible_text"
                self._bridge_checkpoint(state, "output.parsed", detail={"source": source})
                jsonschema.validate(structured, schema)
                self._bridge_checkpoint(state, "output.validated")
                if not output:
                    output = json.dumps(structured, ensure_ascii=False)
            artifacts = {"agent.output": output}
            if structured is not None:
                artifacts["agent.structured"] = structured
            self._runs.complete(run_id, artifacts=artifacts)
            self._traces.append(
                TraceKind.RUN_COMPLETED,
                run_id=run_id,
                session_id=session_id,
                agent=state["agent"],
                model=state["model"],
            )
        except jsonschema.ValidationError as exc:
            self._finish_bridge_failure(
                session_id,
                RunFailure(
                    FailureKind.OUTPUT_SCHEMA_INVALID,
                    "application_contract",
                    str(exc),
                    (self._field_path(exc.absolute_path),),
                ),
                "output.validated",
            )
        except ValueError as exc:
            self._finish_bridge_failure(
                session_id,
                RunFailure(FailureKind.OUTPUT_PARSE_FAILED, "application_contract", str(exc)),
                "output.parsed",
            )
        except BaseException as exc:
            self._finish_bridge_failure(
                session_id,
                RunFailure(FailureKind.ENGINE_UNAVAILABLE, "engine", str(exc)),
                "agent.completed",
            )
        finally:
            with self._ui_runs_lock:
                self._ui_runs.pop(session_id, None)

    def _finish_bridge_failure(self, session_id: str, failure: RunFailure, stage: str) -> None:
        state = self._bridge_run(session_id)
        if state is None:
            return
        self._runs.fail(state["run_id"], failure, stage=stage)
        self._traces.append(
            TraceKind.RUN_FAILED,
            run_id=state["run_id"],
            session_id=session_id,
            agent=state["agent"],
            model=state["model"],
            failure_layer=FailureLayer.OUTPUT if "output" in failure.kind.value else FailureLayer.MODEL,
            data={"message": failure.message, "field_paths": failure.field_paths},
        )
        with self._ui_runs_lock:
            self._ui_runs.pop(session_id, None)

    def _latest_structured_result(self, session_id: str) -> object | None:
        matches = [
            record
            for record in self._runs.list()
            if record.session_id == session_id and "agent.structured" in record.artifacts
        ]
        if not matches:
            return None
        latest = max(matches, key=lambda record: record.completed_at or record.started_at)
        return latest.artifacts["agent.structured"]

    @staticmethod
    def _field_path(path) -> str:
        value = ""
        for item in path:
            value += f"[{item}]" if isinstance(item, int) else f".{item}"
        return value.lstrip(".") or "<root>"

    def reply_permission(
        self,
        request_id: str,
        decision: str,
        message: str = "",
    ) -> dict:
        parsed = ApprovalDecision.parse(decision)
        self._opencode().reply_permission(
            request_id,
            parsed.value,
            message=message.strip() or None,
        )
        return {"ok": True}

    # ── Providers / Auth ───────────────────────────────────────────────────────
    def get_providers(self) -> dict:
        return providers_mod.list_providers(self._server.port)

    def _restart_after(self, action) -> dict:
        """Run a provider mutation, restart the engine, report the new port.

        ``action`` returns a dict of extra fields to merge into the success
        result (or None). Any exception becomes ``{"ok": False, "error": ...}``.
        """
        try:
            extra = action() or {}
            self._server.stop()
            new_port = self._server.start()
            return {"ok": True, "port": new_port, **extra}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_provider_key(self, provider_id: str, api_key: str) -> dict:
        return self._restart_after(
            lambda: providers_mod.save_key(self._server.home_dir, provider_id, api_key))

    def remove_provider_key(self, provider_id: str) -> dict:
        return self._restart_after(
            lambda: providers_mod.remove_key(self._server.home_dir, provider_id))

    def set_default_model(self, provider_id: str, model_id: str) -> dict:
        return self._restart_after(lambda: {
            "model": providers_mod.set_default_model(
                self._project_root, provider_id, model_id)["model"],
        })

    # ── Workspace / Storage ─────────────────────────────────────────────────────
    def workspace_tree(self) -> dict:
        """Folder tree with counts. Folder list is application-defined."""
        tree = {}
        for f in self._config.workspace_folders:
            tree[f.name] = {
                "count": storage.count_dir(self._workspace, f.name),
                "path": f.name,
                "icon": f.icon,
                "label": f.display(),
            }
        return tree

    def workspace_list(self, folder: str = "") -> list:
        return storage.list_dir(self._workspace, folder)

    def workspace_read(self, rel_path: str) -> dict:
        return storage.read(self._workspace, rel_path)

    def workspace_write(self, rel_path: str, content: str) -> dict:
        return storage.write(self._workspace, rel_path, content)

    def workspace_delete(self, rel_path: str) -> dict:
        return storage.delete(self._workspace, rel_path)

    def workspace_new_note_path(self, title: str = "") -> str:
        folder = self._config.default_capture_folder or ""
        return storage.timestamped_name(folder, title) if folder else ""

    # ── Dialogs ──────────────────────────────────────────────────────────────
    def open_folder_dialog(self) -> str:
        """Ask the user for a folder and return its path ("" if cancelled).

        Runs the picker in a short-lived subprocess rather than through
        ``window.create_file_dialog``. pywebview marshals almost every GUI call
        onto the toolkit thread via Invoke(), but *not* create_file_dialog — so
        when a bridge call arrives on one of the UI server's worker threads (as
        all of them do), the Windows folder dialog is constructed off the GUI
        thread and silently never appears. A separate process owns its own main
        thread, which sidesteps that entirely and behaves the same on macOS.
        """
        import subprocess

        script = (
            "import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "root.attributes('-topmost', True)\n"
            "path = filedialog.askdirectory(title='Choose a folder')\n"
            "root.destroy()\n"
            "print(path or '')\n"
        )
        try:
            result = subprocess.run(
                python_c(script),
                capture_output=True, text=True, timeout=300,
                **hidden_console_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return ""
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    def open_file_dialog(self) -> list[str]:
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
        )
        if not result:
            return []
        return list(result) if isinstance(result, (list, tuple)) else [result]

    # ── Application browser ───────────────────────────────────────────────────
    def open_external(self, url: str) -> dict:
        target = str(url or "").strip()
        if not target:
            return {"ok": False, "error": "No URL provided"}
        try:
            import webbrowser

            webbrowser.open(target, new=2)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_open(self, url: str) -> dict:
        """Launch a headed Playwright Chromium browser at the given URL.
        Leaves the app window untouched and returns {ok, port} for the local
        HTTP control API. The browser opens at its platform default size.
        """
        import json
        import subprocess

        self._browser_close_internal()

        # Register atexit once so a clean app exit also kills the browser.
        if not getattr(self, "_browser_atexit_registered", False):
            import atexit
            atexit.register(self._browser_close_internal)
            self._browser_atexit_registered = True

        # Launch the browser in its own process at the platform default size.
        try:
            profile_dir = str(self._workspace / "browser-profile")
            proc = subprocess.Popen(
                python_c(_BROWSER_AGENT, url, profile_dir),
                stdin=subprocess.PIPE,   # agent watches stdin; EOF → agent exits
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **hidden_console_kwargs(),
            )
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read(500)
                return {"ok": False, "error": err or "browser agent failed to start"}
            info = json.loads(line.strip())
            if not info.get("ok"):
                proc.terminate()
                return info
            self._browser_proc = proc
            self._browser_port = info["port"]
            # Watch for subprocess exit so the UI is notified immediately
            # rather than waiting for the health poll.
            import threading
            threading.Thread(
                target=self._watch_browser_exit,
                args=(proc,),
                daemon=True,
            ).start()
            return {"ok": True, "port": info["port"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_close(self) -> dict:
        return self._browser_close_internal()

    def browser_detect_fields(self) -> dict:
        """Scan the active page for HTML form fields and return structured data."""
        port = getattr(self, "_browser_port", None)
        if not port:
            return {"ok": False, "error": "Browser not open"}
        try:
            import json as _json
            import urllib.request
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/detect-fields",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return _json.loads(resp.read().decode())
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_scrape(self, url: str) -> dict:
        """Deterministically fetch a page's visible text for downstream extraction.

        Returns {ok, url, title, text}. If the headed application browser is
        already running, route the scrape through it (one Chrome per profile
        dir is the OS limit) using a throwaway tab. Otherwise launch a dedicated
        headless Chromium with the same persistent profile so logged-in pages
        resolve — modeled on the export_pdf subprocess.
        """
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "No URL provided"}

        # ── Reuse the running headed browser if one is open ───────────────────
        port = getattr(self, "_browser_port", None)
        if port:
            try:
                import json as _json
                import urllib.request
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/scrape",
                    method="POST",
                    data=_json.dumps({"url": url}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=40) as resp:
                    return _json.loads(resp.read().decode())
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # ── Otherwise scrape headless with the persistent profile ─────────────
        import json as _json
        import subprocess
        profile_dir = str(self._workspace / "browser-profile")
        script = (
            "import sys, json, pathlib, os, shutil\n"
            "from playwright.sync_api import sync_playwright\n"
            "url, user_dir = sys.argv[1], sys.argv[2]\n"
            "def _system_chrome_available():\n"
            "    names = ['google-chrome', 'google-chrome-stable', 'chrome']\n"
            "    if sys.platform == 'win32':\n"
            "        local = os.environ.get('LOCALAPPDATA', '')\n"
            "        pf = os.environ.get('PROGRAMFILES', '')\n"
            "        pf86 = os.environ.get('PROGRAMFILES(X86)', '')\n"
            "        paths = [os.path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'), os.path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'), os.path.join(pf86, 'Google', 'Chrome', 'Application', 'chrome.exe')]\n"
            "        return any(os.path.isfile(p) for p in paths) or bool(shutil.which('chrome.exe'))\n"
            "    if sys.platform == 'darwin':\n"
            "        paths = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]\n"
            "        return any(os.path.isfile(p) for p in paths)\n"
            "    return any(shutil.which(name) for name in names)\n"
            "_browser_channel = 'chrome' if _system_chrome_available() else None\n"
            "lock = pathlib.Path(user_dir) / 'SingletonLock'\n"
            "try:\n"
            "    if lock.exists() or lock.is_symlink(): lock.unlink()\n"
            "except Exception: pass\n"
            "pathlib.Path(user_dir).mkdir(parents=True, exist_ok=True)\n"
            "with sync_playwright() as pw:\n"
            "    ctx = pw.chromium.launch_persistent_context(\n"
            "        user_dir, headless=True, channel=_browser_channel,\n"
            "        args=['--disable-blink-features=AutomationControlled'])\n"
            "    page = ctx.pages[0] if ctx.pages else ctx.new_page()\n"
            "    try:\n"
            "        page.goto(url, wait_until='domcontentloaded', timeout=25000)\n"
            "        try: page.wait_for_timeout(1200)\n"
            "        except Exception: pass\n"
            "        title = ''\n"
            "        try: title = page.title()\n"
            "        except Exception: pass\n"
            "        text = page.evaluate('() => document.body ? document.body.innerText : \"\"')\n"
            "        print(json.dumps({'ok': True, 'url': page.url, 'title': title, 'text': text or ''}))\n"
            "    finally:\n"
            "        ctx.close()\n"
        )
        try:
            result = subprocess.run(
                python_c(script, url, profile_dir),
                capture_output=True, text=True, timeout=60,
                **hidden_console_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Scrape timed out (>60s)"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "scrape failed").strip()
            return {"ok": False, "error": err[-400:]}
        # The script prints one JSON line; tolerate any preceding stdout noise.
        lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return {"ok": False, "error": "Scrape produced no output"}
        try:
            return _json.loads(lines[-1])
        except Exception as e:
            return {"ok": False, "error": f"Could not parse scrape output: {e}"}

    def browser_get_profile_status(self) -> dict:
        """Return whether a validated browser profile exists, plus account metadata."""
        meta_path = self._workspace / "browser-profile" / "profile-meta.json"
        if meta_path.exists():
            try:
                import json as _json
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                return {
                    "exists": True,
                    "google_email": meta.get("google_email"),
                    "setup_date": meta.get("setup_date"),
                }
            except Exception:
                pass
        return {"exists": False}

    def browser_setup_profile(self) -> dict:
        """Open a headed Chromium with persistent context at Google sign-in."""
        (self._workspace / "browser-profile").mkdir(parents=True, exist_ok=True)
        return self.browser_open("https://accounts.google.com")

    def browser_check_google_login(self) -> dict:
        """Verify Google session cookies exist and extract the account email.
        On success, writes profile-meta.json so the profile is marked as set up."""
        import json as _json
        import urllib.request
        port = getattr(self, "_browser_port", None)
        if not port:
            return {"ok": False, "error": "Browser not open"}
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/check-google-login",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = _json.loads(resp.read().decode())
            if result.get("ok") and result.get("logged_in"):
                import datetime
                meta = {
                    "google_email": result.get("email"),
                    "setup_date": datetime.date.today().isoformat(),
                }
                meta_path = self._workspace / "browser-profile" / "profile-meta.json"
                meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_reset_profile(self) -> dict:
        """Delete all browser profile data (saved sessions, cookies, logins)."""
        import shutil
        self._browser_close_internal()
        profile_dir = self._workspace / "browser-profile"
        if profile_dir.exists():
            try:
                shutil.rmtree(profile_dir)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / ".gitkeep").touch()
        return {"ok": True}

    def _browser_close_internal(self) -> dict:
        proc = getattr(self, "_browser_proc", None)
        port = getattr(self, "_browser_port", None)
        if proc:
            if port:
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/stop",
                        method="POST",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                    )
                    urllib.request.urlopen(req, timeout=2)
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                pass
            self._browser_proc = None
            self._browser_port = None
        return {"ok": True}

    def _watch_browser_exit(self, proc) -> None:
        """Block until the browser agent subprocess exits and notify the UI."""
        try:
            proc.wait()
        except Exception:
            pass
        # Notify JS so the UI re-renders (shows "Open Application" button, etc.).
        try:
            if webview.windows:
                webview.windows[0].evaluate_js(
                    'typeof _onBrowserProcessDied === "function" && _onBrowserProcessDied()'
                )
        except Exception:
            pass

    # ── Export ───────────────────────────────────────────────────────────────
    def export_pdf(self, html: str, filename: str, out_dir: str = "") -> dict:
        """Render HTML to a PDF via Playwright/Chromium and save it to disk.

        Writes to ``out_dir`` when given (the caller having picked a folder via
        ``open_folder_dialog``), otherwise to the platform's Downloads folder.
        ``pathlib`` and pywebview's folder dialog are both cross-platform, so no
        per-OS branching is needed here.

        Runs Playwright in a subprocess to avoid greenlet/pywebview thread
        conflicts."""
        import os
        import pathlib
        import subprocess
        import tempfile

        if out_dir:
            target = pathlib.Path(out_dir).expanduser()
            if not target.is_dir():
                return {"ok": False, "error": f"Folder not found: {target}"}
        else:
            target = pathlib.Path.home() / "Downloads"
            target.mkdir(parents=True, exist_ok=True)

        # Strip any path separators a caller may have put in the filename so the
        # chosen folder is always where the file lands.
        path = target / pathlib.Path(filename).name

        stem, suffix = path.stem, path.suffix
        i = 1
        while path.exists():
            path = target / f"{stem}_{i}{suffix}"
            i += 1

        # Write HTML to a temp file so the subprocess can read it cleanly
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, mode="w", encoding="utf-8"
            )
            tmp.write(html)
            tmp.close()
        except Exception as e:
            return {"ok": False, "error": f"failed to write temp file: {e}"}

        # Playwright runs in a subprocess — avoids conflicts with pywebview's
        # internal thread/greenlet model that cause sync_playwright() to hang.
        script = (
            "from playwright.sync_api import sync_playwright\n"
            f"html_path = {repr(tmp.name)}\n"
            f"pdf_path  = {repr(str(path))}\n"
            "with sync_playwright() as pw:\n"
            "    b = pw.chromium.launch()\n"
            "    p = b.new_page()\n"
            "    p.set_content(open(html_path, encoding='utf-8').read(), wait_until='domcontentloaded')\n"
            "    p.pdf(path=pdf_path, format='Letter',\n"
            "          margin={'top':'0','right':'0','bottom':'0','left':'0'},\n"
            "          print_background=True)\n"
            "    b.close()\n"
        )

        try:
            result = subprocess.run(
                python_c(script),
                capture_output=True,
                text=True,
                timeout=60,
                **hidden_console_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "PDF generation timed out (>60s)"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            return {"ok": False, "error": err[-400:]}

        return {"ok": True, "path": str(path), "filename": path.name}
