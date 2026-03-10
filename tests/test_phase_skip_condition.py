"""Unit tests for PhaseSpec.skip_condition (v1.1.36).

Covers:
- SkipCondition dataclass creation and field defaults
- SkipCondition.is_met() evaluation logic
- WorkflowPhaseStatus.mark_skipped() / is_resolved()
- expand_phases_from_specs with skip_condition (domain layer)
- expand_phases_with_status with skip_condition (shim layer)
- Dependency chain: skipped phase B between A and C — C still runs

Design reference: DESIGN.md §10.68 (v1.1.36)
"""

from __future__ import annotations

import pytest

from tmux_orchestrator.domain.phase_strategy import (
    AgentSelector,
    PhaseSpec,
    SkipCondition,
    WorkflowPhaseStatus,
    _evaluate_skip,
    expand_phases_from_specs,
)
from tmux_orchestrator.phase_executor import expand_phases_with_status


# ---------------------------------------------------------------------------
# SkipCondition dataclass tests
# ---------------------------------------------------------------------------


class TestSkipConditionDefaults:
    def test_key_required(self) -> None:
        sc = SkipCondition(key="my_key")
        assert sc.key == "my_key"

    def test_value_default_empty(self) -> None:
        sc = SkipCondition(key="k")
        assert sc.value == ""

    def test_negate_default_false(self) -> None:
        sc = SkipCondition(key="k")
        assert sc.negate is False

    def test_all_fields(self) -> None:
        sc = SkipCondition(key="build_status", value="failed", negate=True)
        assert sc.key == "build_status"
        assert sc.value == "failed"
        assert sc.negate is True


# ---------------------------------------------------------------------------
# SkipCondition.is_met() evaluation
# ---------------------------------------------------------------------------


class TestSkipConditionIsMet:
    # --- key-exists semantics (value == "") ---

    def test_key_exists_no_value_skip(self) -> None:
        sc = SkipCondition(key="done")
        assert sc.is_met({"done": "anything"}) is True

    def test_key_missing_no_value_no_skip(self) -> None:
        sc = SkipCondition(key="done")
        assert sc.is_met({}) is False

    def test_key_missing_no_value_no_skip_other_keys(self) -> None:
        sc = SkipCondition(key="done")
        assert sc.is_met({"other": "val"}) is False

    # --- exact-value semantics ---

    def test_value_match_skip(self) -> None:
        sc = SkipCondition(key="build_status", value="failed")
        assert sc.is_met({"build_status": "failed"}) is True

    def test_value_mismatch_no_skip(self) -> None:
        sc = SkipCondition(key="build_status", value="failed")
        assert sc.is_met({"build_status": "success"}) is False

    def test_value_key_missing_no_skip(self) -> None:
        sc = SkipCondition(key="build_status", value="failed")
        assert sc.is_met({}) is False

    # --- negate semantics ---

    def test_negate_key_exists_no_skip(self) -> None:
        """negate=True + key exists → condition is NOT met → no skip."""
        sc = SkipCondition(key="done", negate=True)
        assert sc.is_met({"done": "yes"}) is False

    def test_negate_key_missing_skip(self) -> None:
        """negate=True + key missing → condition IS met → skip."""
        sc = SkipCondition(key="done", negate=True)
        assert sc.is_met({}) is True

    def test_negate_value_match_no_skip(self) -> None:
        """negate=True + value matches → base=True → negate → False (no skip)."""
        sc = SkipCondition(key="status", value="ok", negate=True)
        assert sc.is_met({"status": "ok"}) is False

    def test_negate_value_mismatch_skip(self) -> None:
        """negate=True + value mismatch → base=False → negate → True (skip)."""
        sc = SkipCondition(key="status", value="ok", negate=True)
        assert sc.is_met({"status": "fail"}) is True

    # --- value stored as non-string in scratchpad ---

    def test_value_compared_as_str(self) -> None:
        """Scratchpad values are compared via str() — matches integer 1 with "1"."""
        sc = SkipCondition(key="count", value="1")
        assert sc.is_met({"count": 1}) is True

    def test_value_compared_as_str_mismatch(self) -> None:
        sc = SkipCondition(key="count", value="1")
        assert sc.is_met({"count": 2}) is False


# ---------------------------------------------------------------------------
# _evaluate_skip helper
# ---------------------------------------------------------------------------


class TestEvaluateSkip:
    def test_no_skip_condition_returns_false(self) -> None:
        phase = PhaseSpec(name="p", pattern="single")
        assert _evaluate_skip(phase, {"k": "v"}) is False

    def test_none_scratchpad_returns_false(self) -> None:
        phase = PhaseSpec(
            name="p", pattern="single",
            skip_condition=SkipCondition(key="k"),
        )
        assert _evaluate_skip(phase, None) is False

    def test_skip_condition_met(self) -> None:
        phase = PhaseSpec(
            name="p", pattern="single",
            skip_condition=SkipCondition(key="skip_me"),
        )
        assert _evaluate_skip(phase, {"skip_me": "yes"}) is True

    def test_skip_condition_not_met(self) -> None:
        phase = PhaseSpec(
            name="p", pattern="single",
            skip_condition=SkipCondition(key="skip_me"),
        )
        assert _evaluate_skip(phase, {}) is False


# ---------------------------------------------------------------------------
# WorkflowPhaseStatus.mark_skipped() / is_resolved()
# ---------------------------------------------------------------------------


class TestWorkflowPhaseStatusSkipped:
    def test_mark_skipped_sets_status(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=[])
        ps.mark_skipped()
        assert ps.status == "skipped"

    def test_mark_skipped_sets_timestamps(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=[])
        ps.mark_skipped()
        assert ps.started_at is not None
        assert ps.completed_at is not None

    def test_mark_skipped_preserves_existing_started_at(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=[])
        ps.started_at = 1000.0
        ps.mark_skipped()
        assert ps.started_at == 1000.0

    def test_is_resolved_skipped(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=[])
        ps.mark_skipped()
        assert ps.is_resolved() is True

    def test_is_resolved_complete(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=["t1"])
        ps.mark_complete()
        assert ps.is_resolved() is True

    def test_is_resolved_pending(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=[])
        assert ps.is_resolved() is False

    def test_is_resolved_failed(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=[])
        ps.mark_failed()
        assert ps.is_resolved() is False

    def test_to_dict_includes_skipped_status(self) -> None:
        ps = WorkflowPhaseStatus(name="b", pattern="single", task_ids=[])
        ps.mark_skipped()
        d = ps.to_dict()
        assert d["status"] == "skipped"
        assert d["task_ids"] == []


# ---------------------------------------------------------------------------
# Integration: expand_phases_from_specs with skip_condition (domain)
# ---------------------------------------------------------------------------


def _make_phase(name: str, skip_condition: SkipCondition | None = None) -> PhaseSpec:
    return PhaseSpec(
        name=name,
        pattern="single",
        agents=AgentSelector(tags=[], count=1),
        skip_condition=skip_condition,
    )


class TestExpandPhasesFromSpecsSkip:
    def test_no_skip_all_tasks_created(self) -> None:
        phases = [_make_phase("a"), _make_phase("b"), _make_phase("c")]
        tasks = expand_phases_from_specs(phases, context="ctx", scratchpad={})
        local_ids = [t["local_id"] for t in tasks]
        assert "phase_a_0" in local_ids
        assert "phase_b_0" in local_ids
        assert "phase_c_0" in local_ids

    def test_skip_b_no_task_for_b(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks = expand_phases_from_specs(
            phases, context="ctx", scratchpad={"skip_b": "yes"}
        )
        local_ids = [t["local_id"] for t in tasks]
        assert "phase_a_0" in local_ids
        assert "phase_b_0" not in local_ids
        assert "phase_c_0" in local_ids

    def test_skip_b_not_met_all_tasks_created(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks = expand_phases_from_specs(
            phases, context="ctx", scratchpad={}  # key not present → not skipped
        )
        local_ids = [t["local_id"] for t in tasks]
        assert "phase_b_0" in local_ids

    def test_skip_b_dependency_chain_honoured(self) -> None:
        """Phase C depends on A's task when B is skipped (B passes deps through)."""
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks = expand_phases_from_specs(
            phases, context="ctx", scratchpad={"skip_b": "1"}
        )
        by_id = {t["local_id"]: t for t in tasks}
        # C must depend on A (since B was skipped, A's terminal_ids pass through)
        assert by_id["phase_c_0"]["depends_on"] == ["phase_a_0"]

    def test_no_scratchpad_never_skips(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
        ]
        tasks = expand_phases_from_specs(
            phases, context="ctx", scratchpad=None
        )
        local_ids = [t["local_id"] for t in tasks]
        assert "phase_b_0" in local_ids

    def test_all_phases_skipped_returns_empty(self) -> None:
        phases = [
            _make_phase("a", skip_condition=SkipCondition(key="skip_all")),
            _make_phase("b", skip_condition=SkipCondition(key="skip_all")),
        ]
        tasks = expand_phases_from_specs(
            phases, context="ctx", scratchpad={"skip_all": "yes"}
        )
        assert tasks == []


# ---------------------------------------------------------------------------
# Integration: expand_phases_with_status with skip_condition (shim)
# ---------------------------------------------------------------------------


class TestExpandPhasesWithStatusSkip:
    def test_skipped_phase_has_status_skipped(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={"skip_b": "yes"}
        )
        by_name = {s.name: s for s in statuses}
        assert by_name["b"].status == "skipped"
        assert by_name["b"].task_ids == []

    def test_skipped_phase_returns_status_tracker(self) -> None:
        phases = [
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={"skip_b": "yes"}
        )
        assert len(statuses) == 1
        assert statuses[0].name == "b"
        assert statuses[0].status == "skipped"

    def test_non_skipped_phases_have_normal_status(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={"skip_b": "yes"}
        )
        by_name = {s.name: s for s in statuses}
        assert by_name["a"].status == "pending"
        assert by_name["c"].status == "pending"

    def test_three_statuses_returned_even_when_b_skipped(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={"skip_b": "yes"}
        )
        assert len(statuses) == 3

    def test_tasks_only_for_non_skipped_phases(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks, _ = expand_phases_with_status(
            phases, context="ctx", scratchpad={"skip_b": "yes"}
        )
        local_ids = [t["local_id"] for t in tasks]
        assert "phase_a_0" in local_ids
        assert "phase_b_0" not in local_ids
        assert "phase_c_0" in local_ids

    def test_c_depends_on_a_when_b_skipped(self) -> None:
        phases = [
            _make_phase("a"),
            _make_phase("b", skip_condition=SkipCondition(key="skip_b")),
            _make_phase("c"),
        ]
        tasks, _ = expand_phases_with_status(
            phases, context="ctx", scratchpad={"skip_b": "yes"}
        )
        by_id = {t["local_id"]: t for t in tasks}
        assert by_id["phase_c_0"]["depends_on"] == ["phase_a_0"]

    def test_value_match_skip_condition(self) -> None:
        phases = [
            _make_phase("b", skip_condition=SkipCondition(key="build", value="ok")),
        ]
        # value == "ok" in scratchpad → skip
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={"build": "ok"}
        )
        assert statuses[0].status == "skipped"
        assert tasks == []

    def test_value_mismatch_no_skip(self) -> None:
        phases = [
            _make_phase("b", skip_condition=SkipCondition(key="build", value="ok")),
        ]
        # value != "ok" → not skipped
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={"build": "fail"}
        )
        assert statuses[0].status == "pending"
        assert len(tasks) == 1

    def test_negate_skip_when_key_missing(self) -> None:
        """negate=True + key missing → skip."""
        phases = [
            _make_phase("b", skip_condition=SkipCondition(key="approved", negate=True)),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={}
        )
        assert statuses[0].status == "skipped"

    def test_negate_no_skip_when_key_exists(self) -> None:
        """negate=True + key exists → not skipped."""
        phases = [
            _make_phase("b", skip_condition=SkipCondition(key="approved", negate=True)),
        ]
        tasks, statuses = expand_phases_with_status(
            phases, context="ctx", scratchpad={"approved": "yes"}
        )
        assert statuses[0].status == "pending"
