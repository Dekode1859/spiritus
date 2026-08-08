"""Declarative application, agent, and model configuration contract."""
from __future__ import annotations

import json

import pytest

from spiritus import (
    Access,
    Agent,
    App,
    Model,
    Workspace,
    WorkspaceAccess,
    WorkspaceFolder,
)


def make_agent(**overrides) -> Agent:
    values = {
        "name": "assistant",
        "description": "Answers one task",
        "prompt": "Follow the user's instructions.",
        "model": "opencode/mimo-v2.5-free",
    }
    values.update(overrides)
    return Agent(**values)


class TestModel:
    def test_parses_provider_and_nested_model_id(self):
        model = Model.parse("openrouter/openai/gpt-5")
        assert model.provider_id == "openrouter"
        assert model.model_id == "openai/gpt-5"
        assert str(model) == "openrouter/openai/gpt-5"

    def test_parse_is_idempotent(self):
        model = Model("opencode", "mimo-v2.5-free")
        assert Model.parse(model) is model

    @pytest.mark.parametrize("value", ["", "missing-slash", "/model", "provider/"])
    def test_rejects_incomplete_ids(self, value):
        with pytest.raises(ValueError):
            Model.parse(value)

    def test_request_shape_matches_the_pinned_engine(self):
        assert Model.parse("opencode/model").as_request() == {
            "providerID": "opencode",
            "modelID": "model",
        }


class TestAgent:
    def test_normalizes_model_label_and_duplicate_tools(self):
        agent = make_agent(tools=("read", "read", "grep"))
        assert agent.model == Model("opencode", "mimo-v2.5-free")
        assert agent.label == "Assistant"
        assert agent.tools == ("read", "grep")

    def test_empty_tools_compile_to_explicit_known_denials(self):
        compiled = make_agent().to_opencode()
        assert "*" not in compiled["tools"]
        assert compiled["tools"]["StructuredOutput"] is True
        assert compiled["tools"]["read"] is False
        assert compiled["tools"]["bash"] is False
        assert compiled["permission"]["read"] == "deny"
        assert compiled["permission"]["external_directory"] == "deny"

    def test_declared_tools_override_the_deny_all_default(self):
        compiled = make_agent(tools=("read",)).to_opencode()
        assert compiled["tools"]["read"] is True
        assert compiled["permission"]["read"] == "allow"

    @pytest.mark.parametrize("name", ["", "Has Spaces", "UPPER", "-leading"])
    def test_rejects_unstable_names(self, name):
        with pytest.raises(ValueError):
            make_agent(name=name)

    @pytest.mark.parametrize("field", ["description", "prompt"])
    def test_requires_human_facing_agent_metadata(self, field):
        with pytest.raises(ValueError):
            make_agent(**{field: "   "})


class TestApp:
    def test_compiles_the_minimum_single_agent_configuration(self, tmp_path):
        app = App("probe", "Probe", tmp_path, (make_agent(),))
        config = app.opencode_config()
        assert config["model"] == "opencode/mimo-v2.5-free"
        compiled = config["agent"]["assistant"]
        assert compiled["description"] == "Answers one task"
        assert compiled["mode"] == "primary"
        assert compiled["prompt"] == "Follow the user's instructions."
        assert compiled["model"] == "opencode/mimo-v2.5-free"
        assert compiled["tools"]["StructuredOutput"] is True
        assert set(compiled["permission"].values()) == {"deny"}

    def test_compile_writes_inspectable_json_atomically(self, tmp_path):
        app = App("probe", "Probe", tmp_path, (make_agent(),))
        path = app.compile()
        assert path == tmp_path / "opencode.json"
        assert json.loads(path.read_text(encoding="utf-8")) == app.opencode_config()
        assert path.read_bytes().endswith(b"\n")
        assert not list(tmp_path.glob(".opencode-*.tmp"))
        assert app.engine_directory.is_dir()

    def test_raw_escape_hatch_applies_last_without_dropping_generated_fields(self, tmp_path):
        app = App(
            "probe",
            "Probe",
            tmp_path,
            (make_agent(),),
            raw_config={
                "agent": {"assistant": {"temperature": 0.2}},
                "provider": {"opencode": {"options": {"timeout": 30}}},
            },
        )
        config = app.opencode_config()
        assert config["agent"]["assistant"]["temperature"] == 0.2
        assert config["agent"]["assistant"]["prompt"]
        assert config["provider"]["opencode"]["options"]["timeout"] == 30

    def test_default_agent_controls_the_top_level_model(self, tmp_path):
        first = make_agent(name="first", model="opencode/first")
        second = make_agent(name="second", model="opencode/second")
        app = App("probe", "Probe", tmp_path, (first, second), default_agent="second")
        assert app.default_agent == "second"
        assert app.opencode_config()["model"] == "opencode/second"

    def test_adapts_to_the_existing_desktop_entry_point(self, tmp_path):
        app = App("probe", "Probe", tmp_path, (make_agent(),))
        config = app.to_config()
        assert config.app_id == "probe"
        assert config.app_title == "Probe"
        assert config.app_root == tmp_path
        assert config.default_agent == "assistant"

    def test_run_hands_the_full_app_definition_to_the_desktop_shell(
        self, tmp_path, monkeypatch
    ):
        import spiritus.runtime as runtime

        app = App("probe", "Probe", tmp_path, (make_agent(),))
        received = []
        monkeypatch.setattr(runtime, "run", received.append)

        app.run()

        assert received == [app]

    def test_rejects_no_agents_duplicate_agents_and_unknown_default(self, tmp_path):
        with pytest.raises(ValueError, match="at least one"):
            App("probe", "Probe", tmp_path, ())
        with pytest.raises(ValueError, match="unique"):
            App("probe", "Probe", tmp_path, (make_agent(), make_agent()))
        with pytest.raises(ValueError, match="not declared"):
            App("probe", "Probe", tmp_path, (make_agent(),), default_agent="missing")

    def test_named_workspace_compiles_exact_folder_policy(self, tmp_path):
        agent = make_agent(
            workspace_access=(
                WorkspaceAccess("inbox", access=Access.ASK, read=True),
            )
        )
        app = App(
            "probe",
            "Probe",
            tmp_path,
            (agent,),
            workspace=Workspace((WorkspaceFolder("inbox"), WorkspaceFolder("private"))),
        )
        compiled = app.opencode_config()["agent"]["assistant"]
        folder = (tmp_path / "workspace" / "inbox").resolve()

        assert compiled["tools"]["read"] is True
        assert compiled["permission"]["read"] == "allow"
        assert compiled["permission"]["external_directory"] == {
            "*": "deny",
            str(folder): "ask",
            str(folder / "*"): "ask",
            str(folder / "**"): "ask",
        }
        app.compile()
        assert (tmp_path / "workspace" / "inbox").is_dir()
        assert (tmp_path / "workspace" / "private").is_dir()

    def test_workspace_references_must_be_declared(self, tmp_path):
        with pytest.raises(ValueError, match="no workspace"):
            App(
                "probe",
                "Probe",
                tmp_path,
                (make_agent(workspace_access=(WorkspaceAccess("inbox"),)),),
            )
        with pytest.raises(ValueError, match="unknown workspace"):
            App(
                "probe",
                "Probe",
                tmp_path,
                (make_agent(workspace_access=(WorkspaceAccess("missing"),)),),
                workspace=Workspace((WorkspaceFolder("inbox"),)),
            )

    def test_declared_delegation_compiles_to_exact_task_policy(self, tmp_path):
        primary = make_agent(name="primary", delegates=("worker",))
        worker = make_agent(name="worker", mode="subagent")
        app = App("probe", "Probe", tmp_path, (primary, worker))
        compiled = app.opencode_config()["agent"]

        assert compiled["primary"]["tools"]["task"] is True
        assert compiled["primary"]["permission"]["task"] == {
            "*": "deny",
            "worker": "allow",
        }
        assert compiled["worker"]["tools"]["task"] is False
        assert compiled["worker"]["permission"]["task"] == "deny"

    def test_delegation_rejects_unknown_self_primary_and_cycles(self, tmp_path):
        with pytest.raises(ValueError, match="unknown agent"):
            App(
                "probe",
                "Probe",
                tmp_path,
                (make_agent(delegates=("missing",)),),
            )
        with pytest.raises(ValueError, match="itself"):
            App(
                "probe",
                "Probe",
                tmp_path,
                (make_agent(delegates=("assistant",)),),
            )
        with pytest.raises(ValueError, match="mode"):
            App(
                "probe",
                "Probe",
                tmp_path,
                (
                    make_agent(name="primary", delegates=("worker",)),
                    make_agent(name="worker"),
                ),
            )
        with pytest.raises(ValueError, match="cycle"):
            App(
                "probe",
                "Probe",
                tmp_path,
                (
                    make_agent(name="one", mode="all", delegates=("two",)),
                    make_agent(name="two", mode="all", delegates=("one",)),
                ),
            )
