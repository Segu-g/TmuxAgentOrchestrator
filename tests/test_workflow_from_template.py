"""Tests for v1.2.28 — YAML-driven workflow template execution.

Tests for:
- WorkflowTemplate dataclass and VariableSpec
- load_workflow_template() — loads and parses YAML templates
- render_template() — variable substitution and WorkflowSubmit dict generation
- list_templates() — template catalogue from a directory
- POST /workflows/from-template — REST endpoint
- GET /workflows/templates — template listing endpoint
- Error cases: missing required vars, unknown template, bad YAML

Design references:
- Argo Workflows parameters: ``{{inputs.parameters.message}}`` substitution
  https://argo-workflows.readthedocs.io/en/latest/walk-through/parameters/ (2025)
- Azure Pipelines template parameters
  https://learn.microsoft.com/en-us/azure/devops/pipelines/process/templates (2025)
- Python str.format_map() — lightweight stdlib substitution
- DESIGN.md §10.103 (v1.2.28)
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from tmux_orchestrator.infrastructure.workflow_loader import (
    VariableSpec,
    WorkflowTemplate,
    list_templates,
    load_workflow_template,
    render_template,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal in-memory YAML templates
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_templates_dir(tmp_path: Path) -> Path:
    """Return a temp directory with a few YAML templates for testing."""
    templates_dir = tmp_path / "workflows"
    templates_dir.mkdir()
    generic_dir = templates_dir / "generic"
    generic_dir.mkdir()

    # Simple template with required and optional vars
    (generic_dir / "simple.yaml").write_text(
        textwrap.dedent("""\
        name: "Simple: {topic}"
        description: "A simple 2-phase template"
        variables:
          topic:
            description: "Main topic"
            required: true
          language:
            description: "Language"
            required: false
            default: "python"
        defaults:
          timeout: 120
          pattern: "single"
        phases:
          - name: "phase-a"
            pattern: "single"
            context: "Do something about {topic} in {language}."
          - name: "phase-b"
            pattern: "single"
            depends_on: ["phase-a"]
            context: "Review the {topic} result."
        """)
    )

    # Template with no variables section
    (generic_dir / "novars.yaml").write_text(
        textwrap.dedent("""\
        name: "No Variables Template"
        description: "Template with no declared variables"
        phases:
          - name: "only-phase"
            pattern: "single"
            context: "Do something."
        """)
    )

    # Template with required_tags in a phase
    (generic_dir / "tagged.yaml").write_text(
        textwrap.dedent("""\
        name: "Tagged: {task}"
        description: "Template with required_tags"
        variables:
          task:
            description: "Task description"
            required: true
        defaults:
          timeout: 300
        phases:
          - name: "worker-phase"
            pattern: "single"
            context: "Work on {task}."
            required_tags: ["worker", "{task}_specialist"]
        """)
    )

    # Non-phase-based template (should be excluded from list_templates)
    (templates_dir / "old-style.yaml").write_text(
        textwrap.dedent("""\
        workflow:
          endpoint: /workflows/old
        defaults:
          language: python
        feature: "some feature"
        """)
    )

    # Malformed YAML (not a mapping — excluded from list_templates)
    (generic_dir / "malformed.yaml").write_text("- item1\n- item2\n")

    return templates_dir


# ---------------------------------------------------------------------------
# Tests: VariableSpec and WorkflowTemplate dataclasses
# ---------------------------------------------------------------------------


class TestVariableSpec:
    def test_defaults(self) -> None:
        spec = VariableSpec()
        assert spec.description == ""
        assert spec.required is True
        assert spec.default == ""

    def test_optional_with_default(self) -> None:
        spec = VariableSpec(description="Language", required=False, default="python")
        assert not spec.required
        assert spec.default == "python"


class TestWorkflowTemplate:
    def test_defaults(self) -> None:
        tmpl = WorkflowTemplate()
        assert tmpl.name == "workflow"
        assert tmpl.description == ""
        assert tmpl.phases == []
        assert tmpl.defaults == {}
        assert tmpl.variables == {}
        assert tmpl.context == ""

    def test_with_data(self) -> None:
        tmpl = WorkflowTemplate(
            name="My Workflow",
            description="Does stuff",
            phases=[{"name": "p1", "pattern": "single"}],
            defaults={"timeout": 300},
            variables={"x": VariableSpec(required=True)},
        )
        assert tmpl.name == "My Workflow"
        assert len(tmpl.phases) == 1
        assert tmpl.variables["x"].required


# ---------------------------------------------------------------------------
# Tests: load_workflow_template()
# ---------------------------------------------------------------------------


class TestLoadWorkflowTemplate:
    def test_load_from_generic_subdir(self, tmp_templates_dir: Path) -> None:
        tmpl = load_workflow_template("simple", tmp_templates_dir)
        assert tmpl.name == "Simple: {topic}"
        assert tmpl.description == "A simple 2-phase template"
        assert len(tmpl.phases) == 2
        assert tmpl.phases[0]["name"] == "phase-a"
        assert tmpl.phases[1]["name"] == "phase-b"

    def test_load_variables(self, tmp_templates_dir: Path) -> None:
        tmpl = load_workflow_template("simple", tmp_templates_dir)
        assert "topic" in tmpl.variables
        assert "language" in tmpl.variables
        assert tmpl.variables["topic"].required is True
        assert tmpl.variables["language"].required is False
        assert tmpl.variables["language"].default == "python"

    def test_load_defaults(self, tmp_templates_dir: Path) -> None:
        tmpl = load_workflow_template("simple", tmp_templates_dir)
        assert tmpl.defaults == {"timeout": 120, "pattern": "single"}

    def test_load_no_variables_section(self, tmp_templates_dir: Path) -> None:
        tmpl = load_workflow_template("novars", tmp_templates_dir)
        assert tmpl.variables == {}
        assert len(tmpl.phases) == 1

    def test_file_not_found_raises(self, tmp_templates_dir: Path) -> None:
        with pytest.raises(FileNotFoundError, match="nonexistent"):
            load_workflow_template("nonexistent", tmp_templates_dir)

    def test_file_not_found_message_includes_searched_paths(
        self, tmp_templates_dir: Path
    ) -> None:
        with pytest.raises(FileNotFoundError) as exc_info:
            load_workflow_template("missing_template", tmp_templates_dir)
        assert "missing_template" in str(exc_info.value)
        assert "Searched:" in str(exc_info.value)

    def test_missing_phases_key_raises(self, tmp_path: Path) -> None:
        no_phases_dir = tmp_path / "t"
        no_phases_dir.mkdir()
        (no_phases_dir / "bad.yaml").write_text("name: Bad\ndescription: no phases\n")
        with pytest.raises(ValueError, match="missing a 'phases' key"):
            load_workflow_template("bad", no_phases_dir)

    def test_non_list_phases_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        d.mkdir()
        (d / "bad.yaml").write_text("name: Bad\nphases: not-a-list\n")
        with pytest.raises(ValueError, match="must be a list"):
            load_workflow_template("bad", d)

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "t"
        d.mkdir()
        (d / "bad.yaml").write_text("- item\n- item2\n")
        with pytest.raises(ValueError, match="not a valid YAML mapping"):
            load_workflow_template("bad", d)

    def test_path_traversal_sanitised(self, tmp_templates_dir: Path) -> None:
        """Path traversal attempts are sanitised (directory component stripped)."""
        with pytest.raises(FileNotFoundError):
            load_workflow_template("../etc/passwd", tmp_templates_dir)

    def test_with_required_tags_in_phase(self, tmp_templates_dir: Path) -> None:
        tmpl = load_workflow_template("tagged", tmp_templates_dir)
        assert tmpl.phases[0]["required_tags"] == ["worker", "{task}_specialist"]

    def test_loads_real_tdd_template(self) -> None:
        """The real examples/workflows/generic/tdd.yaml is loadable."""
        real_dir = Path(__file__).parent.parent / "examples" / "workflows"
        if not (real_dir / "generic" / "tdd.yaml").exists():
            pytest.skip("generic/tdd.yaml not present")
        tmpl = load_workflow_template("tdd", real_dir)
        assert len(tmpl.phases) >= 2
        assert "feature" in tmpl.variables
        assert tmpl.variables["feature"].required is True

    def test_loads_real_debate_template(self) -> None:
        """The real examples/workflows/generic/debate.yaml is loadable."""
        real_dir = Path(__file__).parent.parent / "examples" / "workflows"
        if not (real_dir / "generic" / "debate.yaml").exists():
            pytest.skip("generic/debate.yaml not present")
        tmpl = load_workflow_template("debate", real_dir)
        assert len(tmpl.phases) >= 3
        assert "topic" in tmpl.variables

    def test_loads_real_review_template(self) -> None:
        """The real examples/workflows/generic/review.yaml is loadable."""
        real_dir = Path(__file__).parent.parent / "examples" / "workflows"
        if not (real_dir / "generic" / "review.yaml").exists():
            pytest.skip("generic/review.yaml not present")
        tmpl = load_workflow_template("review", real_dir)
        assert len(tmpl.phases) >= 2
        assert "task" in tmpl.variables


# ---------------------------------------------------------------------------
# Tests: render_template()
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def _make_template(self, **kwargs: Any) -> WorkflowTemplate:
        """Helper to create a minimal WorkflowTemplate."""
        defaults = {
            "name": "Test: {topic}",
            "description": "A test template",
            "phases": [
                {
                    "name": "phase-1",
                    "pattern": "single",
                    "context": "Handle {topic} in {language}.",
                },
                {
                    "name": "phase-2",
                    "pattern": "single",
                    "depends_on": ["phase-1"],
                    "context": "Review the {topic} outcome.",
                },
            ],
            "defaults": {"timeout": 200},
            "variables": {
                "topic": VariableSpec(required=True),
                "language": VariableSpec(required=False, default="python"),
            },
        }
        defaults.update(kwargs)
        return WorkflowTemplate(**defaults)

    def test_basic_substitution(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "binary search"})
        assert result["name"] == "Test: binary search"
        assert "binary search" in result["phases"][0]["context"]

    def test_default_variable_used(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "sorting"})
        # language should default to python
        assert "python" in result["phases"][0]["context"]

    def test_caller_overrides_default(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "sorting", "language": "rust"})
        assert "rust" in result["phases"][0]["context"]

    def test_missing_required_variable_raises(self) -> None:
        tmpl = self._make_template()
        with pytest.raises(ValueError, match="Required template variables not provided"):
            render_template(tmpl, {})

    def test_unknown_placeholder_in_template_raises(self) -> None:
        tmpl = WorkflowTemplate(
            name="Test",
            phases=[{"name": "p", "pattern": "single", "context": "Handle {unknown_var}."}],
        )
        with pytest.raises(ValueError, match="unknown_var"):
            render_template(tmpl, {})

    def test_phase_defaults_applied(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "x"})
        assert result["phase_defaults"]["timeout"] == 200

    def test_agent_timeout_override(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "x"}, agent_timeout=600)
        assert result["phase_defaults"]["timeout"] == 600

    def test_priority_applied_to_phases(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "x"}, priority=5)
        for phase in result["phases"]:
            assert phase.get("priority") == 5

    def test_reply_to_attached_to_last_phase(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "x"}, reply_to="director-1")
        last_phase = result["phases"][-1]
        assert last_phase.get("reply_to") == "director-1"

    def test_reply_to_not_on_earlier_phases(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "x"}, reply_to="director-1")
        # reply_to should only be on the last phase
        assert "reply_to" not in result["phases"][0]

    def test_depends_on_preserved(self) -> None:
        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "x"})
        assert result["phases"][1]["depends_on"] == ["phase-1"]

    def test_required_tags_substituted(self) -> None:
        tmpl = WorkflowTemplate(
            name="Tagged",
            phases=[{
                "name": "p",
                "pattern": "single",
                "context": "Do {task}.",
                "required_tags": ["worker", "{task}_expert"],
            }],
            variables={"task": VariableSpec(required=True)},
        )
        result = render_template(tmpl, {"task": "coding"})
        assert result["phases"][0]["required_tags"] == ["worker", "coding_expert"]

    def test_no_variables_template(self) -> None:
        tmpl = WorkflowTemplate(
            name="Static",
            phases=[{"name": "p", "pattern": "single", "context": "No vars here."}],
        )
        result = render_template(tmpl, {})
        assert result["name"] == "Static"
        assert result["phases"][0]["context"] == "No vars here."

    def test_undeclared_variable_accepted(self) -> None:
        """Undeclared variables supplied by caller are accepted (ad-hoc substitution)."""
        tmpl = WorkflowTemplate(
            name="Test {extra}",
            phases=[{"name": "p", "pattern": "single", "context": "Value: {extra}"}],
            variables={},
        )
        result = render_template(tmpl, {"extra": "hello"})
        assert result["name"] == "Test hello"
        assert result["phases"][0]["context"] == "Value: hello"

    def test_result_is_valid_workflow_submit_dict(self) -> None:
        """Rendered result passes WorkflowSubmit.model_validate()."""
        from tmux_orchestrator.web.schemas import WorkflowSubmit  # noqa: PLC0415

        tmpl = self._make_template()
        result = render_template(tmpl, {"topic": "sorting"})
        ws = WorkflowSubmit.model_validate(result)
        assert ws.phases is not None
        assert len(ws.phases) == 2

    def test_template_not_mutated(self) -> None:
        """render_template() does not mutate the original template."""
        tmpl = self._make_template()
        original_phase_0_ctx = tmpl.phases[0]["context"]
        render_template(tmpl, {"topic": "x"})
        assert tmpl.phases[0]["context"] == original_phase_0_ctx

    def test_context_field_rendered(self) -> None:
        tmpl = WorkflowTemplate(
            name="Test",
            context="Global context for {topic}",
            phases=[{"name": "p", "pattern": "single", "context": "Do {topic}."}],
            variables={"topic": VariableSpec(required=True)},
        )
        result = render_template(tmpl, {"topic": "search"})
        assert result["context"] == "Global context for search"

    def test_empty_context_not_in_result(self) -> None:
        tmpl = WorkflowTemplate(
            name="Test",
            phases=[{"name": "p", "pattern": "single", "context": "Do something."}],
        )
        result = render_template(tmpl, {})
        assert "context" not in result or result.get("context") == ""


# ---------------------------------------------------------------------------
# Tests: list_templates()
# ---------------------------------------------------------------------------


class TestListTemplates:
    def test_lists_generic_templates(self, tmp_templates_dir: Path) -> None:
        results = list_templates(tmp_templates_dir)
        names = [r["template"] for r in results]
        assert "simple" in names
        assert "novars" in names
        assert "tagged" in names

    def test_excludes_non_phase_templates(self, tmp_templates_dir: Path) -> None:
        results = list_templates(tmp_templates_dir)
        names = [r["template"] for r in results]
        # old-style.yaml has no phases key
        assert "old-style" not in names

    def test_excludes_malformed_yaml(self, tmp_templates_dir: Path) -> None:
        results = list_templates(tmp_templates_dir)
        names = [r["template"] for r in results]
        assert "malformed" not in names

    def test_descriptor_fields(self, tmp_templates_dir: Path) -> None:
        results = list_templates(tmp_templates_dir)
        simple = next(r for r in results if r["template"] == "simple")
        assert simple["name"] == "Simple: {topic}"
        assert simple["description"] == "A simple 2-phase template"
        assert "topic" in simple["variables"]
        assert "language" in simple["variables"]
        assert "topic" in simple["required_variables"]
        assert "language" not in simple["required_variables"]
        assert "path" in simple

    def test_sorted_alphabetically(self, tmp_templates_dir: Path) -> None:
        results = list_templates(tmp_templates_dir)
        names = [r["template"] for r in results]
        assert names == sorted(names)

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        results = list_templates(tmp_path / "does_not_exist")
        assert results == []

    def test_real_templates_dir(self) -> None:
        """The real examples/workflows/ directory has at least one phase-based template."""
        real_dir = Path(__file__).parent.parent / "examples" / "workflows"
        results = list_templates(real_dir)
        # At least the three generic/ templates we created
        names = [r["template"] for r in results]
        assert len(results) >= 1, f"Expected at least 1 template, got: {names}"


# ---------------------------------------------------------------------------
# Tests: REST endpoint (FastAPI test client)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_task():
    task = MagicMock()
    task.id = "global-task-id-1"
    return task


@pytest.fixture
def mock_orchestrator(mock_task):
    orch = MagicMock()
    orch.submit_task = AsyncMock(return_value=mock_task)
    orch.config = MagicMock()
    orch.config.session_name = "test"
    orch.config.scratchpad_dir = ".orchestrator/scratchpad"
    orch.config.mailbox_dir = ".orchestrator/mailbox"
    orch.config.metrics_enabled = False
    orch.config.workflow_branch_cleanup = False
    orch.config.otlp_endpoint = ""

    wm = MagicMock()
    run = MagicMock()
    run.id = "wf-run-id"
    run.name = "test-workflow"
    run.phases = []
    wm.submit = MagicMock(return_value=run)
    wm.register_phases = MagicMock()
    wm.set_branch_cleanup_fn = MagicMock()
    orch.get_workflow_manager = MagicMock(return_value=wm)

    return orch


@pytest.fixture
def test_client(mock_orchestrator, tmp_templates_dir):
    """Return a FastAPI test client with the mock orchestrator wired."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from tmux_orchestrator.web.app import create_app  # noqa: PLC0415

    hub = MagicMock()
    hub.broadcast = AsyncMock()

    app = create_app(mock_orchestrator, hub, api_key="test-key")

    # Override the templates_dir in the workflows router by rebuilding it.
    # We need to inject the tmp_templates_dir so tests use our fixtures.
    # Re-include the router with the test templates dir.
    from tmux_orchestrator.web.routers.workflows import build_workflows_router  # noqa: PLC0415
    from tmux_orchestrator.application.scratchpad_store import ScratchpadStore  # noqa: PLC0415

    sp = ScratchpadStore()
    test_router = build_workflows_router(
        mock_orchestrator,
        lambda: None,  # no-op auth
        scratchpad=sp,
        templates_dir=tmp_templates_dir,
    )

    # Patch the router's from-template endpoint into the app for test isolation
    # by creating a separate minimal app that only includes the test router.
    from fastapi import FastAPI  # noqa: PLC0415
    mini_app = FastAPI()
    mini_app.include_router(test_router)

    return TestClient(mini_app)


class TestWorkflowFromTemplateEndpoint:
    """Tests for POST /workflows/from-template."""

    def test_submit_simple_template(self, test_client, mock_orchestrator) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={
                "template": "simple",
                "variables": {"topic": "sorting algorithms"},
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["template"] == "simple"
        assert "workflow_id" in data
        assert "task_ids" in data

    def test_submit_with_optional_var_override(self, test_client) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={
                "template": "simple",
                "variables": {"topic": "hash maps", "language": "rust"},
            },
        )
        assert resp.status_code == 200

    def test_unknown_template_returns_404(self, test_client) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={"template": "nonexistent_template_xyz", "variables": {}},
        )
        assert resp.status_code == 404
        assert "nonexistent_template_xyz" in resp.json()["detail"]

    def test_missing_required_variable_returns_422(self, test_client) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={
                "template": "simple",
                "variables": {},  # missing required 'topic'
            },
        )
        assert resp.status_code == 422
        assert "topic" in resp.json()["detail"]

    def test_agent_timeout_override(self, test_client, mock_orchestrator) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={
                "template": "novars",
                "variables": {},
                "agent_timeout": 900,
            },
        )
        assert resp.status_code == 200

    def test_priority_field(self, test_client) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={
                "template": "novars",
                "variables": {},
                "priority": 3,
            },
        )
        assert resp.status_code == 200

    def test_reply_to_field(self, test_client) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={
                "template": "simple",
                "variables": {"topic": "concurrency"},
                "reply_to": "director-1",
            },
        )
        assert resp.status_code == 200

    def test_template_field_in_response(self, test_client) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={"template": "novars", "variables": {}},
        )
        assert resp.status_code == 200
        assert resp.json()["template"] == "novars"

    def test_tasks_submitted_to_orchestrator(
        self, test_client, mock_orchestrator
    ) -> None:
        resp = test_client.post(
            "/workflows/from-template",
            json={
                "template": "simple",
                "variables": {"topic": "heaps"},
            },
        )
        assert resp.status_code == 200
        # The simple template has 2 phases → 2 submit_task calls
        assert mock_orchestrator.submit_task.call_count >= 2


class TestWorkflowTemplatesListEndpoint:
    """Tests for GET /workflows/templates."""

    def test_returns_templates_list(self, test_client) -> None:
        resp = test_client.get("/workflows/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert "templates" in data
        assert isinstance(data["templates"], list)

    def test_templates_include_known_templates(self, test_client) -> None:
        resp = test_client.get("/workflows/templates")
        names = [t["template"] for t in resp.json()["templates"]]
        assert "simple" in names
        assert "novars" in names
        assert "tagged" in names

    def test_template_descriptor_has_required_fields(self, test_client) -> None:
        resp = test_client.get("/workflows/templates")
        templates = resp.json()["templates"]
        assert len(templates) > 0
        for tmpl in templates:
            assert "template" in tmpl
            assert "name" in tmpl
            assert "description" in tmpl
            assert "variables" in tmpl
            assert "required_variables" in tmpl

    def test_non_phase_templates_excluded(self, test_client) -> None:
        resp = test_client.get("/workflows/templates")
        names = [t["template"] for t in resp.json()["templates"]]
        assert "old-style" not in names

    def test_templates_dir_in_response(self, test_client) -> None:
        resp = test_client.get("/workflows/templates")
        assert "templates_dir" in resp.json()


class TestWorkflowFromTemplateSchema:
    """Tests for WorkflowFromTemplateSubmit Pydantic schema."""

    def test_template_required(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        from tmux_orchestrator.web.schemas import WorkflowFromTemplateSubmit  # noqa: PLC0415

        with pytest.raises(ValidationError):
            WorkflowFromTemplateSubmit.model_validate({})  # missing 'template'

    def test_defaults(self) -> None:
        from tmux_orchestrator.web.schemas import WorkflowFromTemplateSubmit  # noqa: PLC0415

        obj = WorkflowFromTemplateSubmit.model_validate({"template": "tdd"})
        assert obj.variables == {}
        assert obj.reply_to is None
        assert obj.agent_timeout is None
        assert obj.priority == 0

    def test_agent_timeout_must_be_positive(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        from tmux_orchestrator.web.schemas import WorkflowFromTemplateSubmit  # noqa: PLC0415

        with pytest.raises(ValidationError):
            WorkflowFromTemplateSubmit.model_validate(
                {"template": "tdd", "agent_timeout": 0}
            )

    def test_all_fields(self) -> None:
        from tmux_orchestrator.web.schemas import WorkflowFromTemplateSubmit  # noqa: PLC0415

        obj = WorkflowFromTemplateSubmit.model_validate({
            "template": "debate",
            "variables": {"topic": "REST vs GraphQL"},
            "reply_to": "director-1",
            "agent_timeout": 600,
            "priority": 2,
        })
        assert obj.template == "debate"
        assert obj.variables == {"topic": "REST vs GraphQL"}
        assert obj.reply_to == "director-1"
        assert obj.agent_timeout == 600
        assert obj.priority == 2
