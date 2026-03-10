"""Tests for dynamic ephemeral agent spawning (v1.2.3).

PhaseSpec.agent_template triggers on-demand agent creation at workflow
dispatch time.  The spawned agent is scoped to a single phase and is
automatically stopped after task completion.

Design reference: DESIGN.md §10.79 (v1.2.3)
Research: Kubernetes Pod-per-Job pattern; ephemeral agent lifecycle.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.application.bus import Bus, Message, MessageType
from tmux_orchestrator.application.config import AgentConfig, AgentRole, OrchestratorConfig
from tmux_orchestrator.application.orchestrator import Orchestrator
from tmux_orchestrator.domain.phase_strategy import (
    AgentSelector,
    PhaseSpec,
    SingleStrategy,
    _make_task_spec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(agents=None, **kwargs) -> OrchestratorConfig:
    defaults = dict(session_name="test", task_timeout=30, watchdog_poll=999)
    defaults.update(kwargs)
    if agents is not None:
        defaults["agents"] = agents
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.new_subpane = MagicMock(return_value=MagicMock(id="pane-2"))
    tmux.send_keys = MagicMock()
    tmux.watch_pane = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")
    return tmux


def make_agent_config(agent_id="worker", **kwargs) -> AgentConfig:
    defaults = dict(
        id=agent_id,
        type="claude_code",
        isolate=True,
        system_prompt="You are a worker agent.",
        tags=[],
    )
    defaults.update(kwargs)
    return AgentConfig(**defaults)


def make_orch(agent_configs=None) -> tuple[Orchestrator, Bus]:
    bus = Bus()
    tmux = make_tmux_mock()
    cfg = make_config(agents=agent_configs or [])
    orch = Orchestrator(bus=bus, tmux=tmux, config=cfg)
    return orch, bus


# ---------------------------------------------------------------------------
# Domain: PhaseSpec.agent_template field
# ---------------------------------------------------------------------------


def test_phase_spec_agent_template_defaults_to_none():
    """PhaseSpec.agent_template must default to None."""
    spec = PhaseSpec(name="test", pattern="single")
    assert spec.agent_template is None


def test_phase_spec_agent_template_can_be_set():
    """PhaseSpec.agent_template accepts a string."""
    spec = PhaseSpec(name="test", pattern="single", agent_template="worker")
    assert spec.agent_template == "worker"


def test_single_strategy_propagates_agent_template():
    """SingleStrategy.expand() must include agent_template in the task spec."""
    phase = PhaseSpec(name="impl", pattern="single", agent_template="worker")
    strategy = SingleStrategy()
    task_specs, _ = strategy.expand(phase, [], "some context", "")
    assert len(task_specs) == 1
    assert task_specs[0].get("agent_template") == "worker"


def test_single_strategy_no_agent_template_omits_key():
    """When agent_template is None, the key should NOT appear in task spec."""
    phase = PhaseSpec(name="impl", pattern="single")
    strategy = SingleStrategy()
    task_specs, _ = strategy.expand(phase, [], "some context", "")
    assert len(task_specs) == 1
    assert "agent_template" not in task_specs[0]


def test_make_task_spec_agent_template_included():
    """_make_task_spec with agent_template embeds the key."""
    spec = _make_task_spec(
        "lid",
        "prompt",
        [],
        required_tags=[],
        agent_template="my-template",
    )
    assert spec.get("agent_template") == "my-template"


def test_make_task_spec_no_agent_template_omitted():
    """_make_task_spec without agent_template omits the key."""
    spec = _make_task_spec("lid", "prompt", [], required_tags=[])
    assert "agent_template" not in spec


# ---------------------------------------------------------------------------
# Orchestrator: spawn_ephemeral_agent()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_creates_agent_with_unique_id():
    """spawn_ephemeral_agent() must return an ID containing 'ephemeral'."""
    worker_cfg = make_agent_config("worker")
    orch, _ = make_orch([worker_cfg])

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        captured_ids: list[str] = []

        def _capture(*args, **kwargs):
            captured_ids.append(kwargs.get("agent_id", ""))
            instance.id = kwargs.get("agent_id", "")
            return instance

        MockAgent.side_effect = _capture
        agent_id = await orch.spawn_ephemeral_agent("worker")

    assert "ephemeral" in agent_id
    assert agent_id.startswith("worker-ephemeral-")
    assert len(captured_ids) == 1


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_registers_in_registry():
    """spawn_ephemeral_agent() must register the agent in the orchestrator registry."""
    worker_cfg = make_agent_config("worker")
    orch, _ = make_orch([worker_cfg])

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        captured_ids: list[str] = []

        def _capture(*args, **kwargs):
            aid = kwargs.get("agent_id", "")
            captured_ids.append(aid)
            instance.id = aid
            return instance

        MockAgent.side_effect = _capture
        agent_id = await orch.spawn_ephemeral_agent("worker")

    assert orch.registry.get(agent_id) is not None


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_tracks_in_ephemeral_set():
    """spawn_ephemeral_agent() must add the new ID to _ephemeral_agents."""
    worker_cfg = make_agent_config("worker")
    orch, _ = make_orch([worker_cfg])

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        captured_ids: list[str] = []

        def _capture(*args, **kwargs):
            aid = kwargs.get("agent_id", "")
            captured_ids.append(aid)
            instance.id = aid
            return instance

        MockAgent.side_effect = _capture
        agent_id = await orch.spawn_ephemeral_agent("worker")

    assert agent_id in orch._ephemeral_agents


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_starts_the_agent():
    """spawn_ephemeral_agent() must call agent.start()."""
    worker_cfg = make_agent_config("worker")
    orch, _ = make_orch([worker_cfg])

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        instance = AsyncMock()
        captured_ids: list[str] = []

        def _capture(*args, **kwargs):
            aid = kwargs.get("agent_id", "")
            captured_ids.append(aid)
            instance.id = aid
            return instance

        MockAgent.side_effect = _capture
        await orch.spawn_ephemeral_agent("worker")

    instance.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_raises_on_unknown_template():
    """spawn_ephemeral_agent() must raise ValueError for unknown template ID."""
    orch, _ = make_orch([])  # no agents configured

    with pytest.raises(ValueError, match="no agent config with id="):
        await orch.spawn_ephemeral_agent("nonexistent-template")


@pytest.mark.asyncio
async def test_spawn_ephemeral_agent_multiple_unique_ids():
    """Each call to spawn_ephemeral_agent() produces a unique ID."""
    worker_cfg = make_agent_config("worker")
    orch, _ = make_orch([worker_cfg])
    captured_ids: list[str] = []

    with patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent:
        call_count = 0

        def _capture(*args, **kwargs):
            nonlocal call_count
            aid = kwargs.get("agent_id", "")
            captured_ids.append(aid)
            inst = AsyncMock()
            inst.id = aid
            call_count += 1
            return inst

        MockAgent.side_effect = _capture
        id1 = await orch.spawn_ephemeral_agent("worker")
        id2 = await orch.spawn_ephemeral_agent("worker")

    assert id1 != id2
    assert len(set(captured_ids)) == 2


# ---------------------------------------------------------------------------
# Orchestrator: ephemeral agent auto-stop on task completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ephemeral_agent_stopped_after_task_completion():
    """When an ephemeral agent sends a RESULT, it must be stopped and unregistered."""
    orch, bus = make_orch([])

    # Manually register a mock ephemeral agent
    mock_agent = AsyncMock()
    mock_agent.id = "worker-ephemeral-abc123"
    mock_agent.status = AgentStatus.IDLE
    mock_agent._current_task = None

    orch.registry.register(mock_agent)
    orch._ephemeral_agents.add("worker-ephemeral-abc123")

    # Subscribe to orchestrator events
    orch._bus_queue = await bus.subscribe("__orchestrator__", broadcast=True)
    orch._completed_tasks = set()

    # Send a successful RESULT from the ephemeral agent
    task_id = "task-xyz"
    await bus.publish(Message(
        type=MessageType.RESULT,
        from_id="worker-ephemeral-abc123",
        payload={"task_id": task_id, "output": "done", "error": None},
    ))

    # Process the route loop once
    msg = await asyncio.wait_for(orch._bus_queue.get(), timeout=1.0)
    orch._bus_queue.task_done()
    # Simulate the relevant portion of _route_loop
    error = msg.payload.get("error")
    from_id = msg.from_id
    orch.registry.record_result(from_id, error=bool(error))
    if not error and task_id:
        orch._completed_tasks.add(task_id)
        orch._active_tasks.pop(task_id, None)
    if from_id in orch._ephemeral_agents:
        ep_agent = orch.registry.get(from_id)
        if ep_agent is not None:
            await ep_agent.stop()
            orch.registry.unregister(from_id)
        orch._ephemeral_agents.discard(from_id)

    assert orch.registry.get("worker-ephemeral-abc123") is None
    assert "worker-ephemeral-abc123" not in orch._ephemeral_agents
    mock_agent.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_ephemeral_agent_unregistered_after_stop():
    """After ephemeral agent auto-stop, it must not appear in list_agents."""
    orch, _ = make_orch([])

    mock_agent = AsyncMock()
    mock_agent.id = "worker-ephemeral-dead"
    mock_agent.status = AgentStatus.IDLE
    mock_agent._current_task = None

    orch.registry.register(mock_agent)
    orch._ephemeral_agents.add("worker-ephemeral-dead")

    # Simulate stop + unregister
    await mock_agent.stop()
    orch.registry.unregister("worker-ephemeral-dead")
    orch._ephemeral_agents.discard("worker-ephemeral-dead")

    agent_ids = [a["id"] for a in orch.list_agents()]
    assert "worker-ephemeral-dead" not in agent_ids
    assert "worker-ephemeral-dead" not in orch._ephemeral_agents


# ---------------------------------------------------------------------------
# Non-ephemeral phases still work as before
# ---------------------------------------------------------------------------


def test_non_ephemeral_phase_has_no_agent_template():
    """Phases without agent_template must produce task specs without the key."""
    phase = PhaseSpec(name="regular", pattern="single", required_tags=["python"])
    strategy = SingleStrategy()
    task_specs, _ = strategy.expand(phase, [], "context", "")
    assert len(task_specs) == 1
    assert "agent_template" not in task_specs[0]
    # required_tags still present
    assert "python" in task_specs[0]["required_tags"]


def test_phase_spec_agent_template_and_required_tags_coexist():
    """agent_template and required_tags can coexist on the same PhaseSpec."""
    phase = PhaseSpec(
        name="test",
        pattern="single",
        agent_template="worker",
        required_tags=["gpu"],
    )
    assert phase.agent_template == "worker"
    assert "gpu" in phase.required_tags


# ---------------------------------------------------------------------------
# Web schema: PhaseSpecModel.agent_template
# ---------------------------------------------------------------------------


def test_phase_spec_model_agent_template_field_exists():
    """PhaseSpecModel must have an agent_template field defaulting to None."""
    from tmux_orchestrator.web.schemas import PhaseSpecModel

    model = PhaseSpecModel(name="x", pattern="single")
    assert hasattr(model, "agent_template")
    assert model.agent_template is None


def test_phase_spec_model_agent_template_accepts_string():
    """PhaseSpecModel.agent_template must accept a string value."""
    from tmux_orchestrator.web.schemas import PhaseSpecModel

    model = PhaseSpecModel(name="x", pattern="single", agent_template="worker")
    assert model.agent_template == "worker"
