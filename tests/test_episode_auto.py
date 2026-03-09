"""Tests for episodic memory auto-record + auto-inject (v1.0.29).

Covers:
- Auto-record: explicit /task-complete → episode appended to store
- Auto-record disabled: memory_auto_record=False → no episode created
- Auto-record: empty output → no episode created
- Auto-inject: memory_inject_count > 0 → episodes prepended to task prompt
- Auto-inject disabled: memory_inject_count=0 → prompt unchanged
- Auto-inject: no episodes in store → prompt unchanged
- Episode format in injected prompt (section header, numbered list, separator)
- OrchestratorConfig defaults for memory_auto_record and memory_inject_count
- Config load_config picks up YAML values

Design reference: DESIGN.md §10.29 (v1.0.29);
  Wang & Chen "MIRIX" arXiv:2507.07957 (2025);
  "PlugMem" arXiv:2603.03296 (2025);
  "Design Patterns for Long-Term Memory" Serokell Blog (2025).
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tmux_orchestrator.agents.base import AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig, load_config
from tmux_orchestrator.episode_store import EpisodeStore
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app

try:
    from tests.integration.test_orchestration import HeadlessAgent
except ImportError:
    from integration.test_orchestration import HeadlessAgent  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_API_KEY = "test-auto-ep-key"


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test-auto",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


def make_tmux_mock():
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


class _StubHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _make_app(tmp_path, **config_kwargs):
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(mailbox_dir=str(tmp_path), **config_kwargs)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    app = create_app(orch, _StubHub(), api_key=_API_KEY)  # type: ignore[arg-type]
    return app, orch


def auth_headers() -> dict:
    return {"X-API-Key": _API_KEY}


# ---------------------------------------------------------------------------
# OrchestratorConfig defaults
# ---------------------------------------------------------------------------


def test_config_memory_auto_record_default():
    """memory_auto_record defaults to True."""
    cfg = OrchestratorConfig()
    assert cfg.memory_auto_record is True


def test_config_memory_inject_count_default():
    """memory_inject_count defaults to 5."""
    cfg = OrchestratorConfig()
    assert cfg.memory_inject_count == 5


def test_config_memory_auto_record_override():
    cfg = OrchestratorConfig(memory_auto_record=False)
    assert cfg.memory_auto_record is False


def test_config_memory_inject_count_override():
    cfg = OrchestratorConfig(memory_inject_count=0)
    assert cfg.memory_inject_count == 0


# ---------------------------------------------------------------------------
# load_config YAML parsing
# ---------------------------------------------------------------------------


def test_load_config_memory_fields(tmp_path):
    """load_config correctly parses memory_auto_record and memory_inject_count."""
    yaml_content = textwrap.dedent("""\
        session_name: test
        agents: []
        memory_auto_record: false
        memory_inject_count: 3
    """)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(cfg_file)
    assert cfg.memory_auto_record is False
    assert cfg.memory_inject_count == 3


def test_load_config_memory_defaults(tmp_path):
    """load_config uses defaults when memory fields are absent."""
    yaml_content = textwrap.dedent("""\
        session_name: test
        agents: []
    """)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_content)
    cfg = load_config(cfg_file)
    assert cfg.memory_auto_record is True
    assert cfg.memory_inject_count == 5


# ---------------------------------------------------------------------------
# Auto-record: task-complete endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def setup(tmp_path):
    app, orch = _make_app(tmp_path)
    agent = HeadlessAgent("worker-1", orch.bus)
    orch.register_agent(agent)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, orch


def _set_busy(orch, agent_id: str, task_id: str = "task-001") -> None:
    """Force agent into BUSY state with a dummy current task."""
    agent = orch.get_agent(agent_id)
    task = Task(id=task_id, prompt="do something")
    agent._current_task = task
    agent.status = AgentStatus.BUSY


def test_auto_record_creates_episode_on_task_complete(setup, tmp_path):
    """Explicit /task-complete auto-records an episode."""
    client, orch = setup
    _set_busy(orch, "worker-1", "task-abc")

    resp = client.post(
        "/agents/worker-1/task-complete",
        json={"output": "implemented merge_sort.py"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Verify episode was recorded
    mem_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    assert mem_resp.status_code == 200
    episodes = mem_resp.json()
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep["summary"] == "implemented merge_sort.py"
    assert ep["outcome"] == "success"
    assert ep["agent_id"] == "worker-1"
    assert ep["task_id"] == "task-abc"


def test_auto_record_empty_output_no_episode(setup):
    """Empty output string → no episode recorded."""
    client, orch = setup
    _set_busy(orch, "worker-1", "task-empty")

    resp = client.post(
        "/agents/worker-1/task-complete",
        json={"output": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    mem_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    assert mem_resp.json() == []


def test_auto_record_disabled_no_episode(tmp_path):
    """memory_auto_record=False → no episode created on task-complete."""
    app, orch = _make_app(tmp_path, memory_auto_record=False)
    agent = HeadlessAgent("worker-1", orch.bus)
    orch.register_agent(agent)
    with TestClient(app, raise_server_exceptions=True) as client:
        _set_busy(orch, "worker-1", "task-disabled")

        resp = client.post(
            "/agents/worker-1/task-complete",
            json={"output": "done"},
            headers=auth_headers(),
        )
        assert resp.status_code == 200

        mem_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
        assert mem_resp.json() == []


def test_auto_record_stop_hook_nudge_no_episode(setup):
    """Stop-hook nudge (stop_hook_active=false) does NOT create an episode."""
    client, orch = setup
    _set_busy(orch, "worker-1", "task-nudge")

    resp = client.post(
        "/agents/worker-1/task-complete",
        json={"stop_hook_active": False},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "nudged"

    mem_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    assert mem_resp.json() == []


def test_auto_record_summary_truncated_at_500_chars(setup):
    """Long output is truncated to 500 chars in the episode summary."""
    client, orch = setup
    _set_busy(orch, "worker-1", "task-long")
    long_output = "x" * 800

    client.post(
        "/agents/worker-1/task-complete",
        json={"output": long_output},
        headers=auth_headers(),
    )

    mem_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    ep = mem_resp.json()[0]
    assert len(ep["summary"]) == 500


def test_auto_record_multiple_tasks(setup):
    """Multiple task completions create multiple episodes, newest-first."""
    client, orch = setup

    for i in range(3):
        _set_busy(orch, "worker-1", f"task-{i}")
        client.post(
            "/agents/worker-1/task-complete",
            json={"output": f"completed task {i}"},
            headers=auth_headers(),
        )

    mem_resp = client.get("/agents/worker-1/memory", headers=auth_headers())
    episodes = mem_resp.json()
    assert len(episodes) == 3
    # Newest-first
    assert "task 2" in episodes[0]["summary"]
    assert "task 0" in episodes[2]["summary"]


# ---------------------------------------------------------------------------
# Auto-inject: orchestrator dispatch loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_inject_prepends_episodes_to_prompt(tmp_path):
    """Episode inject: prompt gets 'past episodes' prefix when store has entries."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = OrchestratorConfig(
        session_name="inj-test",
        mailbox_dir=str(tmp_path),
        memory_inject_count=3,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    # Populate episode store
    store = EpisodeStore(root_dir=str(tmp_path), session_name="inj-test")
    store.append("worker-x", summary="wrote sort.py", outcome="success")
    store.append("worker-x", summary="wrote tests.py", outcome="success")
    orch._episode_store = store

    # Capture the task that arrives at send_task
    received_prompts = []

    class CaptureAgent(HeadlessAgent):
        async def send_task(self, task: Task) -> None:
            received_prompts.append(task.prompt)
            # Don't actually enqueue — we just capture.

    agent = CaptureAgent("worker-x", bus)
    agent.status = AgentStatus.IDLE
    orch.registry.register(agent)

    # Submit task and run dispatch loop briefly
    await orch.submit_task("original prompt", _task_id="t1")
    dispatch = asyncio.create_task(orch._dispatch_loop())
    await asyncio.sleep(0.1)
    dispatch.cancel()
    try:
        await dispatch
    except asyncio.CancelledError:
        pass

    assert len(received_prompts) == 1
    prompt = received_prompts[0]
    assert "過去のタスク経験" in prompt
    assert "wrote sort.py" in prompt
    assert "wrote tests.py" in prompt
    assert "original prompt" in prompt
    # Original prompt should come after the prefix
    assert prompt.index("過去のタスク経験") < prompt.index("original prompt")


@pytest.mark.asyncio
async def test_auto_inject_disabled_prompt_unchanged(tmp_path):
    """memory_inject_count=0 → prompt is not modified."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = OrchestratorConfig(
        session_name="inj-off",
        mailbox_dir=str(tmp_path),
        memory_inject_count=0,  # disabled
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    store = EpisodeStore(root_dir=str(tmp_path), session_name="inj-off")
    store.append("worker-y", summary="wrote foo.py", outcome="success")
    orch._episode_store = store

    received_prompts = []

    class CaptureAgent(HeadlessAgent):
        async def send_task(self, task: Task) -> None:
            received_prompts.append(task.prompt)

    agent = CaptureAgent("worker-y", bus)
    agent.status = AgentStatus.IDLE
    orch.registry.register(agent)

    await orch.submit_task("clean prompt", _task_id="t2")
    dispatch = asyncio.create_task(orch._dispatch_loop())
    await asyncio.sleep(0.1)
    dispatch.cancel()
    try:
        await dispatch
    except asyncio.CancelledError:
        pass

    assert len(received_prompts) == 1
    assert received_prompts[0] == "clean prompt"


@pytest.mark.asyncio
async def test_auto_inject_no_store_prompt_unchanged(tmp_path):
    """No episode store → prompt not modified even if inject_count > 0."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = OrchestratorConfig(
        session_name="no-store",
        mailbox_dir=str(tmp_path),
        memory_inject_count=5,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    # _episode_store stays None (default)

    received_prompts = []

    class CaptureAgent(HeadlessAgent):
        async def send_task(self, task: Task) -> None:
            received_prompts.append(task.prompt)

    agent = CaptureAgent("worker-z", bus)
    agent.status = AgentStatus.IDLE
    orch.registry.register(agent)

    await orch.submit_task("bare prompt", _task_id="t3")
    dispatch = asyncio.create_task(orch._dispatch_loop())
    await asyncio.sleep(0.1)
    dispatch.cancel()
    try:
        await dispatch
    except asyncio.CancelledError:
        pass

    assert len(received_prompts) == 1
    assert received_prompts[0] == "bare prompt"


@pytest.mark.asyncio
async def test_auto_inject_no_episodes_prompt_unchanged(tmp_path):
    """Store exists but agent has no episodes → prompt unchanged."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = OrchestratorConfig(
        session_name="empty-store",
        mailbox_dir=str(tmp_path),
        memory_inject_count=5,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    store = EpisodeStore(root_dir=str(tmp_path), session_name="empty-store")
    orch._episode_store = store  # empty store, no episodes for this agent

    received_prompts = []

    class CaptureAgent(HeadlessAgent):
        async def send_task(self, task: Task) -> None:
            received_prompts.append(task.prompt)

    agent = CaptureAgent("worker-q", bus)
    agent.status = AgentStatus.IDLE
    orch.registry.register(agent)

    await orch.submit_task("empty store prompt", _task_id="t4")
    dispatch = asyncio.create_task(orch._dispatch_loop())
    await asyncio.sleep(0.1)
    dispatch.cancel()
    try:
        await dispatch
    except asyncio.CancelledError:
        pass

    assert len(received_prompts) == 1
    assert received_prompts[0] == "empty store prompt"


@pytest.mark.asyncio
async def test_auto_inject_respects_inject_count(tmp_path):
    """Only the N most recent episodes are injected (inject_count=2 of 4 total)."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = OrchestratorConfig(
        session_name="count-test",
        mailbox_dir=str(tmp_path),
        memory_inject_count=2,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    store = EpisodeStore(root_dir=str(tmp_path), session_name="count-test")
    for i in range(4):
        store.append("agent-c", summary=f"task {i}", outcome="success")
    orch._episode_store = store

    received_prompts = []

    class CaptureAgent(HeadlessAgent):
        async def send_task(self, task: Task) -> None:
            received_prompts.append(task.prompt)

    agent = CaptureAgent("agent-c", bus)
    agent.status = AgentStatus.IDLE
    orch.registry.register(agent)

    await orch.submit_task("count test", _task_id="t5")
    dispatch = asyncio.create_task(orch._dispatch_loop())
    await asyncio.sleep(0.1)
    dispatch.cancel()
    try:
        await dispatch
    except asyncio.CancelledError:
        pass

    assert len(received_prompts) == 1
    prompt = received_prompts[0]
    # Should contain task 3 and task 2 (newest 2), but NOT task 0 or task 1
    assert "task 3" in prompt
    assert "task 2" in prompt
    assert "task 0" not in prompt


@pytest.mark.asyncio
async def test_auto_inject_format_section_header(tmp_path):
    """Injected prefix contains the correct section header."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = OrchestratorConfig(
        session_name="fmt-test",
        mailbox_dir=str(tmp_path),
        memory_inject_count=5,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)

    store = EpisodeStore(root_dir=str(tmp_path), session_name="fmt-test")
    store.append("agent-f", summary="did x", outcome="success")
    orch._episode_store = store

    received_prompts = []

    class CaptureAgent(HeadlessAgent):
        async def send_task(self, task: Task) -> None:
            received_prompts.append(task.prompt)

    agent = CaptureAgent("agent-f", bus)
    agent.status = AgentStatus.IDLE
    orch.registry.register(agent)

    await orch.submit_task("fmt test", _task_id="t6")
    dispatch = asyncio.create_task(orch._dispatch_loop())
    await asyncio.sleep(0.1)
    dispatch.cancel()
    try:
        await dispatch
    except asyncio.CancelledError:
        pass

    assert len(received_prompts) == 1
    prompt = received_prompts[0]
    assert "## 過去のタスク経験" in prompt
    assert "---" in prompt
    assert "outcome: success" in prompt


# ---------------------------------------------------------------------------
# Episode store shared between web layer and orchestrator
# ---------------------------------------------------------------------------


def test_episode_store_shared_with_orchestrator(tmp_path):
    """create_app() sets orchestrator._episode_store to the same store instance."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(mailbox_dir=str(tmp_path))
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    app = create_app(orch, _StubHub(), api_key=_API_KEY)  # type: ignore[arg-type]

    assert orch._episode_store is not None
    assert isinstance(orch._episode_store, EpisodeStore)
