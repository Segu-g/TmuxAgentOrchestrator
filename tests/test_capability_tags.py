"""Tests for capability tag-based task routing (Option C).

When a Task has ``required_tags`` set, the dispatch loop MUST route
the task ONLY to agents whose ``tags`` include ALL required tags.

Design reference:
- FIPA Agent Communication Language — Directory Facilitator (DF) service
  discovery by capability advertisement (2002):
  https://smythos.com/developers/agent-development/fipa-agent-communication-language/
- Kubernetes nodeSelector / Node Affinity — label-based workload routing:
  https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/
- COLA: Collaborative Multi-Agent Framework with Dynamic Collaboration
  (EMNLP 2025) — scenario-aware agent selection via capability matching:
  https://aclanthology.org/2025.emnlp-main.227.pdf
- DESIGN.md §10.14 (v0.18.0, 2026-03-05)

Semantics:
- ``AgentConfig.tags: list[str]`` — capabilities advertised by an agent.
- ``Task.required_tags: list[str]`` — all must be present in target agent's tags.
- ``find_idle_worker(required_tags)`` returns first IDLE worker where
  ``set(required_tags) <= set(agent.tags)``.
- Tasks with empty required_tags match any idle worker (backwards-compatible).
- Tasks with required_tags that no agent can satisfy go to DLQ after
  dlq_max_retries, just like target_agent for a busy agent.
- The REST API accepts ``required_tags: list[str]`` in POST /tasks.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus, Message, MessageType
from tmux_orchestrator.config import AgentConfig, OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.registry import AgentRegistry
from tmux_orchestrator.web.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**kwargs) -> OrchestratorConfig:
    defaults = dict(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
    )
    defaults.update(kwargs)
    return OrchestratorConfig(**defaults)


class TaggedDummyAgent(Agent):
    """In-process stub that records dispatched tasks and reports capability tags."""

    def __init__(self, agent_id: str, bus: Bus, tags: list[str] | None = None) -> None:
        super().__init__(agent_id, bus)
        self.tags: list[str] = tags or []
        self.dispatched: list[Task] = []
        self.dispatched_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop(), name=f"{self.id}-loop")

    async def stop(self) -> None:
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task: Task) -> None:
        self.dispatched.append(task)
        self.dispatched_event.set()
        await asyncio.sleep(0.01)
        task_id = task.id
        await self.bus.publish(
            Message(type=MessageType.RESULT, from_id=self.id,
                    payload={"task_id": task_id, "output": "done"})
        )
        self._set_idle()

    async def handle_output(self, text: str) -> None:
        pass

    async def notify_stdin(self, notification: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Unit tests: Task dataclass
# ---------------------------------------------------------------------------


def test_task_has_required_tags_field():
    """Task.required_tags defaults to empty list."""
    task = Task(id="t1", prompt="hello")
    assert task.required_tags == []


def test_task_required_tags_stored():
    task = Task(id="t1", prompt="hello", required_tags=["python", "testing"])
    assert task.required_tags == ["python", "testing"]


# ---------------------------------------------------------------------------
# Unit tests: AgentConfig.tags
# ---------------------------------------------------------------------------


def test_agent_config_has_tags_field():
    cfg = AgentConfig(id="a1", type="claude_code", tags=["python"])
    assert cfg.tags == ["python"]


def test_agent_config_tags_defaults_to_empty():
    cfg = AgentConfig(id="a1", type="claude_code")
    assert cfg.tags == []


# ---------------------------------------------------------------------------
# Unit tests: AgentRegistry.find_idle_worker with required_tags
# ---------------------------------------------------------------------------


def _make_registry() -> AgentRegistry:
    return AgentRegistry(p2p_permissions=[], circuit_breaker_threshold=3, circuit_breaker_recovery=60.0)


def _make_idle_tagged_agent(agent_id: str, bus: Bus, tags: list[str]) -> TaggedDummyAgent:
    agent = TaggedDummyAgent(agent_id, bus, tags=tags)
    agent.status = AgentStatus.IDLE
    return agent


def test_find_idle_worker_no_tags_matches_any():
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=["python"])
    reg.register(agent)
    result = reg.find_idle_worker(required_tags=[])
    assert result is agent


def test_find_idle_worker_required_tags_matches():
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=["python", "testing"])
    reg.register(agent)
    result = reg.find_idle_worker(required_tags=["python"])
    assert result is agent


def test_find_idle_worker_required_tags_all_must_match():
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=["python", "testing"])
    reg.register(agent)
    result = reg.find_idle_worker(required_tags=["python", "testing"])
    assert result is agent


def test_find_idle_worker_missing_tag_skips_agent():
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=["python"])
    reg.register(agent)
    result = reg.find_idle_worker(required_tags=["golang"])
    assert result is None


def test_find_idle_worker_partial_match_skips_agent():
    """Agent has [python] but task requires [python, testing] — must be skipped."""
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=["python"])
    reg.register(agent)
    result = reg.find_idle_worker(required_tags=["python", "testing"])
    assert result is None


def test_find_idle_worker_selects_capable_agent():
    """Only the agent with required tags should be selected."""
    bus = Bus()
    reg = _make_registry()
    agent_a = _make_idle_tagged_agent("a1", bus, tags=["python"])
    agent_b = _make_idle_tagged_agent("a2", bus, tags=["golang"])
    reg.register(agent_a)
    reg.register(agent_b)
    result = reg.find_idle_worker(required_tags=["golang"])
    assert result is agent_b


def test_find_idle_worker_no_required_tags_backwards_compatible():
    """Empty required_tags behaves the same as current find_idle_worker()."""
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=[])
    reg.register(agent)
    result = reg.find_idle_worker(required_tags=[])
    assert result is agent


# ---------------------------------------------------------------------------
# Integration tests: Orchestrator dispatch with required_tags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_dispatches_to_tagged_agent():
    """Task with required_tags goes to the correctly tagged agent."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus, MagicMock(), config)

    py_agent = TaggedDummyAgent("python-expert", bus, tags=["python", "testing"])
    doc_agent = TaggedDummyAgent("docs-writer", bus, tags=["markdown", "documentation"])
    orch.register_agent(py_agent)
    orch.register_agent(doc_agent)
    await orch.start()

    task = await orch.submit_task(
        "Write unit tests for the knapsack solver",
        required_tags=["python", "testing"],
    )
    await asyncio.sleep(0.3)

    # Python expert should have received the task; docs writer should be untouched
    assert any(t.id == task.id for t in py_agent.dispatched), \
        "python-expert should have received the task"
    assert not any(t.id == task.id for t in doc_agent.dispatched), \
        "docs-writer should NOT have received the task"

    await orch.stop()


@pytest.mark.asyncio
async def test_task_dispatches_to_correct_specialist():
    """Two tasks with different required_tags go to the right specialists."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus, MagicMock(), config)

    py_agent = TaggedDummyAgent("python-expert", bus, tags=["python", "testing"])
    doc_agent = TaggedDummyAgent("docs-writer", bus, tags=["markdown", "documentation"])
    orch.register_agent(py_agent)
    orch.register_agent(doc_agent)
    await orch.start()

    t1 = await orch.submit_task("Write tests", required_tags=["python", "testing"])
    t2 = await orch.submit_task("Write README.md", required_tags=["markdown", "documentation"])
    await asyncio.sleep(0.5)

    assert any(t.id == t1.id for t in py_agent.dispatched)
    assert any(t.id == t2.id for t in doc_agent.dispatched)
    # Cross-check: no cross-dispatch
    assert not any(t.id == t2.id for t in py_agent.dispatched)
    assert not any(t.id == t1.id for t in doc_agent.dispatched)

    await orch.stop()


@pytest.mark.asyncio
async def test_task_no_capable_agent_goes_to_dlq():
    """Task requiring tags no agent has is dead-lettered after max retries."""
    bus = Bus()
    config = make_config(dlq_max_retries=2)
    orch = Orchestrator(bus, MagicMock(), config)

    agent = TaggedDummyAgent("py-agent", bus, tags=["python"])
    orch.register_agent(agent)
    await orch.start()

    task = await orch.submit_task("Rust work", required_tags=["rust"])
    await asyncio.sleep(1.0)

    dlq = orch.list_dlq()
    assert len(dlq) > 0
    assert dlq[0]["task_id"] == task.id
    assert "required_tags" in dlq[0]["reason"].lower() or "tag" in dlq[0]["reason"].lower()

    await orch.stop()


@pytest.mark.asyncio
async def test_task_without_required_tags_dispatches_to_any_idle():
    """Tasks without required_tags still work with any idle worker."""
    bus = Bus()
    config = make_config()
    orch = Orchestrator(bus, MagicMock(), config)

    agent = TaggedDummyAgent("gen-agent", bus, tags=["python"])
    orch.register_agent(agent)
    await orch.start()

    task = await orch.submit_task("any work")
    await asyncio.sleep(0.3)

    assert any(t.id == task.id for t in agent.dispatched)

    await orch.stop()


# ---------------------------------------------------------------------------
# REST API tests
# ---------------------------------------------------------------------------

_API_KEY = "tag-test-key"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _MockOrchestrator:
    _dispatch_task = None

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        return None

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False

    async def submit_task(self, prompt, **kwargs):
        required_tags = kwargs.pop("required_tags", [])
        t = Task(id="fake-id", prompt=prompt, required_tags=required_tags,
                 **{k: v for k, v in kwargs.items() if v is not None})
        return t

    @property
    def bus(self):
        b = MagicMock()
        b.subscribe = AsyncMock(return_value=MagicMock())
        b.unsubscribe = AsyncMock()
        return b


@pytest.fixture
def web_client():
    orch = _MockOrchestrator()
    hub = _MockHub()
    app = create_app(orch, hub, api_key=_API_KEY)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_post_task_accepts_required_tags(web_client):
    resp = web_client.post(
        "/tasks",
        json={"prompt": "write tests", "required_tags": ["python", "testing"]},
        headers={"X-API-Key": _API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "task_id" in body


def test_post_task_required_tags_in_response(web_client):
    resp = web_client.post(
        "/tasks",
        json={"prompt": "write tests", "required_tags": ["python"]},
        headers={"X-API-Key": _API_KEY},
    )
    body = resp.json()
    assert body.get("required_tags") == ["python"]


def test_post_task_no_required_tags_omitted(web_client):
    resp = web_client.post(
        "/tasks",
        json={"prompt": "any work"},
        headers={"X-API-Key": _API_KEY},
    )
    body = resp.json()
    assert body.get("required_tags", []) == []


def test_post_task_empty_required_tags(web_client):
    resp = web_client.post(
        "/tasks",
        json={"prompt": "any work", "required_tags": []},
        headers={"X-API-Key": _API_KEY},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# YAML config loading: tags field
# ---------------------------------------------------------------------------


def test_load_config_with_tags(tmp_path):
    """AgentConfig.tags is loaded from YAML."""
    from tmux_orchestrator.config import load_config

    yaml_content = """
session_name: test
agents:
  - id: python-expert
    type: claude_code
    tags:
      - python
      - testing
  - id: docs-writer
    type: claude_code
    tags:
      - markdown
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)

    config = load_config(config_file)
    agents_by_id = {a.id: a for a in config.agents}
    assert agents_by_id["python-expert"].tags == ["python", "testing"]
    assert agents_by_id["docs-writer"].tags == ["markdown"]


def test_load_config_without_tags(tmp_path):
    """AgentConfig.tags defaults to [] when not specified in YAML."""
    from tmux_orchestrator.config import load_config

    yaml_content = """
session_name: test
agents:
  - id: worker-1
    type: claude_code
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)

    config = load_config(config_file)
    assert config.agents[0].tags == []


# ---------------------------------------------------------------------------
# list_all: tags appear in agent snapshot
# ---------------------------------------------------------------------------


def test_list_all_includes_tags():
    """AgentRegistry.list_all() includes tags field for each agent."""
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=["python", "testing"])
    reg.register(agent)
    snapshot = reg.list_all()
    assert len(snapshot) == 1
    assert snapshot[0]["tags"] == ["python", "testing"]


def test_list_all_tags_empty_when_unset():
    """AgentRegistry.list_all() tags is [] for untagged agents."""
    bus = Bus()
    reg = _make_registry()
    agent = _make_idle_tagged_agent("a1", bus, tags=[])
    reg.register(agent)
    snapshot = reg.list_all()
    assert snapshot[0]["tags"] == []
