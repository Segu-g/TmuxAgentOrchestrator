"""Tests for web/schemas.py — extracted Pydantic schema module (v1.1.5).

Verifies that:
1. All schemas are importable from ``web/schemas.py``.
2. All schemas are re-exported from ``web/app.py`` for backward compatibility.
3. Validators still work correctly in the new module.
4. No regression in schema field shapes.

Design reference: DESIGN.md §10.41 (v1.1.5).
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Import tests — schemas must be importable from both locations
# ---------------------------------------------------------------------------


def test_schemas_importable_from_web_schemas():
    """All schemas can be imported from web/schemas.py."""
    from tmux_orchestrator.web.schemas import (
        AgentKillResponse,
        AgentSelectorModel,
        AdrWorkflowSubmit,
        AutoScalerUpdate,
        ChangeStrategyRequest,
        CleanArchWorkflowSubmit,
        CompetitionWorkflowSubmit,
        DDDWorkflowSubmit,
        DebateWorkflowSubmit,
        DelphiWorkflowSubmit,
        DirectorChat,
        DynamicAgentCreate,
        FulldevWorkflowSubmit,
        GroupAddAgent,
        GroupCreate,
        PairWorkflowSubmit,
        PhaseSpecModel,
        RateLimitUpdate,
        RedBlueWorkflowSubmit,
        ScratchpadWrite,
        SendMessage,
        SocraticWorkflowSubmit,
        SpawnAgent,
        TaskBatchItem,
        TaskBatchSubmit,
        TaskCompleteBody,
        TaskPriorityUpdate,
        TaskSubmit,
        TddWorkflowSubmit,
        WebhookCreate,
        WorkflowSubmit,
        WorkflowTaskSpec,
    )
    # Spot-check a few
    assert TaskSubmit is not None
    assert WorkflowSubmit is not None
    assert CompetitionWorkflowSubmit is not None


def test_schemas_re_exported_from_app():
    """All schemas are re-exported from web/app.py for backward compatibility."""
    from tmux_orchestrator.web.app import (
        AgentKillResponse,
        ChangeStrategyRequest,
        CompetitionWorkflowSubmit,
        DDDWorkflowSubmit,
        TaskSubmit,
        WorkflowSubmit,
    )
    assert TaskSubmit is not None
    assert WorkflowSubmit is not None


# ---------------------------------------------------------------------------
# Validator tests — ensure validators still work in the new module
# ---------------------------------------------------------------------------


def test_task_submit_default_fields():
    from tmux_orchestrator.web.schemas import TaskSubmit
    t = TaskSubmit(prompt="hello")
    assert t.prompt == "hello"
    assert t.priority == 0
    assert t.required_tags == []
    assert t.inherit_priority is True
    assert t.ttl is None


def test_change_strategy_request_valid_pattern():
    from tmux_orchestrator.web.schemas import ChangeStrategyRequest
    r = ChangeStrategyRequest(pattern="parallel", count=3)
    assert r.pattern == "parallel"
    assert r.count == 3


def test_change_strategy_request_invalid_pattern_raises():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import ChangeStrategyRequest
    with pytest.raises(ValidationError, match="pattern must be one of"):
        ChangeStrategyRequest(pattern="debate")  # debate not yet valid for this schema


def test_change_strategy_request_count_too_high_raises():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import ChangeStrategyRequest
    with pytest.raises(ValidationError, match="count must be <= 10"):
        ChangeStrategyRequest(pattern="parallel", count=11)


def test_workflow_submit_requires_tasks_or_phases():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import WorkflowSubmit
    with pytest.raises(ValidationError, match="Either 'tasks' or 'phases' must be provided"):
        WorkflowSubmit(name="test")


def test_workflow_submit_with_tasks():
    from tmux_orchestrator.web.schemas import WorkflowSubmit, WorkflowTaskSpec
    spec = WorkflowTaskSpec(local_id="t1", prompt="do thing")
    w = WorkflowSubmit(name="test-wf", tasks=[spec])
    assert len(w.tasks) == 1  # type: ignore[arg-type]
    assert w.tasks[0].local_id == "t1"  # type: ignore[index]


def test_delphi_submit_experts_validation():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import DelphiWorkflowSubmit
    # Only 1 expert — should fail
    with pytest.raises(ValidationError, match="at least 2 personas"):
        DelphiWorkflowSubmit(topic="test", experts=["security"])


def test_delphi_submit_duplicate_experts_raises():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import DelphiWorkflowSubmit
    with pytest.raises(ValidationError, match="unique"):
        DelphiWorkflowSubmit(topic="test", experts=["security", "security"])


def test_competition_submit_valid():
    from tmux_orchestrator.web.schemas import CompetitionWorkflowSubmit
    c = CompetitionWorkflowSubmit(
        problem="solve X",
        strategies=["greedy", "dp"],
    )
    assert c.problem == "solve X"
    assert len(c.strategies) == 2


def test_competition_submit_too_few_strategies_raises():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import CompetitionWorkflowSubmit
    with pytest.raises(ValidationError, match="at least 2"):
        CompetitionWorkflowSubmit(problem="X", strategies=["only-one"])


def test_ddd_submit_blank_context_raises():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import DDDWorkflowSubmit
    with pytest.raises(ValidationError, match="context names must not be blank"):
        DDDWorkflowSubmit(topic="test", contexts=["Orders", ""])


def test_phase_spec_model_invalid_pattern_raises():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import PhaseSpecModel
    with pytest.raises(ValidationError, match="pattern must be one of"):
        PhaseSpecModel(name="phase1", pattern="invalid_pattern")


def test_phase_spec_model_valid_patterns():
    from tmux_orchestrator.web.schemas import PhaseSpecModel
    for pattern in ("single", "parallel", "competitive", "debate"):
        m = PhaseSpecModel(name=f"p-{pattern}", pattern=pattern)
        assert m.pattern == pattern


def test_tdd_workflow_submit_empty_feature_raises():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import TddWorkflowSubmit
    with pytest.raises(ValidationError, match="feature must not be empty"):
        TddWorkflowSubmit(feature="   ")


def test_debate_workflow_submit_max_rounds_out_of_range():
    from pydantic import ValidationError
    from tmux_orchestrator.web.schemas import DebateWorkflowSubmit
    with pytest.raises(ValidationError, match="max_rounds must be between"):
        DebateWorkflowSubmit(topic="test", max_rounds=5)


def test_dynamic_agent_create_defaults():
    from tmux_orchestrator.web.schemas import DynamicAgentCreate
    d = DynamicAgentCreate()
    assert d.isolate is True
    assert d.role == "worker"
    assert d.merge_on_stop is False


def test_scratchpad_write_accepts_any_value():
    from tmux_orchestrator.web.schemas import ScratchpadWrite
    # Strings
    s = ScratchpadWrite(value="hello")
    assert s.value == "hello"
    # Nested dicts
    s2 = ScratchpadWrite(value={"nested": [1, 2, 3]})
    assert s2.value == {"nested": [1, 2, 3]}


def test_task_batch_submit_with_items():
    from tmux_orchestrator.web.schemas import TaskBatchItem, TaskBatchSubmit
    items = [
        TaskBatchItem(prompt="task 1"),
        TaskBatchItem(local_id="t2", prompt="task 2", depends_on=["t1"]),
    ]
    batch = TaskBatchSubmit(tasks=items)
    assert len(batch.tasks) == 2
    assert batch.tasks[1].local_id == "t2"
