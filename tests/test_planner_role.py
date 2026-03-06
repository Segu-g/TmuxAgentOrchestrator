"""Tests for Planner agent role template and /plan-workflow command.

Design references:
- §12「ワークフロー設計の層構造」層1 自律モード
- arXiv:2502.07056 (HTDAG 2025): Planner-Executor pattern
- arXiv:2507.14447 (Routine 2025): Structural planning framework
- DESIGN.md §10.15 (v0.49.0)
"""

from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Planner role template
# ---------------------------------------------------------------------------

PLANNER_ROLE_PATH = Path(__file__).parent.parent / ".claude" / "prompts" / "roles" / "planner.md"
PLAN_WORKFLOW_CMD_PATH = Path(__file__).parent.parent / ".claude" / "commands" / "plan-workflow.md"


def test_planner_role_exists():
    """planner.md role template file must exist."""
    assert PLANNER_ROLE_PATH.exists(), f"Missing: {PLANNER_ROLE_PATH}"


def test_planner_role_has_required_sections():
    """planner.md must document the JSON output format and all four patterns."""
    content = PLANNER_ROLE_PATH.read_text()
    assert "single" in content
    assert "parallel" in content
    assert "competitive" in content
    assert "debate" in content


def test_planner_role_output_format():
    """planner.md must describe the phases JSON schema."""
    content = PLANNER_ROLE_PATH.read_text()
    assert '"phases"' in content
    assert '"pattern"' in content
    assert '"name"' in content


def test_planner_role_has_prohibited_behaviours():
    """planner.md must document prohibited behaviours (reduces sycophancy)."""
    content = PLANNER_ROLE_PATH.read_text()
    assert "Prohibited" in content or "prohibited" in content


def test_planner_role_has_design_references():
    """planner.md must include research references."""
    content = PLANNER_ROLE_PATH.read_text()
    assert "arXiv" in content or "Design Reference" in content


# ---------------------------------------------------------------------------
# /plan-workflow command
# ---------------------------------------------------------------------------


def test_plan_workflow_command_exists():
    """/plan-workflow command file must exist."""
    assert PLAN_WORKFLOW_CMD_PATH.exists(), f"Missing: {PLAN_WORKFLOW_CMD_PATH}"


def test_plan_workflow_command_uses_context_json():
    """plan-workflow.md must read __orchestrator_context__.json."""
    content = PLAN_WORKFLOW_CMD_PATH.read_text()
    assert "__orchestrator_context__.json" in content


def test_plan_workflow_command_references_rest_endpoint():
    """plan-workflow.md must reference POST /workflows."""
    content = PLAN_WORKFLOW_CMD_PATH.read_text()
    assert "/workflows" in content


def test_plan_workflow_command_submit_mode():
    """plan-workflow.md must document --submit mode."""
    content = PLAN_WORKFLOW_CMD_PATH.read_text()
    assert "--submit" in content


def test_plan_workflow_command_workflow_plan_json():
    """plan-workflow.md must reference WORKFLOW_PLAN.json."""
    content = PLAN_WORKFLOW_CMD_PATH.read_text()
    assert "WORKFLOW_PLAN.json" in content


# ---------------------------------------------------------------------------
# PhaseSpec <-> WorkflowSubmit integration
# ---------------------------------------------------------------------------


def test_phase_spec_model_in_workflow_submit():
    """PhaseSpecModel and WorkflowSubmit are importable from web.app."""
    from tmux_orchestrator.web.app import PhaseSpecModel, WorkflowSubmit, AgentSelectorModel

    # Build a phases-based submission
    body = WorkflowSubmit(
        name="test",
        context="Build something",
        phases=[
            PhaseSpecModel(
                name="design",
                pattern="debate",
                agents=AgentSelectorModel(tags=["advocate"]),
                critic_agents=AgentSelectorModel(tags=["critic"]),
                judge_agents=AgentSelectorModel(tags=["judge"]),
                debate_rounds=1,
            ),
            PhaseSpecModel(name="implement", pattern="single"),
        ],
    )
    assert body.phases is not None
    assert len(body.phases) == 2
    assert body.phases[0].pattern == "debate"
    assert body.phases[1].pattern == "single"


def test_workflow_submit_neither_raises():
    """WorkflowSubmit with neither tasks nor phases raises ValidationError."""
    from pydantic import ValidationError
    from tmux_orchestrator.web.app import WorkflowSubmit

    with pytest.raises(ValidationError):
        WorkflowSubmit(name="empty")


def test_workflow_submit_both_raises():
    """WorkflowSubmit with both tasks and phases raises ValidationError."""
    from pydantic import ValidationError
    from tmux_orchestrator.web.app import WorkflowSubmit, WorkflowTaskSpec, PhaseSpecModel

    with pytest.raises(ValidationError):
        WorkflowSubmit(
            name="conflict",
            tasks=[WorkflowTaskSpec(local_id="t1", prompt="do it")],
            phases=[PhaseSpecModel(name="x", pattern="single")],
        )


import pytest
