"""Tests for PhaseExecutor — translates PhaseSpec into WorkflowTaskSpec lists.

Design references:
- §12「ワークフロー設計の層構造」層1・2・3
- arXiv:2512.19769 (PayPal DSL): declarative pattern → task expansion
- arXiv:2502.07056 (HTDAG): hierarchical phase decomposition
- DESIGN.md §10.15 (v0.48.0)
"""

from __future__ import annotations

import pytest

from tmux_orchestrator.phase_executor import (
    AgentSelector,
    PhaseSpec,
    WorkflowPhaseStatus,
    expand_phases,
)


# ---------------------------------------------------------------------------
# AgentSelector
# ---------------------------------------------------------------------------


def test_agent_selector_defaults():
    sel = AgentSelector()
    assert sel.tags == []
    assert sel.count == 1
    assert sel.target_agent is None
    assert sel.target_group is None


def test_agent_selector_custom():
    sel = AgentSelector(tags=["implementer"], count=3, target_group="workers")
    assert sel.tags == ["implementer"]
    assert sel.count == 3
    assert sel.target_group == "workers"


# ---------------------------------------------------------------------------
# PhaseSpec
# ---------------------------------------------------------------------------


def test_phase_spec_single_defaults():
    spec = PhaseSpec(name="implement", pattern="single")
    assert spec.pattern == "single"
    assert spec.context is None
    assert spec.required_tags == []


def test_phase_spec_parallel():
    spec = PhaseSpec(name="review", pattern="parallel", agents=AgentSelector(count=3))
    assert spec.agents.count == 3


def test_phase_spec_invalid_pattern():
    with pytest.raises(Exception):
        PhaseSpec(name="bad", pattern="invalid_pattern")


def test_phase_spec_debate():
    spec = PhaseSpec(
        name="design",
        pattern="debate",
        agents=AgentSelector(tags=["advocate"]),
        critic_agents=AgentSelector(tags=["critic"]),
        judge_agents=AgentSelector(tags=["judge"]),
    )
    assert spec.pattern == "debate"
    assert spec.agents.tags == ["advocate"]
    assert spec.critic_agents.tags == ["critic"]
    assert spec.judge_agents.tags == ["judge"]


def test_phase_spec_competitive():
    spec = PhaseSpec(
        name="solve",
        pattern="competitive",
        agents=AgentSelector(count=3, tags=["solver"]),
    )
    assert spec.agents.count == 3


# ---------------------------------------------------------------------------
# expand_phases — single pattern
# ---------------------------------------------------------------------------


def test_expand_single_phase():
    phases = [PhaseSpec(name="implement", pattern="single")]
    tasks = expand_phases(phases, context="Build a function", scratchpad_prefix="wf")
    assert len(tasks) == 1
    t = tasks[0]
    assert t["local_id"] == "phase_implement_0"
    assert "implement" in t["prompt"].lower() or "Build a function" in t["prompt"]
    assert t["depends_on"] == []
    assert t["required_tags"] == []


def test_expand_single_with_tags():
    phases = [PhaseSpec(name="implement", pattern="single", agents=AgentSelector(tags=["coder"]))]
    tasks = expand_phases(phases, context="Build X", scratchpad_prefix="wf")
    assert tasks[0]["required_tags"] == ["coder"]


def test_expand_single_chain():
    phases = [
        PhaseSpec(name="design", pattern="single"),
        PhaseSpec(name="implement", pattern="single"),
        PhaseSpec(name="review", pattern="single"),
    ]
    tasks = expand_phases(phases, context="Build a system", scratchpad_prefix="wf")
    assert len(tasks) == 3
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == ["phase_design_0"]
    assert tasks[2]["depends_on"] == ["phase_implement_0"]


# ---------------------------------------------------------------------------
# expand_phases — parallel pattern
# ---------------------------------------------------------------------------


def test_expand_parallel_phase_creates_n_tasks():
    phases = [PhaseSpec(name="review", pattern="parallel", agents=AgentSelector(count=3))]
    tasks = expand_phases(phases, context="Review this", scratchpad_prefix="wf")
    assert len(tasks) == 3
    for i, t in enumerate(tasks):
        assert t["local_id"] == f"phase_review_{i}"
        assert t["depends_on"] == []


def test_expand_parallel_then_single():
    phases = [
        PhaseSpec(name="review", pattern="parallel", agents=AgentSelector(count=2)),
        PhaseSpec(name="merge", pattern="single"),
    ]
    tasks = expand_phases(phases, context="ctx", scratchpad_prefix="wf")
    assert len(tasks) == 3  # 2 parallel + 1 merge
    parallel_ids = [t["local_id"] for t in tasks if "review" in t["local_id"]]
    assert len(parallel_ids) == 2
    merge_task = next(t for t in tasks if "merge" in t["local_id"])
    # merge depends on both parallel tasks
    assert set(merge_task["depends_on"]) == set(parallel_ids)


# ---------------------------------------------------------------------------
# expand_phases — competitive pattern
# ---------------------------------------------------------------------------


def test_expand_competitive_phase_creates_n_tasks():
    phases = [PhaseSpec(name="solve", pattern="competitive", agents=AgentSelector(count=3))]
    tasks = expand_phases(phases, context="Solve X", scratchpad_prefix="wf")
    assert len(tasks) == 3
    for t in tasks:
        assert t["required_tags"] == []


def test_expand_competitive_with_tags():
    phases = [PhaseSpec(name="solve", pattern="competitive", agents=AgentSelector(count=2, tags=["solver"]))]
    tasks = expand_phases(phases, context="Solve X", scratchpad_prefix="wf")
    for t in tasks:
        assert t["required_tags"] == ["solver"]


# ---------------------------------------------------------------------------
# expand_phases — debate pattern
# ---------------------------------------------------------------------------


def test_expand_debate_phase():
    phases = [
        PhaseSpec(
            name="design",
            pattern="debate",
            agents=AgentSelector(tags=["advocate"]),
            critic_agents=AgentSelector(tags=["critic"]),
            judge_agents=AgentSelector(tags=["judge"]),
            debate_rounds=1,
        )
    ]
    tasks = expand_phases(phases, context="Should we use microservices?", scratchpad_prefix="wf")
    # 1 round = advocate + critic; final = judge → total 3
    assert len(tasks) == 3
    local_ids = [t["local_id"] for t in tasks]
    assert any("advocate" in lid for lid in local_ids)
    assert any("critic" in lid for lid in local_ids)
    assert any("judge" in lid for lid in local_ids)


def test_expand_debate_two_rounds():
    phases = [
        PhaseSpec(
            name="debate",
            pattern="debate",
            agents=AgentSelector(tags=["advocate"]),
            critic_agents=AgentSelector(tags=["critic"]),
            judge_agents=AgentSelector(tags=["judge"]),
            debate_rounds=2,
        )
    ]
    tasks = expand_phases(phases, context="Topic", scratchpad_prefix="wf")
    # 2 rounds × 2 (advocate + critic) + 1 judge = 5
    assert len(tasks) == 5


# ---------------------------------------------------------------------------
# expand_phases — per-phase context override
# ---------------------------------------------------------------------------


def test_phase_context_override():
    phases = [
        PhaseSpec(name="design", pattern="single", context="Design the API"),
        PhaseSpec(name="implement", pattern="single"),
    ]
    tasks = expand_phases(phases, context="Global context", scratchpad_prefix="wf")
    assert "Design the API" in tasks[0]["prompt"]
    assert "Global context" in tasks[1]["prompt"]


# ---------------------------------------------------------------------------
# WorkflowPhaseStatus
# ---------------------------------------------------------------------------


def test_workflow_phase_status_defaults():
    ps = WorkflowPhaseStatus(name="design", pattern="single", task_ids=["t1"])
    assert ps.status == "pending"
    assert ps.started_at is None
    assert ps.completed_at is None


def test_workflow_phase_status_to_dict():
    ps = WorkflowPhaseStatus(name="design", pattern="single", task_ids=["t1", "t2"])
    d = ps.to_dict()
    assert d["name"] == "design"
    assert d["pattern"] == "single"
    assert d["task_ids"] == ["t1", "t2"]
    assert d["status"] == "pending"


def test_workflow_phase_status_mark_running():
    ps = WorkflowPhaseStatus(name="design", pattern="single", task_ids=["t1"])
    ps.mark_running()
    assert ps.status == "running"
    assert ps.started_at is not None


def test_workflow_phase_status_mark_complete():
    ps = WorkflowPhaseStatus(name="design", pattern="single", task_ids=["t1"])
    ps.mark_complete()
    assert ps.status == "complete"
    assert ps.completed_at is not None


def test_workflow_phase_status_mark_failed():
    ps = WorkflowPhaseStatus(name="design", pattern="single", task_ids=["t1"])
    ps.mark_failed()
    assert ps.status == "failed"
    assert ps.completed_at is not None
