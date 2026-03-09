"""Tests that domain/ contains only pure stdlib types.

Verifies:
1. Each domain module imports only Python stdlib modules (no third-party libs).
2. Shim re-exports from old locations still work.
3. The domain package itself re-exports all public types.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

DOMAIN_DIR = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "domain"
DOMAIN_FILES = ["agent.py", "task.py", "message.py", "workflow.py", "phase_strategy.py"]

# sys.stdlib_module_names is available from Python 3.10+
STDLIB_NAMES: frozenset[str] = frozenset(sys.stdlib_module_names)

# These are always allowed (internal package imports within domain/)
INTERNAL_PREFIX = "tmux_orchestrator.domain"


def _extract_top_level_imports(filepath: Path) -> list[str]:
    """Parse a Python file and return the top-level module names imported."""
    tree = ast.parse(filepath.read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module.split(".")[0])
    return modules


# ------------------------------------------------------------------
# Purity tests: domain/ must import only stdlib
# ------------------------------------------------------------------


@pytest.mark.parametrize("filename", DOMAIN_FILES)
def test_domain_module_imports_only_stdlib(filename: str) -> None:
    """Each domain/*.py file must import only Python stdlib modules."""
    filepath = DOMAIN_DIR / filename
    assert filepath.exists(), f"Domain file missing: {filepath}"

    imported_top_levels = _extract_top_level_imports(filepath)
    non_stdlib = [
        name
        for name in imported_top_levels
        if name not in STDLIB_NAMES
        and name != "__future__"
        and not name.startswith("tmux_orchestrator")
    ]
    assert non_stdlib == [], (
        f"{filename} imports non-stdlib modules: {non_stdlib}. "
        "domain/ must be free of third-party dependencies."
    )


# ------------------------------------------------------------------
# Shim tests: old import paths must still work
# ------------------------------------------------------------------


def test_agent_status_shim() -> None:
    """AgentStatus can be imported from agents.base (backward compat)."""
    from tmux_orchestrator.agents.base import AgentStatus

    assert AgentStatus.IDLE == "IDLE"
    assert AgentStatus.BUSY == "BUSY"
    assert AgentStatus.ERROR == "ERROR"
    assert AgentStatus.STOPPED == "STOPPED"
    assert AgentStatus.DRAINING == "DRAINING"


def test_agent_role_shim() -> None:
    """AgentRole can be imported from config (backward compat)."""
    from tmux_orchestrator.config import AgentRole

    assert AgentRole.WORKER == "worker"
    assert AgentRole.DIRECTOR == "director"


def test_task_shim() -> None:
    """Task can be imported from agents.base (backward compat)."""
    from tmux_orchestrator.agents.base import Task

    t = Task(id="x", prompt="hello")
    assert t.id == "x"
    assert t.prompt == "hello"
    assert t.priority == 0


def test_message_type_shim() -> None:
    """MessageType can be imported from bus (backward compat)."""
    from tmux_orchestrator.bus import MessageType

    assert MessageType.TASK == "TASK"
    assert MessageType.RESULT == "RESULT"
    assert MessageType.PEER_MSG == "PEER_MSG"


def test_message_shim() -> None:
    """Message can be imported from bus (backward compat)."""
    from tmux_orchestrator.bus import Message, MessageType

    msg = Message(type=MessageType.STATUS)
    assert msg.type == MessageType.STATUS
    d = msg.to_dict()
    assert d["type"] == "STATUS"


def test_broadcast_shim() -> None:
    """BROADCAST sentinel can be imported from bus (backward compat)."""
    from tmux_orchestrator.bus import BROADCAST

    assert BROADCAST == "*"


# ------------------------------------------------------------------
# Domain package re-export tests
# ------------------------------------------------------------------


def test_domain_package_exports_agent_status() -> None:
    from tmux_orchestrator.domain import AgentStatus

    assert AgentStatus.IDLE == "IDLE"


def test_domain_package_exports_agent_role() -> None:
    from tmux_orchestrator.domain import AgentRole

    assert AgentRole.WORKER == "worker"


def test_domain_package_exports_task() -> None:
    from tmux_orchestrator.domain import Task

    t = Task(id="t1", prompt="p")
    assert t.id == "t1"


def test_domain_package_exports_message_type() -> None:
    from tmux_orchestrator.domain import MessageType

    assert MessageType.CONTROL == "CONTROL"


def test_domain_package_exports_message() -> None:
    from tmux_orchestrator.domain import Message, MessageType

    msg = Message(type=MessageType.TASK)
    assert msg.type == MessageType.TASK


def test_domain_package_exports_broadcast() -> None:
    from tmux_orchestrator.domain import BROADCAST

    assert BROADCAST == "*"


# ------------------------------------------------------------------
# Identity tests: shim and domain types must be the SAME object
# ------------------------------------------------------------------


def test_agent_status_is_same_class() -> None:
    """The shim at agents.base and domain.agent must be the same class."""
    from tmux_orchestrator.agents.base import AgentStatus as ShimStatus
    from tmux_orchestrator.domain.agent import AgentStatus as DomainStatus

    assert ShimStatus is DomainStatus


def test_agent_role_is_same_class() -> None:
    from tmux_orchestrator.config import AgentRole as ShimRole
    from tmux_orchestrator.domain.agent import AgentRole as DomainRole

    assert ShimRole is DomainRole


def test_task_is_same_class() -> None:
    from tmux_orchestrator.agents.base import Task as ShimTask
    from tmux_orchestrator.domain.task import Task as DomainTask

    assert ShimTask is DomainTask


def test_message_type_is_same_class() -> None:
    from tmux_orchestrator.bus import MessageType as ShimMT
    from tmux_orchestrator.domain.message import MessageType as DomainMT

    assert ShimMT is DomainMT


def test_message_is_same_class() -> None:
    from tmux_orchestrator.bus import Message as ShimMsg
    from tmux_orchestrator.domain.message import Message as DomainMsg

    assert ShimMsg is DomainMsg


# ------------------------------------------------------------------
# domain/workflow.py — WorkflowRun, WorkflowPhase, WorkflowStatus
# ------------------------------------------------------------------


def test_workflow_status_values() -> None:
    """WorkflowStatus has the expected string values."""
    from tmux_orchestrator.domain.workflow import WorkflowStatus

    assert WorkflowStatus.PENDING == "pending"
    assert WorkflowStatus.RUNNING == "running"
    assert WorkflowStatus.COMPLETE == "complete"
    assert WorkflowStatus.FAILED == "failed"
    assert WorkflowStatus.CANCELLED == "cancelled"


def test_workflow_run_create_factory() -> None:
    """WorkflowRun.create() assigns a UUID and correct defaults."""
    from tmux_orchestrator.domain.workflow import WorkflowRun

    run = WorkflowRun.create("my-wf", ["t1", "t2"])
    assert run.name == "my-wf"
    assert run.task_ids == ["t1", "t2"]
    assert run.status == "pending"
    assert len(run.id) == 36  # UUID format
    assert run.completed_at is None
    assert run.phases == []


def test_workflow_run_to_dict() -> None:
    """WorkflowRun.to_dict() contains expected keys."""
    from tmux_orchestrator.domain.workflow import WorkflowRun

    run = WorkflowRun.create("wf", ["a", "b", "c"])
    d = run.to_dict()
    assert d["name"] == "wf"
    assert d["task_ids"] == ["a", "b", "c"]
    assert d["status"] == "pending"
    assert d["tasks_total"] == 3
    assert d["tasks_done"] == 0
    assert d["tasks_failed"] == 0
    assert "phases" not in d  # empty phases not included


def test_workflow_phase_lifecycle() -> None:
    """WorkflowPhase transitions correctly."""
    from tmux_orchestrator.domain.workflow import WorkflowPhase

    phase = WorkflowPhase(name="analysis", pattern="single", task_ids=["t1"])
    assert phase.status == "pending"

    phase.mark_running()
    assert phase.status == "running"
    assert phase.started_at is not None

    phase.mark_complete()
    assert phase.status == "complete"
    assert phase.completed_at is not None


def test_workflow_phase_failed() -> None:
    """WorkflowPhase.mark_failed() transitions to failed."""
    from tmux_orchestrator.domain.workflow import WorkflowPhase

    phase = WorkflowPhase(name="build", pattern="parallel", task_ids=["t1", "t2"])
    phase.mark_failed()
    assert phase.status == "failed"
    assert phase.completed_at is not None


def test_workflow_phase_to_dict() -> None:
    """WorkflowPhase.to_dict() is JSON-serialisable."""
    from tmux_orchestrator.domain.workflow import WorkflowPhase

    phase = WorkflowPhase(name="test", pattern="competitive", task_ids=["x", "y"])
    d = phase.to_dict()
    assert d["name"] == "test"
    assert d["pattern"] == "competitive"
    assert d["task_ids"] == ["x", "y"]
    assert d["status"] == "pending"


def test_workflow_run_shim_same_class() -> None:
    """workflow_manager.WorkflowRun is the same class as domain.workflow.WorkflowRun."""
    from tmux_orchestrator.domain.workflow import WorkflowRun as DomainRun
    from tmux_orchestrator.workflow_manager import WorkflowRun as ShimRun

    assert ShimRun is DomainRun


def test_domain_package_exports_workflow_run() -> None:
    """domain package exports WorkflowRun."""
    from tmux_orchestrator.domain import WorkflowRun

    run = WorkflowRun.create("wf", ["t1"])
    assert run.name == "wf"


def test_domain_package_exports_workflow_status() -> None:
    """domain package exports WorkflowStatus."""
    from tmux_orchestrator.domain import WorkflowStatus

    assert WorkflowStatus.COMPLETE == "complete"


def test_domain_package_exports_workflow_phase() -> None:
    """domain package exports WorkflowPhase."""
    from tmux_orchestrator.domain import WorkflowPhase

    phase = WorkflowPhase(name="p1", pattern="single", task_ids=[])
    assert phase.name == "p1"


# ------------------------------------------------------------------
# domain/phase_strategy.py — Strategy pattern, PhaseSpec, AgentSelector
# ------------------------------------------------------------------


def test_agent_selector_defaults() -> None:
    """AgentSelector has correct defaults."""
    from tmux_orchestrator.domain.phase_strategy import AgentSelector

    sel = AgentSelector()
    assert sel.tags == []
    assert sel.count == 1
    assert sel.target_agent is None
    assert sel.target_group is None


def test_phase_spec_valid_patterns() -> None:
    """PhaseSpec accepts all valid patterns."""
    from tmux_orchestrator.domain.phase_strategy import PhaseSpec

    for pattern in ("single", "parallel", "competitive", "debate"):
        spec = PhaseSpec(name="p", pattern=pattern)  # type: ignore[arg-type]
        assert spec.pattern == pattern


def test_phase_spec_invalid_pattern() -> None:
    """PhaseSpec raises ValueError for invalid pattern."""
    import pytest

    from tmux_orchestrator.domain.phase_strategy import PhaseSpec

    with pytest.raises(ValueError, match="Invalid pattern"):
        PhaseSpec(name="p", pattern="auction")  # type: ignore[arg-type]


def test_single_strategy_produces_one_task() -> None:
    """SingleStrategy expands to exactly one task."""
    from tmux_orchestrator.domain.phase_strategy import PhaseSpec, SingleStrategy

    strategy = SingleStrategy()
    phase = PhaseSpec(name="analyse", pattern="single")
    tasks, statuses = strategy.expand(phase, [], "Do analysis", "wf/run1")
    assert len(tasks) == 1
    assert len(statuses) == 1
    assert tasks[0]["local_id"] == "phase_analyse_0"
    assert tasks[0]["depends_on"] == []


def test_parallel_strategy_count() -> None:
    """ParallelStrategy produces N tasks."""
    from tmux_orchestrator.domain.phase_strategy import AgentSelector, ParallelStrategy, PhaseSpec

    strategy = ParallelStrategy()
    phase = PhaseSpec(name="solve", pattern="parallel", agents=AgentSelector(count=3))
    tasks, statuses = strategy.expand(phase, ["prior_task"], "Solve it", "")
    assert len(tasks) == 3
    for t in tasks:
        assert t["depends_on"] == ["prior_task"]


def test_competitive_strategy_count() -> None:
    """CompetitiveStrategy produces N tasks like parallel."""
    from tmux_orchestrator.domain.phase_strategy import AgentSelector, CompetitiveStrategy, PhaseSpec

    strategy = CompetitiveStrategy()
    phase = PhaseSpec(name="compete", pattern="competitive", agents=AgentSelector(count=4))
    tasks, statuses = strategy.expand(phase, [], "Compete", "")
    assert len(tasks) == 4
    assert statuses[0].pattern == "competitive"


def test_debate_strategy_structure() -> None:
    """DebateStrategy produces advocate + critic + judge tasks."""
    from tmux_orchestrator.domain.phase_strategy import DebateStrategy, PhaseSpec

    strategy = DebateStrategy()
    phase = PhaseSpec(name="debate", pattern="debate", debate_rounds=2)
    tasks, statuses = strategy.expand(phase, [], "Debate topic", "")
    # 2 rounds × (advocate + critic) + judge = 5 tasks
    assert len(tasks) == 5
    ids = [t["local_id"] for t in tasks]
    assert "phase_debate_advocate_r1" in ids
    assert "phase_debate_critic_r1" in ids
    assert "phase_debate_advocate_r2" in ids
    assert "phase_debate_critic_r2" in ids
    assert "phase_debate_judge" in ids


def test_get_strategy_valid() -> None:
    """get_strategy returns the correct strategy instance."""
    from tmux_orchestrator.domain.phase_strategy import (
        CompetitiveStrategy,
        DebateStrategy,
        ParallelStrategy,
        SingleStrategy,
        get_strategy,
    )

    assert isinstance(get_strategy("single"), SingleStrategy)
    assert isinstance(get_strategy("parallel"), ParallelStrategy)
    assert isinstance(get_strategy("competitive"), CompetitiveStrategy)
    assert isinstance(get_strategy("debate"), DebateStrategy)


def test_get_strategy_invalid() -> None:
    """get_strategy raises ValueError for unknown patterns."""
    import pytest

    from tmux_orchestrator.domain.phase_strategy import get_strategy

    with pytest.raises(ValueError, match="Unknown phase pattern"):
        get_strategy("tournament")


def test_phase_strategy_protocol_satisfied() -> None:
    """Concrete strategies satisfy the PhaseStrategy Protocol."""
    from tmux_orchestrator.domain.phase_strategy import (
        CompetitiveStrategy,
        DebateStrategy,
        ParallelStrategy,
        PhaseStrategy,
        SingleStrategy,
    )

    for cls in (SingleStrategy, ParallelStrategy, CompetitiveStrategy, DebateStrategy):
        assert isinstance(cls(), PhaseStrategy)


def test_phase_executor_shim_re_exports() -> None:
    """phase_executor shim re-exports all canonical types from domain."""
    from tmux_orchestrator.domain.phase_strategy import AgentSelector as DomainAgentSelector
    from tmux_orchestrator.domain.phase_strategy import PhaseSpec as DomainPhaseSpec
    from tmux_orchestrator.domain.phase_strategy import WorkflowPhaseStatus as DomainWPS
    from tmux_orchestrator.phase_executor import AgentSelector as ShimAgentSelector
    from tmux_orchestrator.phase_executor import PhaseSpec as ShimPhaseSpec
    from tmux_orchestrator.phase_executor import WorkflowPhaseStatus as ShimWPS

    assert ShimAgentSelector is DomainAgentSelector
    assert ShimPhaseSpec is DomainPhaseSpec
    assert ShimWPS is DomainWPS


def test_phase_executor_expand_phases_works() -> None:
    """phase_executor.expand_phases still works via shim."""
    from tmux_orchestrator.phase_executor import PhaseSpec, expand_phases

    phases = [
        PhaseSpec(name="step1", pattern="single"),
        PhaseSpec(name="step2", pattern="single"),
    ]
    tasks = expand_phases(phases, context="Do the work")
    assert len(tasks) == 2
    assert tasks[1]["depends_on"] == ["phase_step1_0"]


def test_domain_package_exports_phase_strategy_types() -> None:
    """domain package exports PhaseStrategy, PhaseSpec, AgentSelector, strategies."""
    from tmux_orchestrator.domain import (
        AgentSelector,
        CompetitiveStrategy,
        DebateStrategy,
        ParallelStrategy,
        PhaseSpec,
        PhaseStrategy,
        SingleStrategy,
        WorkflowPhaseStatus,
        get_strategy,
    )

    assert AgentSelector is not None
    assert PhaseSpec is not None
    assert PhaseStrategy is not None
    assert SingleStrategy is not None
    assert ParallelStrategy is not None
    assert CompetitiveStrategy is not None
    assert DebateStrategy is not None
    assert WorkflowPhaseStatus is not None
    assert callable(get_strategy)
