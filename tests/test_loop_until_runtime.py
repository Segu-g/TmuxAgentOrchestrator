"""Tests for loop until runtime evaluation (v1.2.7).

WorkflowManager.register_loop() + _check_loop_until() + _mark_task_skipped()
cancel remaining iteration tasks when the until condition is met after an
iteration completes.

Design reference: DESIGN.md §10.83 (v1.2.7)
"""

from __future__ import annotations

import pytest

from tmux_orchestrator.application.workflow_manager import WorkflowManager
from tmux_orchestrator.domain.phase_strategy import LoopBlock, LoopSpec, SkipCondition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_loop_spec(key: str, value: str = "", negate: bool = False, max_iter: int = 3) -> tuple[LoopSpec, LoopBlock]:
    """Build a minimal LoopSpec and LoopBlock for tests."""
    until = SkipCondition(key=key, value=value, negate=negate)
    spec = LoopSpec(max=max_iter, until=until)
    block = LoopBlock(name="test_loop", loop=spec, phases=[])
    return spec, block


def _make_wm() -> WorkflowManager:
    return WorkflowManager()


# ---------------------------------------------------------------------------
# 1. register_loop stores iterations and loop_spec correctly
# ---------------------------------------------------------------------------


def test_register_loop_stores_data():
    wm = _make_wm()
    run = wm.submit("wf-test", ["t1", "t2", "t3", "t4"])
    spec, block = _make_loop_spec("quality_ok", value="yes")
    iterations = [["t1", "t2"], ["t3", "t4"]]

    wm.register_loop(run.id, "test_loop", spec, iterations, "prefix")

    key = (run.id, "test_loop")
    assert wm._loop_iterations[key] == iterations
    assert wm._loop_specs[key] is spec
    assert wm._loop_scratchpad_prefix[key] == "prefix"


# ---------------------------------------------------------------------------
# 2. on_task_complete updates _completed_tasks
# ---------------------------------------------------------------------------


def test_completed_tasks_tracked():
    wm = _make_wm()
    run = wm.submit("wf", ["t1", "t2"])
    wm.on_task_complete("t1")
    assert "t1" in wm._completed_tasks
    assert "t2" not in wm._completed_tasks


# ---------------------------------------------------------------------------
# 3. Until condition met → remaining task IDs are cancelled
# ---------------------------------------------------------------------------


def test_until_condition_met_triggers_cancel():
    cancelled = []
    wm = _make_wm()
    wm.set_cancel_task_fn(lambda tid: cancelled.append(tid))
    # Scratchpad has quality_ok=yes → condition will be met
    wm.set_scratchpad({"quality_ok": "yes"})

    run = wm.submit("wf", ["t1", "t2", "t3", "t4"])
    spec, block = _make_loop_spec("quality_ok", value="yes")
    iterations = [["t1", "t2"], ["t3", "t4"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    # Complete iteration 0 — condition is met
    wm.on_task_complete("t1")
    wm.on_task_complete("t2")

    # t3 and t4 (iteration 1) should have been scheduled for cancellation
    assert set(cancelled) == {"t3", "t4"}


# ---------------------------------------------------------------------------
# 4. Until condition NOT met → no cancellation
# ---------------------------------------------------------------------------


def test_until_condition_not_met_no_cancel():
    cancelled = []
    wm = _make_wm()
    wm.set_cancel_task_fn(lambda tid: cancelled.append(tid))
    # Scratchpad does NOT have quality_ok=yes
    wm.set_scratchpad({"quality_ok": "no"})

    run = wm.submit("wf", ["t1", "t2", "t3", "t4"])
    spec, block = _make_loop_spec("quality_ok", value="yes")
    iterations = [["t1", "t2"], ["t3", "t4"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    wm.on_task_complete("t1")
    wm.on_task_complete("t2")

    assert cancelled == []
    # Loop registration still present (condition not met)
    assert (run.id, "test_loop") in wm._loop_iterations


# ---------------------------------------------------------------------------
# 5. Cancelled tasks counted as completed → workflow reaches "complete"
# ---------------------------------------------------------------------------


def test_workflow_completes_after_loop_early_exit():
    wm = _make_wm()
    wm.set_cancel_task_fn(lambda tid: None)  # no-op cancel
    wm.set_scratchpad({"done": ""})  # key exists → condition met

    run = wm.submit("wf", ["t1", "t2", "t3", "t4"])
    spec = LoopSpec(max=2, until=SkipCondition(key="done", value=""))
    iterations = [["t1", "t2"], ["t3", "t4"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    wm.on_task_complete("t1")
    wm.on_task_complete("t2")

    # t3, t4 should have been marked as resolved → workflow complete
    assert run.status == "complete"


# ---------------------------------------------------------------------------
# 6. Incomplete iteration → no evaluation yet
# ---------------------------------------------------------------------------


def test_partial_iteration_not_evaluated():
    cancelled = []
    wm = _make_wm()
    wm.set_cancel_task_fn(lambda tid: cancelled.append(tid))
    wm.set_scratchpad({"done": "yes"})

    run = wm.submit("wf", ["t1", "t2", "t3", "t4"])
    spec, _ = _make_loop_spec("done", value="yes")
    iterations = [["t1", "t2"], ["t3", "t4"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    # Complete only one of two tasks in iter 0 — condition should NOT fire yet
    wm.on_task_complete("t1")

    assert cancelled == []
    assert (run.id, "test_loop") in wm._loop_iterations


# ---------------------------------------------------------------------------
# 7. Registry cleaned up after condition fires
# ---------------------------------------------------------------------------


def test_loop_registry_cleaned_up_after_cancel():
    wm = _make_wm()
    wm.set_cancel_task_fn(lambda tid: None)
    wm.set_scratchpad({"flag": "1"})

    run = wm.submit("wf", ["t1", "t2", "t3"])
    spec = LoopSpec(max=2, until=SkipCondition(key="flag", value="1"))
    iterations = [["t1"], ["t2", "t3"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    wm.on_task_complete("t1")

    assert (run.id, "test_loop") not in wm._loop_iterations
    assert (run.id, "test_loop") not in wm._loop_specs


# ---------------------------------------------------------------------------
# 8. is_until_condition_met with value="" (key-exists check)
# ---------------------------------------------------------------------------


def test_is_until_key_exists():
    from tmux_orchestrator.phase_executor import is_until_condition_met

    spec = LoopSpec(max=3, until=SkipCondition(key="mykey", value=""))
    block = LoopBlock(name="b", loop=spec, phases=[])

    assert is_until_condition_met(block, {"mykey": "anything"}) is True
    assert is_until_condition_met(block, {}) is False


# ---------------------------------------------------------------------------
# 9. is_until_condition_met with negate=True
# ---------------------------------------------------------------------------


def test_is_until_negate():
    from tmux_orchestrator.phase_executor import is_until_condition_met

    spec = LoopSpec(max=3, until=SkipCondition(key="done", value="yes", negate=True))
    block = LoopBlock(name="b", loop=spec, phases=[])

    # negate=True → condition met when key does NOT equal "yes"
    assert is_until_condition_met(block, {"done": "no"}) is True
    assert is_until_condition_met(block, {"done": "yes"}) is False
    assert is_until_condition_met(block, {}) is True  # key absent → negate of "not present" check


# ---------------------------------------------------------------------------
# 10. Scratchpad injection via set_scratchpad
# ---------------------------------------------------------------------------


def test_set_scratchpad_injected():
    wm = _make_wm()
    sp = {"test_key": "hello"}
    wm.set_scratchpad(sp)
    assert wm._scratchpad is sp


# ---------------------------------------------------------------------------
# 11. No cancel fn → cancelled tasks still marked as resolved (graceful)
# ---------------------------------------------------------------------------


def test_no_cancel_fn_graceful():
    """Even without a cancel function, the loop registry is cleaned up."""
    wm = _make_wm()
    # No cancel fn injected
    wm.set_scratchpad({"flag": "yes"})

    run = wm.submit("wf", ["t1", "t2", "t3"])
    spec = LoopSpec(max=2, until=SkipCondition(key="flag", value="yes"))
    iterations = [["t1"], ["t2", "t3"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    wm.on_task_complete("t1")  # should not raise

    # Registry cleaned up
    assert (run.id, "test_loop") not in wm._loop_iterations
    # Remaining tasks still marked as resolved
    assert run.status == "complete"


# ---------------------------------------------------------------------------
# 12. is_until_condition_met returns False when until is None
# ---------------------------------------------------------------------------


def test_is_until_none_always_false():
    from tmux_orchestrator.phase_executor import is_until_condition_met

    spec = LoopSpec(max=3, until=None)
    block = LoopBlock(name="b", loop=spec, phases=[])
    # Even with a populated scratchpad, always False
    assert is_until_condition_met(block, {"any_key": "any_value"}) is False


# ---------------------------------------------------------------------------
# 13. Three-iteration loop: condition met after iter 1 → only iter 1 runs
# ---------------------------------------------------------------------------


def test_three_iter_only_first_runs():
    cancelled = []
    wm = _make_wm()
    wm.set_cancel_task_fn(lambda tid: cancelled.append(tid))
    wm.set_scratchpad({"ok": "yes"})

    # 3 iterations, 1 task each
    run = wm.submit("wf", ["t1", "t2", "t3"])
    spec = LoopSpec(max=3, until=SkipCondition(key="ok", value="yes"))
    iterations = [["t1"], ["t2"], ["t3"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    wm.on_task_complete("t1")  # iter 0 done → condition met → cancel iter 1 + 2

    assert set(cancelled) == {"t2", "t3"}
    assert run.status == "complete"


# ---------------------------------------------------------------------------
# 14. Phase status marked skipped for cancelled tasks
# ---------------------------------------------------------------------------


def test_phase_status_marked_skipped():
    from tmux_orchestrator.domain.phase_strategy import WorkflowPhaseStatus

    wm = _make_wm()
    wm.set_cancel_task_fn(lambda tid: None)
    wm.set_scratchpad({"ok": "yes"})

    run = wm.submit("wf", ["t1", "t2", "t3"])
    spec = LoopSpec(max=2, until=SkipCondition(key="ok", value="yes"))
    iterations = [["t1"], ["t2", "t3"]]
    wm.register_loop(run.id, "test_loop", spec, iterations, "pref")

    # Attach phases so _mark_task_skipped can find them
    ps1 = WorkflowPhaseStatus(name="iter1", pattern="single", task_ids=["t1"])
    ps2 = WorkflowPhaseStatus(name="iter2", pattern="single", task_ids=["t2", "t3"])
    run.phases = [ps1, ps2]
    wm.register_phases(run.id)

    wm.on_task_complete("t1")

    assert ps2.status == "skipped"


# ---------------------------------------------------------------------------
# 15. Multiple loops in same workflow — only the matching loop fires
# ---------------------------------------------------------------------------


def test_multiple_loops_independent():
    cancelled_a = []
    cancelled_b = []

    wm = _make_wm()
    # loop_a condition met, loop_b condition not met
    wm.set_scratchpad({"flag_a": "yes"})

    def cancel_fn(tid: str) -> None:
        if tid.startswith("a"):
            cancelled_a.append(tid)
        else:
            cancelled_b.append(tid)

    wm.set_cancel_task_fn(cancel_fn)

    all_tids = ["a1", "a2", "a3", "b1", "b2", "b3"]
    run = wm.submit("wf", all_tids)

    spec_a = LoopSpec(max=2, until=SkipCondition(key="flag_a", value="yes"))
    spec_b = LoopSpec(max=2, until=SkipCondition(key="flag_b", value="yes"))
    wm.register_loop(run.id, "loop_a", spec_a, [["a1"], ["a2", "a3"]], "pref")
    wm.register_loop(run.id, "loop_b", spec_b, [["b1"], ["b2", "b3"]], "pref")

    wm.on_task_complete("a1")  # iter 0 of loop_a done → cancel a2, a3

    assert set(cancelled_a) == {"a2", "a3"}
    assert cancelled_b == []
    # loop_b still registered (condition not met)
    assert (run.id, "loop_b") in wm._loop_iterations
