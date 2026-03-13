"""Tests for workflow phase webhook events (v1.2.9).

Verifies that WorkflowManager fires phase_complete, phase_failed, and
phase_skipped webhook events when phases transition to terminal states.

Design reference: DESIGN.md §10.85 (v1.2.9)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tmux_orchestrator.application.workflow_manager import WorkflowManager
from tmux_orchestrator.domain.phase_strategy import WorkflowPhaseStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> WorkflowManager:
    """Return a fresh WorkflowManager for each test."""
    return WorkflowManager()


def _make_phase(name: str, task_ids: list[str], pattern: str = "single") -> WorkflowPhaseStatus:
    """Build a WorkflowPhaseStatus with the given task_ids."""
    return WorkflowPhaseStatus(name=name, pattern=pattern, task_ids=list(task_ids))


def _submit_with_phases(
    wm: WorkflowManager,
    name: str,
    phases: list[WorkflowPhaseStatus],
) -> str:
    """Submit a workflow, attach phases, call register_phases, return workflow_id."""
    all_task_ids = [tid for ps in phases for tid in ps.task_ids]
    run = wm.submit(name=name, task_ids=all_task_ids)
    run.phases = phases
    wm.register_phases(run.id)
    return run.id


async def _complete_tasks_collect_calls(
    wm: WorkflowManager,
    task_ids: list[str],
    *,
    fn: Any,
) -> list[tuple[str, dict]]:
    """Install webhook fn, complete all tasks, drain pending tasks, return calls."""
    calls: list[tuple[str, dict]] = []

    async def capturing_fn(event_type: str, payload: dict) -> None:
        calls.append((event_type, payload))

    wm.set_webhook_fn(capturing_fn)
    for tid in task_ids:
        wm.on_task_complete(tid)
    # Drain all pending asyncio tasks (ensure_future callbacks)
    await asyncio.sleep(0)
    return calls


async def _fail_tasks_collect_calls(
    wm: WorkflowManager,
    task_ids: list[str],
) -> list[tuple[str, dict]]:
    """Install webhook fn, fail all tasks, drain, return calls."""
    calls: list[tuple[str, dict]] = []

    async def capturing_fn(event_type: str, payload: dict) -> None:
        calls.append((event_type, payload))

    wm.set_webhook_fn(capturing_fn)
    for tid in task_ids:
        wm.on_task_failed(tid)
    await asyncio.sleep(0)
    return calls


# ---------------------------------------------------------------------------
# 1. set_webhook_fn stores the function
# ---------------------------------------------------------------------------


class TestSetWebhookFn:
    def test_set_webhook_fn_stores_function(self) -> None:
        wm = _make_manager()

        async def fn(event_type: str, payload: dict) -> None:
            pass

        wm.set_webhook_fn(fn)
        assert wm._fire_webhook_fn is fn

    def test_set_webhook_fn_replaces_previous(self) -> None:
        wm = _make_manager()

        async def fn1(event_type: str, payload: dict) -> None:
            pass

        async def fn2(event_type: str, payload: dict) -> None:
            pass

        wm.set_webhook_fn(fn1)
        wm.set_webhook_fn(fn2)
        assert wm._fire_webhook_fn is fn2

    def test_initial_fire_webhook_fn_is_none(self) -> None:
        wm = _make_manager()
        assert wm._fire_webhook_fn is None


# ---------------------------------------------------------------------------
# 2. phase_complete event
# ---------------------------------------------------------------------------


class TestPhaseCompleteWebhook:
    def test_phase_complete_fires_webhook(self) -> None:
        """Completing all tasks in a phase fires 'phase_complete' webhook."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase_a = _make_phase("plan", ["t1", "t2"])
            _submit_with_phases(wm, "wf", [phase_a])

            wm.on_task_complete("t1")
            await asyncio.sleep(0)
            assert len(calls) == 0, "webhook must not fire until all tasks done"

            wm.on_task_complete("t2")
            await asyncio.sleep(0)

            assert len(calls) == 1
            assert calls[0][0] == "phase_complete"

        asyncio.run(_test())

    def test_phase_complete_payload_fields(self) -> None:
        """phase_complete payload contains required fields."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase_a = _make_phase("implement", ["t1"])
            wf_id = _submit_with_phases(wm, "my-workflow", [phase_a])

            wm.on_task_complete("t1")
            await asyncio.sleep(0)

            assert len(calls) == 1
            _, payload = calls[0]
            assert payload["workflow_id"] == wf_id
            assert payload["workflow_name"] == "my-workflow"
            assert payload["phase_name"] == "implement"
            assert payload["task_ids"] == ["t1"]
            assert "timestamp" in payload

        asyncio.run(_test())

    def test_phase_complete_fires_after_all_tasks_done(self) -> None:
        """Webhook fires only after ALL tasks in the phase complete (not just first)."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase = _make_phase("impl", ["t1", "t2", "t3"])
            _submit_with_phases(wm, "wf", [phase])

            wm.on_task_complete("t1")
            await asyncio.sleep(0)
            assert len(calls) == 0

            wm.on_task_complete("t2")
            await asyncio.sleep(0)
            assert len(calls) == 0

            wm.on_task_complete("t3")
            await asyncio.sleep(0)
            assert len(calls) == 1
            assert calls[0][0] == "phase_complete"

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# 3. phase_failed event
# ---------------------------------------------------------------------------


class TestPhaseFailedWebhook:
    def test_phase_failed_fires_webhook(self) -> None:
        """Failing all tasks in a single-task phase fires 'phase_failed' webhook."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase = _make_phase("test", ["t1"])
            wf_id = _submit_with_phases(wm, "wf", [phase])

            wm.on_task_failed("t1")
            await asyncio.sleep(0)

            assert len(calls) == 1
            event_type, payload = calls[0]
            assert event_type == "phase_failed"
            assert payload["workflow_id"] == wf_id
            assert payload["phase_name"] == "test"

        asyncio.run(_test())

    def test_phase_failed_fires_when_any_task_fails(self) -> None:
        """phase_failed fires when at least one task fails (others may complete)."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase = _make_phase("test", ["t1", "t2"])
            _submit_with_phases(wm, "wf", [phase])

            wm.on_task_complete("t1")
            await asyncio.sleep(0)
            assert len(calls) == 0  # not all resolved yet

            wm.on_task_failed("t2")
            await asyncio.sleep(0)

            assert len(calls) == 1
            assert calls[0][0] == "phase_failed"

        asyncio.run(_test())

    def test_phase_failed_payload_contains_workflow_id_and_phase_name(self) -> None:
        """phase_failed payload includes workflow_id and phase_name."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase = _make_phase("build", ["t1"])
            wf_id = _submit_with_phases(wm, "pipeline", [phase])

            wm.on_task_failed("t1")
            await asyncio.sleep(0)

            assert len(calls) == 1
            payload = calls[0][1]
            assert payload["workflow_id"] == wf_id
            assert payload["workflow_name"] == "pipeline"
            assert payload["phase_name"] == "build"
            assert "timestamp" in payload

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# 4. phase_skipped event
# ---------------------------------------------------------------------------


class TestPhaseSkippedWebhook:
    def test_phase_skipped_fires_webhook_via_mark_task_skipped(self) -> None:
        """phase_skipped fires when _mark_task_skipped transitions a phase to skipped."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase = _make_phase("iter2", ["t2"])
            wf_id = _submit_with_phases(wm, "loop-wf", [phase])

            # Simulate loop early termination skipping this task
            wm._mark_task_skipped("t2", wf_id)
            await asyncio.sleep(0)

            skipped_calls = [c for c in calls if c[0] == "phase_skipped"]
            assert len(skipped_calls) == 1
            assert skipped_calls[0][1]["phase_name"] == "iter2"

        asyncio.run(_test())

    def test_phase_skipped_not_fired_twice_if_already_skipped(self) -> None:
        """Calling _mark_task_skipped on already-skipped phase does not fire again."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase = _make_phase("iter2", ["t2", "t3"])
            wf_id = _submit_with_phases(wm, "loop-wf", [phase])

            wm._mark_task_skipped("t2", wf_id)
            await asyncio.sleep(0)
            first_count = len([c for c in calls if c[0] == "phase_skipped"])

            # Second mark_task_skipped on already-skipped phase: no additional event
            wm._mark_task_skipped("t3", wf_id)
            await asyncio.sleep(0)
            second_count = len([c for c in calls if c[0] == "phase_skipped"])

            assert second_count == first_count  # no new skipped event

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# 5. No crash when webhook fn not set
# ---------------------------------------------------------------------------


class TestNoWebhookFn:
    def test_no_crash_when_webhook_fn_not_set(self) -> None:
        """WorkflowManager must not crash if set_webhook_fn was never called."""
        wm = _make_manager()
        phase = _make_phase("plan", ["t1"])
        _submit_with_phases(wm, "wf", [phase])
        # Should not raise
        wm.on_task_complete("t1")

    def test_no_crash_on_task_not_in_phases(self) -> None:
        """Tasks not in any phase must not trigger webhook or crash."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            # Submit workflow without phases
            wm.submit("wf", ["t1"])
            wm.on_task_complete("t1")
            await asyncio.sleep(0)
            assert calls == []

        asyncio.run(_test())

    def test_no_crash_on_failed_task_not_in_phases(self) -> None:
        """Tasks not in any phase must not trigger webhook or crash on failure."""
        wm = _make_manager()
        wm.submit("wf", ["t1"])
        # Should not raise even without phases
        wm.on_task_failed("t1")


# ---------------------------------------------------------------------------
# 6. Multiple phases completing — one webhook per phase
# ---------------------------------------------------------------------------


class TestMultiplePhases:
    def test_one_webhook_per_phase(self) -> None:
        """Each phase that completes fires exactly one webhook event."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            plan = _make_phase("plan", ["t1"])
            impl = _make_phase("impl", ["t2"])
            test = _make_phase("test", ["t3"])
            _submit_with_phases(wm, "wf", [plan, impl, test])

            for tid in ["t1", "t2", "t3"]:
                wm.on_task_complete(tid)
            await asyncio.sleep(0)

            complete_calls = [c for c in calls if c[0] == "phase_complete"]
            assert len(complete_calls) == 3
            phase_names = {c[1]["phase_name"] for c in complete_calls}
            assert phase_names == {"plan", "impl", "test"}

        asyncio.run(_test())

    def test_workflow_id_consistent_across_phase_events(self) -> None:
        """All phase webhooks for the same workflow share the same workflow_id."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            plan = _make_phase("plan", ["t1"])
            impl = _make_phase("impl", ["t2"])
            wf_id = _submit_with_phases(wm, "wf", [plan, impl])

            wm.on_task_complete("t1")
            wm.on_task_complete("t2")
            await asyncio.sleep(0)

            assert all(c[1]["workflow_id"] == wf_id for c in calls)

        asyncio.run(_test())

    def test_task_ids_in_payload_match_phase(self) -> None:
        """Webhook payload task_ids must match the phase's task_ids."""
        async def _test() -> None:
            wm = _make_manager()
            calls: list[tuple[str, dict]] = []

            async def fn(event_type: str, payload: dict) -> None:
                calls.append((event_type, payload))

            wm.set_webhook_fn(fn)
            phase = _make_phase("parallel-impl", ["t1", "t2", "t3"])
            _submit_with_phases(wm, "wf", [phase])

            wm.on_task_complete("t1")
            wm.on_task_complete("t2")
            wm.on_task_complete("t3")
            await asyncio.sleep(0)

            assert len(calls) == 1
            payload = calls[0][1]
            assert set(payload["task_ids"]) == {"t1", "t2", "t3"}

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# 7. KNOWN_EVENTS includes new phase events
# ---------------------------------------------------------------------------


class TestKnownEvents:
    def test_phase_complete_in_known_events(self) -> None:
        from tmux_orchestrator.webhook_manager import KNOWN_EVENTS  # noqa: PLC0415
        assert "phase_complete" in KNOWN_EVENTS

    def test_phase_failed_in_known_events(self) -> None:
        from tmux_orchestrator.webhook_manager import KNOWN_EVENTS  # noqa: PLC0415
        assert "phase_failed" in KNOWN_EVENTS

    def test_phase_skipped_in_known_events(self) -> None:
        from tmux_orchestrator.webhook_manager import KNOWN_EVENTS  # noqa: PLC0415
        assert "phase_skipped" in KNOWN_EVENTS
