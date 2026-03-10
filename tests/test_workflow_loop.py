"""Tests for loop workflow support (LoopBlock, LoopSpec, {iter} substitution).

Design reference: DESIGN.md §10.76 (v1.1.44)
"""

from __future__ import annotations

import pytest

from tmux_orchestrator.domain.phase_strategy import (
    AgentSelector,
    LoopBlock,
    LoopSpec,
    PhaseSpec,
    SkipCondition,
    WorkflowPhaseStatus,
)
from tmux_orchestrator.phase_executor import (
    _inject_header_into_phase,
    _iter_prefix_header,
    _substitute_iter,
    _substitute_iter_in_phase,
    expand_loop_iter,
    expand_phase_items_with_status,
    expand_phases_with_status,
    is_until_condition_met,
)


# ---------------------------------------------------------------------------
# LoopSpec / LoopBlock dataclass tests
# ---------------------------------------------------------------------------


class TestLoopSpec:
    def test_default_values(self) -> None:
        ls = LoopSpec()
        assert ls.max == 5
        assert ls.until is None

    def test_custom_values(self) -> None:
        cond = SkipCondition(key="done", value="yes")
        ls = LoopSpec(max=3, until=cond)
        assert ls.max == 3
        assert ls.until is cond

    def test_max_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="max must be >= 1"):
            LoopSpec(max=0)

    def test_max_one_is_valid(self) -> None:
        ls = LoopSpec(max=1)
        assert ls.max == 1


class TestLoopBlock:
    def test_basic_construction(self) -> None:
        phase = PhaseSpec(name="p1", pattern="single")
        lb = LoopBlock(name="my_loop", loop=LoopSpec(max=2), phases=[phase])
        assert lb.name == "my_loop"
        assert lb.loop.max == 2
        assert len(lb.phases) == 1

    def test_empty_phases(self) -> None:
        lb = LoopBlock(name="empty", loop=LoopSpec(max=1), phases=[])
        assert lb.phases == []


# ---------------------------------------------------------------------------
# {iter} substitution tests
# ---------------------------------------------------------------------------


class TestSubstituteIter:
    def test_replaces_placeholder(self) -> None:
        assert _substitute_iter("plan_iter{iter}", 1) == "plan_iter1"
        assert _substitute_iter("plan_iter{iter}", 3) == "plan_iter3"

    def test_no_placeholder(self) -> None:
        assert _substitute_iter("no_placeholder", 2) == "no_placeholder"

    def test_multiple_occurrences(self) -> None:
        assert _substitute_iter("{iter}_{iter}", 2) == "2_2"


class TestSubstituteIterInPhase:
    def test_name_substitution(self) -> None:
        phase = PhaseSpec(name="plan_iter{iter}", pattern="single")
        result = _substitute_iter_in_phase(phase, 2)
        assert result.name == "plan_iter2"
        # Original unchanged
        assert phase.name == "plan_iter{iter}"

    def test_context_substitution(self) -> None:
        phase = PhaseSpec(name="p", pattern="single", context="step {iter}")
        result = _substitute_iter_in_phase(phase, 3)
        assert result.context == "step 3"

    def test_none_context_stays_none(self) -> None:
        phase = PhaseSpec(name="p", pattern="single", context=None)
        result = _substitute_iter_in_phase(phase, 1)
        assert result.context is None

    def test_other_fields_preserved(self) -> None:
        phase = PhaseSpec(
            name="p{iter}",
            pattern="single",
            required_tags=["foo"],
            timeout=60,
        )
        result = _substitute_iter_in_phase(phase, 1)
        assert result.required_tags == ["foo"]
        assert result.timeout == 60


# ---------------------------------------------------------------------------
# _iter_prefix_header tests
# ---------------------------------------------------------------------------


class TestIterPrefixHeader:
    def test_iter1_no_prev_keys_empty(self) -> None:
        h = _iter_prefix_header(1, 4, [])
        assert h == ""

    def test_iter2_no_prev_keys(self) -> None:
        h = _iter_prefix_header(2, 4, [])
        assert "2/4" in h

    def test_iter1_with_prev_keys(self) -> None:
        h = _iter_prefix_header(1, 3, ["k1", "k2"])
        assert "k1" in h
        assert "k2" in h
        assert "1/3" in h

    def test_iter2_with_prev_keys(self) -> None:
        h = _iter_prefix_header(2, 3, ["plan_iter1"])
        assert "plan_iter1" in h
        assert "2/3" in h


class TestInjectHeaderIntoPhase:
    def test_empty_header_no_change(self) -> None:
        phase = PhaseSpec(name="p", pattern="single", context="existing")
        result = _inject_header_into_phase(phase, "")
        assert result.context == "existing"

    def test_header_prepended_to_context(self) -> None:
        phase = PhaseSpec(name="p", pattern="single", context="body")
        result = _inject_header_into_phase(phase, "[header]\n\n")
        assert result.context is not None
        assert result.context.startswith("[header]")
        assert "body" in result.context

    def test_header_with_none_context(self) -> None:
        phase = PhaseSpec(name="p", pattern="single", context=None)
        result = _inject_header_into_phase(phase, "[hdr]")
        assert result.context is not None
        assert "[hdr]" in result.context


# ---------------------------------------------------------------------------
# expand_loop_iter tests
# ---------------------------------------------------------------------------


class TestExpandLoopIter:
    def _make_single_phase(self, name: str) -> PhaseSpec:
        return PhaseSpec(name=name, pattern="single")

    def test_iter1_generates_tasks(self) -> None:
        lb = LoopBlock(
            name="my_loop",
            loop=LoopSpec(max=3),
            phases=[self._make_single_phase("plan")],
        )
        tasks, statuses, terminals = expand_loop_iter(
            lb, 1, context="ctx", scratchpad_prefix="sp"
        )
        assert len(tasks) == 1
        assert len(statuses) == 1
        assert len(terminals) == 1

    def test_iter_substitution_in_task_name(self) -> None:
        phase = PhaseSpec(name="plan_iter{iter}", pattern="single")
        lb = LoopBlock(name="loop", loop=LoopSpec(max=2), phases=[phase])
        tasks, _, _ = expand_loop_iter(lb, 2, context="ctx", scratchpad_prefix="")
        # The task local_id should contain the substituted name
        assert "plan_iter2" in tasks[0]["local_id"]

    def test_multiple_phases_in_loop_body(self) -> None:
        phases = [
            PhaseSpec(name="plan", pattern="single"),
            PhaseSpec(name="do", pattern="single"),
            PhaseSpec(name="check", pattern="single"),
        ]
        lb = LoopBlock(name="loop", loop=LoopSpec(max=1), phases=phases)
        tasks, statuses, terminals = expand_loop_iter(
            lb, 1, context="c", scratchpad_prefix=""
        )
        assert len(tasks) == 3
        assert len(statuses) == 3

    def test_dependency_chain_within_iteration(self) -> None:
        phases = [
            PhaseSpec(name="plan", pattern="single"),
            PhaseSpec(name="do", pattern="single"),
        ]
        lb = LoopBlock(name="loop", loop=LoopSpec(max=1), phases=phases)
        tasks, _, _ = expand_loop_iter(lb, 1, context="c", scratchpad_prefix="")
        # 'do' phase should depend on 'plan' terminal
        do_task = next(t for t in tasks if "do" in t["local_id"])
        plan_task = next(t for t in tasks if "plan" in t["local_id"])
        assert plan_task["local_id"] in do_task["depends_on"]

    def test_prior_ids_applied_to_first_phase(self) -> None:
        phase = PhaseSpec(name="plan", pattern="single")
        lb = LoopBlock(name="loop", loop=LoopSpec(max=1), phases=[phase])
        tasks, _, _ = expand_loop_iter(
            lb, 1, context="c", scratchpad_prefix="", prior_ids=["task_abc"]
        )
        assert "task_abc" in tasks[0]["depends_on"]

    def test_iter_header_prepended_for_iter2(self) -> None:
        phase = PhaseSpec(name="plan", pattern="single", context="original_ctx")
        lb = LoopBlock(name="loop", loop=LoopSpec(max=3), phases=[phase])
        tasks, _, _ = expand_loop_iter(
            lb, 2, context="c", scratchpad_prefix="",
            prev_scratchpad_keys=["plan_iter1"]
        )
        # The prompt should contain iteration header
        prompt = tasks[0]["prompt"]
        assert "2/3" in prompt or "plan_iter1" in prompt

    def test_returns_terminal_ids(self) -> None:
        phases = [
            PhaseSpec(name="a", pattern="single"),
            PhaseSpec(name="b", pattern="single"),
        ]
        lb = LoopBlock(name="loop", loop=LoopSpec(max=1), phases=phases)
        _, _, terminals = expand_loop_iter(lb, 1, context="c", scratchpad_prefix="")
        # Terminal should be the last phase's task ID
        assert len(terminals) == 1
        assert "b" in terminals[0]


# ---------------------------------------------------------------------------
# is_until_condition_met tests
# ---------------------------------------------------------------------------


class TestIsUntilConditionMet:
    def test_no_until_condition_returns_false(self) -> None:
        lb = LoopBlock(name="loop", loop=LoopSpec(max=3), phases=[])
        assert is_until_condition_met(lb, {"some_key": "val"}) is False

    def test_condition_not_met_returns_false(self) -> None:
        cond = SkipCondition(key="done", value="yes")
        lb = LoopBlock(name="loop", loop=LoopSpec(max=3, until=cond), phases=[])
        assert is_until_condition_met(lb, {}) is False

    def test_condition_met_returns_true(self) -> None:
        cond = SkipCondition(key="quality_approved", value="yes")
        lb = LoopBlock(name="loop", loop=LoopSpec(max=3, until=cond), phases=[])
        assert is_until_condition_met(lb, {"quality_approved": "yes"}) is True

    def test_key_exists_no_value_condition(self) -> None:
        cond = SkipCondition(key="done")
        lb = LoopBlock(name="loop", loop=LoopSpec(max=3, until=cond), phases=[])
        assert is_until_condition_met(lb, {"done": "any"}) is True
        assert is_until_condition_met(lb, {}) is False


# ---------------------------------------------------------------------------
# expand_phase_items_with_status tests
# ---------------------------------------------------------------------------


class TestExpandPhaseItemsWithStatus:
    def test_flat_phase_specs_work(self) -> None:
        """Backward compatibility: PhaseSpec-only list uses same logic."""
        phases = [
            PhaseSpec(name="a", pattern="single"),
            PhaseSpec(name="b", pattern="single"),
        ]
        tasks, statuses, loop_terminals = expand_phase_items_with_status(
            phases, context="ctx", scratchpad_prefix=""
        )
        assert len(tasks) == 2
        assert len(statuses) == 2
        assert loop_terminals == {}

    def test_loop_block_generates_all_iter_tasks(self) -> None:
        """expand_phase_items_with_status pre-expands all max iterations."""
        lb = LoopBlock(
            name="pdca",
            loop=LoopSpec(max=3),
            phases=[
                PhaseSpec(name="plan", pattern="single"),
                PhaseSpec(name="do", pattern="single"),
            ],
        )
        tasks, statuses, loop_terminals = expand_phase_items_with_status(
            [lb], context="ctx", scratchpad_prefix=""
        )
        # 3 iterations × 2 phases = 6 tasks
        assert len(tasks) == 6
        assert "pdca" in loop_terminals
        assert len(loop_terminals["pdca"]) == 1  # terminal of last iter's last phase

    def test_outer_phase_after_loop_gets_loop_terminals(self) -> None:
        """Outer phase's depends_on can reference loop block name."""
        inner = PhaseSpec(name="work", pattern="single")
        lb = LoopBlock(name="the_loop", loop=LoopSpec(max=2), phases=[inner])
        # We deliberately DON'T add depends_on to outer_phase here since
        # depends_on resolution happens in the router. We just verify the
        # loop_terminals dict is populated correctly.
        outer = PhaseSpec(name="finalize", pattern="single")
        tasks, statuses, loop_terminals = expand_phase_items_with_status(
            [lb, outer], context="ctx", scratchpad_prefix=""
        )
        # loop_terminals should have the loop's terminal ids
        assert "the_loop" in loop_terminals

    def test_loop_terminal_ids_used_as_prior_for_next_item(self) -> None:
        """Items after a LoopBlock depend on the loop's terminal tasks."""
        inner = PhaseSpec(name="work", pattern="single")
        lb = LoopBlock(name="loop1", loop=LoopSpec(max=1), phases=[inner])
        outer = PhaseSpec(name="after", pattern="single")
        tasks, _, _ = expand_phase_items_with_status(
            [lb, outer], context="ctx", scratchpad_prefix=""
        )
        outer_task = next(t for t in tasks if "after" in t["local_id"])
        # outer_task should depend on the loop's terminal task
        loop_task = next(t for t in tasks if "work" in t["local_id"])
        assert loop_task["local_id"] in outer_task["depends_on"]

    def test_loop_block_registers_loop_terminal_ids(self) -> None:
        lb = LoopBlock(
            name="my_loop",
            loop=LoopSpec(max=1),
            phases=[PhaseSpec(name="p", pattern="single")],
        )
        _, _, loop_terminals = expand_phase_items_with_status(
            [lb], context="c", scratchpad_prefix=""
        )
        assert "my_loop" in loop_terminals
        assert isinstance(loop_terminals["my_loop"], list)
        assert len(loop_terminals["my_loop"]) > 0

    def test_mixed_items_generates_correct_phase_statuses(self) -> None:
        lb = LoopBlock(
            name="loop",
            loop=LoopSpec(max=2),
            phases=[
                PhaseSpec(name="a{iter}", pattern="single"),
                PhaseSpec(name="b{iter}", pattern="single"),
            ],
        )
        tasks, statuses, _ = expand_phase_items_with_status(
            [lb], context="c", scratchpad_prefix=""
        )
        # 2 iterations × 2 phases = 4 statuses total
        assert len(statuses) == 4

    def test_skipped_phase_spec_in_items(self) -> None:
        """PhaseSpec with met skip_condition is skipped."""
        skip_phase = PhaseSpec(
            name="skip_me",
            pattern="single",
            skip_condition=SkipCondition(key="already_done"),
        )
        keep_phase = PhaseSpec(name="keep_me", pattern="single")
        tasks, statuses, _ = expand_phase_items_with_status(
            [skip_phase, keep_phase],
            context="c",
            scratchpad_prefix="",
            scratchpad={"already_done": "1"},
        )
        assert len(tasks) == 1
        assert any(s.status == "skipped" for s in statuses)


# ---------------------------------------------------------------------------
# Backward compatibility: existing flat phases still work
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify that existing non-loop workflows are unaffected."""

    def test_expand_phases_with_status_unaffected(self) -> None:
        phases = [
            PhaseSpec(name="setup", pattern="single"),
            PhaseSpec(name="run", pattern="single"),
            PhaseSpec(name="verify", pattern="single"),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad_prefix="sp"
        )
        assert len(tasks) == 3
        assert len(statuses) == 3
        # Chaining: run depends on setup, verify depends on run
        run_task = next(t for t in tasks if "run" in t["local_id"])
        setup_task = next(t for t in tasks if "setup" in t["local_id"])
        verify_task = next(t for t in tasks if "verify" in t["local_id"])
        assert setup_task["local_id"] in run_task["depends_on"]
        assert run_task["local_id"] in verify_task["depends_on"]

    def test_expand_phase_items_with_flat_phases_produces_same_result(self) -> None:
        phases = [
            PhaseSpec(name="a", pattern="single"),
            PhaseSpec(name="b", pattern="single"),
        ]
        tasks1, statuses1 = expand_phases_with_status(
            phases, context="ctx", scratchpad_prefix=""
        )
        tasks2, statuses2, lt = expand_phase_items_with_status(
            phases, context="ctx", scratchpad_prefix=""
        )
        assert len(tasks1) == len(tasks2)
        assert lt == {}  # no loop terminals for flat phases

    def test_parallel_phases_still_work(self) -> None:
        phases = [
            PhaseSpec(
                name="parallel_work",
                pattern="parallel",
                agents=AgentSelector(count=3),
            ),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad_prefix=""
        )
        assert len(tasks) == 3
        assert len(statuses) == 1


# ---------------------------------------------------------------------------
# Web schema tests
# ---------------------------------------------------------------------------


class TestLoopSchemas:
    def test_loop_spec_model(self) -> None:
        from tmux_orchestrator.web.schemas import LoopSpecModel

        model = LoopSpecModel(max=3)
        assert model.max == 3
        assert model.until is None

    def test_loop_spec_model_with_until(self) -> None:
        from tmux_orchestrator.web.schemas import LoopSpecModel, SkipConditionModel

        cond = SkipConditionModel(key="done", value="yes")
        model = LoopSpecModel(max=2, until=cond)
        assert model.until is not None
        assert model.until.key == "done"

    def test_loop_spec_model_max_ge_1(self) -> None:
        from pydantic import ValidationError

        from tmux_orchestrator.web.schemas import LoopSpecModel

        with pytest.raises(ValidationError):
            LoopSpecModel(max=0)

    def test_loop_block_model(self) -> None:
        from tmux_orchestrator.web.schemas import LoopBlockModel, PhaseSpecModel

        inner = PhaseSpecModel(name="plan", pattern="single")
        lb = LoopBlockModel(name="my_loop", phases=[inner.model_dump()])
        assert lb.name == "my_loop"
        assert lb.loop.max == 5  # default

    def test_pdca_workflow_submit_defaults(self) -> None:
        from tmux_orchestrator.web.schemas import PdcaWorkflowSubmit

        model = PdcaWorkflowSubmit(objective="improve sort algorithm")
        assert model.max_cycles == 3
        assert model.scratchpad_prefix == "pdca"
        assert model.agent_timeout == 300
        assert model.success_condition is None

    def test_pdca_workflow_submit_custom(self) -> None:
        from tmux_orchestrator.web.schemas import PdcaWorkflowSubmit, SkipConditionModel

        model = PdcaWorkflowSubmit(
            objective="build feature X",
            max_cycles=5,
            success_condition=SkipConditionModel(key="quality_approved", value="yes"),
            planner_tags=["planner"],
            doer_tags=["doer"],
            checker_tags=["checker"],
            actor_tags=["actor"],
        )
        assert model.max_cycles == 5
        assert model.success_condition is not None
        assert model.success_condition.key == "quality_approved"


# ---------------------------------------------------------------------------
# POST /workflows/pdca endpoint test
# ---------------------------------------------------------------------------


class TestPdcaEndpoint:
    """Integration test for POST /workflows/pdca via FastAPI test client."""

    def test_pdca_endpoint_returns_workflow_id(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from tmux_orchestrator.application.workflow_manager import (
            WorkflowManager,
        )
        from tmux_orchestrator.web.routers.workflows import build_workflows_router

        # Build mock orchestrator
        wm = WorkflowManager()
        mock_orch = MagicMock()
        mock_orch.get_workflow_manager.return_value = wm

        task_counter = [0]

        async def _submit_task(prompt, **kwargs):
            task_counter[0] += 1
            t = MagicMock()
            t.id = f"task_{task_counter[0]:04d}"
            return t

        mock_orch.submit_task = _submit_task
        mock_orch.submit_task = AsyncMock(side_effect=_submit_task)

        auth = lambda: None  # noqa: E731

        app = FastAPI()
        router = build_workflows_router(mock_orch, auth)
        app.include_router(router)

        client = TestClient(app)
        resp = client.post(
            "/workflows/pdca",
            json={
                "objective": "improve sort algorithm quality",
                "max_cycles": 2,
                "planner_tags": ["pdca_planner"],
                "doer_tags": ["pdca_doer"],
                "checker_tags": ["pdca_checker"],
                "actor_tags": ["pdca_actor"],
            },
            headers={"X-API-Key": "test"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "workflow_id" in data
        assert "task_ids" in data
        assert data["max_cycles"] == 2
        assert data["loop_block_name"] == "pdca_cycle"
        # 2 cycles × 4 phases = 8 tasks
        assert len(data["task_ids"]) == 8

    def test_pdca_endpoint_max_cycles_1(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from tmux_orchestrator.application.workflow_manager import WorkflowManager
        from tmux_orchestrator.web.routers.workflows import build_workflows_router

        wm = WorkflowManager()
        mock_orch = MagicMock()
        mock_orch.get_workflow_manager.return_value = wm

        task_counter = [0]

        async def _submit_task(prompt, **kwargs):
            task_counter[0] += 1
            t = MagicMock()
            t.id = f"task_{task_counter[0]:04d}"
            return t

        mock_orch.submit_task = AsyncMock(side_effect=_submit_task)
        auth = lambda: None  # noqa: E731

        app = FastAPI()
        router = build_workflows_router(mock_orch, auth)
        app.include_router(router)

        client = TestClient(app)
        resp = client.post(
            "/workflows/pdca",
            json={"objective": "test", "max_cycles": 1},
            headers={"X-API-Key": "test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # 1 cycle × 4 phases = 4 tasks
        assert len(data["task_ids"]) == 4
