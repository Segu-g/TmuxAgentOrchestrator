"""Tests for phase-level defaults in WorkflowSubmit (v1.2.23).

Verifies that:
1. WorkflowSubmit.phase_defaults is accepted and stored.
2. apply_phase_defaults() merges phase_defaults into each PhaseSpecModel.
3. Phase-level values always win over phase_defaults (phase-wins-over-defaults).
4. phase_defaults only applies to phases= mode (ignored for tasks= mode).
5. New role docs (planner.md, spec-writer.md, architect.md) exist in agent_plugin/docs/.
6. All workflow YAML templates now have a defaults: section.

Design references:
- Argo Workflows templateDefaults: template-specific values take priority.
  https://argo-workflows.readthedocs.io/en/latest/template-defaults/ (2025)
- GitLab CI default: keyword: job-level settings inherit from top-level default:.
  https://docs.gitlab.com/ci/yaml/ (2025)
- DESIGN.md §10.98 (v1.2.23)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tmux_orchestrator.web.schemas import (
    PhaseSpecModel,
    WorkflowSubmit,
)
from tmux_orchestrator.workflow_defaults import apply_phase_defaults


# ---------------------------------------------------------------------------
# apply_phase_defaults() unit tests
# ---------------------------------------------------------------------------


class TestApplyPhaseDefaults:
    """Unit tests for the apply_phase_defaults() helper."""

    def _make_phase(self, **kwargs: Any) -> dict[str, Any]:
        defaults = {
            "name": "test-phase",
            "pattern": "single",
        }
        defaults.update(kwargs)
        return defaults

    def test_timeout_applied_when_absent(self):
        """phase_defaults timeout fills in phase with no timeout set."""
        phase = self._make_phase()
        result = apply_phase_defaults(phase, {"timeout": 300})
        assert result["timeout"] == 300

    def test_phase_timeout_wins_over_default(self):
        """Phase-level timeout takes priority over phase_defaults."""
        phase = self._make_phase(timeout=600)
        result = apply_phase_defaults(phase, {"timeout": 300})
        assert result["timeout"] == 600

    def test_required_tags_applied_when_absent(self):
        """phase_defaults required_tags fills in phase with no required_tags."""
        phase = self._make_phase()
        result = apply_phase_defaults(phase, {"required_tags": ["gpu"]})
        assert result["required_tags"] == ["gpu"]

    def test_phase_required_tags_wins_over_default(self):
        """Phase-level required_tags takes priority over phase_defaults."""
        phase = self._make_phase(required_tags=["cpu"])
        result = apply_phase_defaults(phase, {"required_tags": ["gpu"]})
        assert result["required_tags"] == ["cpu"]

    def test_context_applied_when_absent(self):
        """phase_defaults context fills in phase with no context set."""
        phase = self._make_phase()
        result = apply_phase_defaults(phase, {"context": "default context"})
        assert result["context"] == "default context"

    def test_empty_phase_defaults_noop(self):
        """Empty phase_defaults dict returns phase unchanged."""
        phase = self._make_phase(timeout=300)
        result = apply_phase_defaults(phase, {})
        assert result["timeout"] == 300

    def test_none_phase_defaults_noop(self):
        """None phase_defaults returns phase unchanged."""
        phase = self._make_phase(timeout=300)
        result = apply_phase_defaults(phase, None)
        assert result["timeout"] == 300

    def test_does_not_mutate_input(self):
        """apply_phase_defaults must not mutate the input phase dict."""
        phase = self._make_phase()
        original = dict(phase)
        apply_phase_defaults(phase, {"timeout": 300})
        assert phase == original

    def test_multiple_defaults_applied(self):
        """All missing fields from phase_defaults are applied."""
        phase = self._make_phase()
        defaults = {"timeout": 300, "required_tags": ["worker"], "context": "ctx"}
        result = apply_phase_defaults(phase, defaults)
        assert result["timeout"] == 300
        assert result["required_tags"] == ["worker"]
        assert result["context"] == "ctx"

    def test_null_value_in_phase_preserved(self):
        """Explicit None in phase is kept (not overwritten by default)."""
        phase = self._make_phase(timeout=None)
        result = apply_phase_defaults(phase, {"timeout": 300})
        assert result["timeout"] is None

    def test_zero_timeout_in_phase_preserved(self):
        """Explicit 0 in phase is kept (falsy but present)."""
        phase = self._make_phase(timeout=0)
        result = apply_phase_defaults(phase, {"timeout": 300})
        assert result["timeout"] == 0

    def test_extra_keys_in_defaults_are_passed_through(self):
        """Keys in phase_defaults that phase does not have are added."""
        phase = self._make_phase()
        result = apply_phase_defaults(phase, {"unknown_field": "value"})
        assert result["unknown_field"] == "value"


# ---------------------------------------------------------------------------
# WorkflowSubmit.phase_defaults field
# ---------------------------------------------------------------------------


class TestWorkflowSubmitPhaseDefaults:
    """Verify WorkflowSubmit accepts and stores phase_defaults."""

    def _minimal_phase(self) -> dict:
        return {"name": "p", "pattern": "single"}

    def test_phase_defaults_field_exists(self):
        """WorkflowSubmit must accept a phase_defaults field."""
        body = WorkflowSubmit(
            phases=[self._minimal_phase()],
            phase_defaults={"timeout": 300},
        )
        assert body.phase_defaults == {"timeout": 300}

    def test_phase_defaults_default_is_none(self):
        """phase_defaults defaults to None when not provided."""
        body = WorkflowSubmit(
            phases=[self._minimal_phase()],
        )
        assert body.phase_defaults is None

    def test_phase_defaults_empty_dict_accepted(self):
        """phase_defaults can be an empty dict."""
        body = WorkflowSubmit(
            phases=[self._minimal_phase()],
            phase_defaults={},
        )
        assert body.phase_defaults == {}

    def test_phase_defaults_with_timeout_and_tags(self):
        """phase_defaults can contain timeout and required_tags."""
        body = WorkflowSubmit(
            phases=[self._minimal_phase()],
            phase_defaults={"timeout": 600, "required_tags": ["worker"]},
        )
        assert body.phase_defaults["timeout"] == 600
        assert body.phase_defaults["required_tags"] == ["worker"]

    def test_phase_defaults_ignored_for_tasks_mode(self):
        """phase_defaults is accepted but has no effect in tasks= mode."""
        from tmux_orchestrator.web.schemas import WorkflowTaskSpec
        body = WorkflowSubmit(
            tasks=[WorkflowTaskSpec(local_id="t1", prompt="do it")],
            phase_defaults={"timeout": 300},
        )
        assert body.phase_defaults == {"timeout": 300}
        assert body.tasks is not None


# ---------------------------------------------------------------------------
# Integration: phase_defaults applied to phases before conversion
# ---------------------------------------------------------------------------


class TestPhaseDefaultsAppliedToPhases:
    """Verify that phase_defaults are merged into each phase spec."""

    def _submit_with_defaults(
        self,
        phases: list[dict],
        phase_defaults: dict | None,
    ) -> WorkflowSubmit:
        return WorkflowSubmit(
            phases=phases,
            phase_defaults=phase_defaults,
        )

    def test_timeout_default_fills_all_phases(self):
        """phase_defaults timeout must appear in all phases that lack explicit timeout."""
        body = self._submit_with_defaults(
            phases=[
                {"name": "phase-1", "pattern": "single"},
                {"name": "phase-2", "pattern": "parallel"},
            ],
            phase_defaults={"timeout": 300},
        )
        # Retrieve the effective phases (with defaults applied)
        effective = body.effective_phases()
        assert all(p.get("timeout") == 300 for p in effective)

    def test_explicit_phase_timeout_overrides_default(self):
        """Phase with explicit timeout keeps its value."""
        body = self._submit_with_defaults(
            phases=[
                {"name": "phase-1", "pattern": "single", "timeout": 600},
                {"name": "phase-2", "pattern": "single"},
            ],
            phase_defaults={"timeout": 300},
        )
        effective = body.effective_phases()
        assert effective[0]["timeout"] == 600
        assert effective[1]["timeout"] == 300

    def test_no_phase_defaults_returns_phases_unchanged(self):
        """Without phase_defaults, effective_phases() returns phases as-is."""
        body = self._submit_with_defaults(
            phases=[
                {"name": "p", "pattern": "single", "timeout": 100},
            ],
            phase_defaults=None,
        )
        effective = body.effective_phases()
        assert effective[0]["timeout"] == 100

    def test_effective_phases_returns_list(self):
        """effective_phases() always returns a list."""
        body = WorkflowSubmit(
            phases=[{"name": "p", "pattern": "single"}],
        )
        assert isinstance(body.effective_phases(), list)

    def test_required_tags_default_applied_to_all_phases(self):
        """phase_defaults required_tags fills in phases that have none."""
        body = self._submit_with_defaults(
            phases=[
                {"name": "p1", "pattern": "single"},
                {"name": "p2", "pattern": "single", "required_tags": ["gpu"]},
            ],
            phase_defaults={"required_tags": ["worker"]},
        )
        effective = body.effective_phases()
        assert effective[0]["required_tags"] == ["worker"]
        assert effective[1]["required_tags"] == ["gpu"]


# ---------------------------------------------------------------------------
# New role docs: planner.md, spec-writer.md, architect.md
# ---------------------------------------------------------------------------


class TestNewRoleDocs:
    """Verify the new role documentation files exist with correct content."""

    DOCS_DIR = (
        Path(__file__).parent.parent
        / "src"
        / "tmux_orchestrator"
        / "agent_plugin"
        / "docs"
    )

    def test_planner_md_exists(self):
        """planner.md must exist in agent_plugin/docs/."""
        assert (self.DOCS_DIR / "planner.md").is_file()

    def test_spec_writer_md_exists(self):
        """spec-writer.md must exist in agent_plugin/docs/."""
        assert (self.DOCS_DIR / "spec-writer.md").is_file()

    def test_architect_md_exists(self):
        """architect.md must exist in agent_plugin/docs/."""
        assert (self.DOCS_DIR / "architect.md").is_file()

    def test_planner_md_mentions_plan(self):
        """planner.md must mention PLAN.md or planning."""
        content = (self.DOCS_DIR / "planner.md").read_text(encoding="utf-8")
        assert "plan" in content.lower()

    def test_spec_writer_md_mentions_spec(self):
        """spec-writer.md must mention specification or spec."""
        content = (self.DOCS_DIR / "spec-writer.md").read_text(encoding="utf-8")
        assert "spec" in content.lower()

    def test_architect_md_mentions_architecture(self):
        """architect.md must mention architecture or design."""
        content = (self.DOCS_DIR / "architect.md").read_text(encoding="utf-8")
        assert any(word in content.lower() for word in ["architect", "design", "layer"])

    def test_all_existing_role_docs_present(self):
        """All 8 role docs (including 5 existing + 3 new) must be present."""
        expected = {
            "worker.md",
            "director.md",
            "tester.md",
            "coder.md",
            "reviewer.md",
            "planner.md",
            "spec-writer.md",
            "architect.md",
        }
        found = {f.name for f in self.DOCS_DIR.glob("*.md")}
        missing = expected - found
        assert not missing, f"Missing role docs: {missing}"


# ---------------------------------------------------------------------------
# Workflow YAML templates: all must have defaults: sections
# ---------------------------------------------------------------------------


class TestWorkflowYamlDefaults:
    """Verify all workflow YAML templates have a defaults: section."""

    TEMPLATES_DIR = Path(__file__).parent.parent / "examples" / "workflows"

    def _get_yaml_files(self) -> list[Path]:
        return sorted(self.TEMPLATES_DIR.glob("*.yaml"))

    def test_all_templates_have_defaults_section(self):
        """Every *.yaml in examples/workflows/ must contain a 'defaults:' key."""
        import yaml

        files_without_defaults = []
        for path in self._get_yaml_files():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict) or "defaults" not in data:
                files_without_defaults.append(path.name)
        assert not files_without_defaults, (
            f"These workflow templates are missing a 'defaults:' section: "
            f"{files_without_defaults}"
        )

    def test_template_count_is_at_least_15(self):
        """There must be at least 15 workflow YAML templates."""
        assert len(self._get_yaml_files()) >= 15
