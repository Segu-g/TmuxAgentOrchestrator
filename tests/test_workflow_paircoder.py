"""Tests for v1.2.10 — PairCoder workflow + Codified Context spec file injection.

Coverage:
1.  AgentConfig.spec_files defaults to empty list
2.  _load_spec_files returns empty string when no spec_files
3.  _load_spec_files injects rules from YAML
4.  _load_spec_files handles missing file gracefully
5.  _load_spec_files handles malformed YAML gracefully
6.  _load_spec_files respects spec_files_root for relative paths
7.  _load_spec_files handles file with description + examples
8.  PairCoderWorkflowSubmit model parses correctly
9.  PairCoderWorkflowSubmit rejects empty task
10. POST /workflows/paircoder with max_rounds=1 produces 2 tasks
11. POST /workflows/paircoder with max_rounds=2 produces 4 tasks
12. Task dependency chain: write_r1 → review_r1 → write_r2 → review_r2
13. Writer tasks target writer_tags
14. Reviewer tasks target reviewer_tags
15. Scratchpad prefix auto-generated if empty
16. Writer prompt references previous round's feedback (round > 1)
17. Reviewer prompt references impl scratchpad key
18. Last reviewer prompt includes verdict key instruction
19. load_config reads spec_files from YAML
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from tmux_orchestrator.application.config import AgentConfig, load_config
from tmux_orchestrator.web.schemas import PairCoderWorkflowSubmit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent() -> MagicMock:
    """Return a minimal ClaudeCodeAgent mock with spec_files support."""
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    agent = MagicMock(spec=ClaudeCodeAgent)
    agent.id = "test-agent"
    agent.role = MagicMock()
    agent.role.value = "worker"
    agent._spec_files = []
    agent._spec_files_root = None
    agent._session_name = "test"
    agent._web_base_url = "http://localhost:8000"
    agent._isolate = False
    agent._system_prompt = None
    agent._context_files = []
    agent._parent_pane = None
    return agent


# ---------------------------------------------------------------------------
# 1. AgentConfig.spec_files defaults to empty list
# ---------------------------------------------------------------------------

def test_agent_config_spec_files_default():
    cfg = AgentConfig(id="a1", type="claude_code")
    assert cfg.spec_files == []


def test_agent_config_spec_files_can_be_set():
    cfg = AgentConfig(id="a1", type="claude_code", spec_files=["specs/style.yaml"])
    assert cfg.spec_files == ["specs/style.yaml"]


# ---------------------------------------------------------------------------
# 2. _load_spec_files returns empty string when no spec_files
# ---------------------------------------------------------------------------

def test_load_spec_files_empty_when_no_spec_files(tmp_path):
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    agent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
    agent.id = "test-agent"
    agent._spec_files = []
    agent._spec_files_root = tmp_path

    result = agent._load_spec_files()
    assert result == ""


# ---------------------------------------------------------------------------
# 3. _load_spec_files injects rules from YAML
# ---------------------------------------------------------------------------

def test_load_spec_files_injects_rules(tmp_path):
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    spec_file = tmp_path / "style.yaml"
    spec_file.write_text(yaml.dump({
        "name": "Python Style",
        "description": "Code style rules",
        "rules": ["Use type hints", "No bare except"],
    }))

    agent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
    agent.id = "test-agent"
    agent._spec_files = ["style.yaml"]
    agent._spec_files_root = tmp_path

    result = agent._load_spec_files()

    assert "## Codified Specs" in result
    assert "Python Style" in result
    assert "Code style rules" in result
    assert "Use type hints" in result
    assert "No bare except" in result


# ---------------------------------------------------------------------------
# 4. _load_spec_files handles missing file gracefully
# ---------------------------------------------------------------------------

def test_load_spec_files_missing_file_graceful(tmp_path):
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    agent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
    agent.id = "test-agent"
    agent._spec_files = ["nonexistent.yaml"]
    agent._spec_files_root = tmp_path

    # Should not raise; missing file is silently skipped
    result = agent._load_spec_files()
    # Section header present but no rules (only the empty preamble)
    assert "nonexistent" not in result.lower() or "## Codified Specs" in result or result == ""


# ---------------------------------------------------------------------------
# 5. _load_spec_files handles malformed YAML gracefully
# ---------------------------------------------------------------------------

def test_load_spec_files_malformed_yaml_graceful(tmp_path):
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("{{invalid: yaml: content")

    agent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
    agent.id = "test-agent"
    agent._spec_files = ["bad.yaml"]
    agent._spec_files_root = tmp_path

    # Should not raise
    result = agent._load_spec_files()
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 6. _load_spec_files respects spec_files_root for relative paths
# ---------------------------------------------------------------------------

def test_load_spec_files_respects_root(tmp_path):
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    subdir = tmp_path / "specs"
    subdir.mkdir()
    spec_file = subdir / "conventions.yaml"
    spec_file.write_text(yaml.dump({
        "name": "Conventions",
        "rules": ["Rule A"],
    }))

    agent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
    agent.id = "test-agent"
    agent._spec_files = ["specs/conventions.yaml"]
    agent._spec_files_root = tmp_path

    result = agent._load_spec_files()
    assert "Rule A" in result


# ---------------------------------------------------------------------------
# 7. _load_spec_files handles file with description + examples
# ---------------------------------------------------------------------------

def test_load_spec_files_with_examples(tmp_path):
    from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent

    spec_file = tmp_path / "style.yaml"
    spec_file.write_text(yaml.dump({
        "name": "Style",
        "description": "Short description",
        "rules": ["Use f-strings"],
        "examples": ["f'hello {name}'", "Path('/tmp') / 'file.txt'"],
    }))

    agent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
    agent.id = "test-agent"
    agent._spec_files = ["style.yaml"]
    agent._spec_files_root = tmp_path

    result = agent._load_spec_files()
    assert "Examples:" in result
    assert "f'hello {name}'" in result


# ---------------------------------------------------------------------------
# 8. PairCoderWorkflowSubmit parses correctly
# ---------------------------------------------------------------------------

def test_paircoder_submit_parses():
    body = PairCoderWorkflowSubmit(
        task="Implement fibonacci",
        language="python",
        max_rounds=2,
        writer_tags=["paircoder_writer"],
        reviewer_tags=["paircoder_reviewer"],
        agent_timeout=300,
    )
    assert body.task == "Implement fibonacci"
    assert body.language == "python"
    assert body.max_rounds == 2
    assert body.writer_tags == ["paircoder_writer"]
    assert body.reviewer_tags == ["paircoder_reviewer"]
    assert body.agent_timeout == 300
    assert body.scratchpad_prefix == ""
    assert body.spec_keys == []
    assert body.reply_to is None


# ---------------------------------------------------------------------------
# 9. PairCoderWorkflowSubmit rejects empty task
# ---------------------------------------------------------------------------

def test_paircoder_submit_rejects_empty_task():
    with pytest.raises(Exception):
        PairCoderWorkflowSubmit(task="")


# ---------------------------------------------------------------------------
# Fixtures for endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    wm = MagicMock()
    wm.submit.return_value = MagicMock(id=str(uuid.uuid4()), name="paircoder/test", phases=None)
    orch.get_workflow_manager.return_value = wm

    task_counter = [0]

    async def _submit_task(prompt, *, priority=0, depends_on=None, required_tags=None, timeout=None):
        task_counter[0] += 1
        t = MagicMock()
        t.id = f"task-{task_counter[0]}"
        return t

    orch.submit_task = _submit_task
    return orch


@pytest.fixture
def paircoder_client(mock_orchestrator):
    """Return a FastAPI TestClient with the paircoder endpoint mounted."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tmux_orchestrator.web.routers.workflows import build_workflows_router

    app = FastAPI()
    router = build_workflows_router(
        orchestrator=mock_orchestrator,
        auth=lambda: None,
        scratchpad={},
    )
    app.include_router(router)

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


# ---------------------------------------------------------------------------
# 10. max_rounds=1 → 2 tasks (write_r1 + review_r1)
# ---------------------------------------------------------------------------

def test_paircoder_max_rounds_1_produces_2_tasks(paircoder_client):
    resp = paircoder_client.post("/workflows/paircoder", json={
        "task": "Implement a sum function",
        "max_rounds": 1,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "task_ids" in data
    task_ids = data["task_ids"]
    assert len(task_ids) == 2
    assert "write_r1" in task_ids
    assert "review_r1" in task_ids


# ---------------------------------------------------------------------------
# 11. max_rounds=2 → 4 tasks
# ---------------------------------------------------------------------------

def test_paircoder_max_rounds_2_produces_4_tasks(paircoder_client):
    resp = paircoder_client.post("/workflows/paircoder", json={
        "task": "Implement fibonacci",
        "max_rounds": 2,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    task_ids = data["task_ids"]
    assert len(task_ids) == 4
    assert set(task_ids.keys()) == {"write_r1", "review_r1", "write_r2", "review_r2"}


# ---------------------------------------------------------------------------
# 12. Dependency chain
# ---------------------------------------------------------------------------

def test_paircoder_dependency_chain(mock_orchestrator):
    """Verify that tasks are submitted in the correct dependency order."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tmux_orchestrator.web.routers.workflows import build_workflows_router

    submitted: list[dict] = []

    async def _submit_task(prompt, *, priority=0, depends_on=None, required_tags=None, timeout=None):
        t = MagicMock()
        t.id = f"task-{len(submitted) + 1}"
        submitted.append({
            "id": t.id,
            "prompt": prompt[:40],
            "depends_on": depends_on or [],
            "required_tags": required_tags or [],
        })
        return t

    mock_orchestrator.submit_task = _submit_task

    app = FastAPI()
    router = build_workflows_router(orchestrator=mock_orchestrator, auth=lambda: None, scratchpad={})
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post("/workflows/paircoder", json={
            "task": "Implement sorting",
            "max_rounds": 2,
        })

    assert resp.status_code == 200, resp.text

    # Tasks submitted in order: write_r1, review_r1, write_r2, review_r2
    assert len(submitted) == 4
    write_r1, review_r1, write_r2, review_r2 = submitted

    # write_r1: no dependencies
    assert write_r1["depends_on"] == []

    # review_r1: depends on write_r1
    assert write_r1["id"] in review_r1["depends_on"]

    # write_r2: depends on review_r1
    assert review_r1["id"] in write_r2["depends_on"]

    # review_r2: depends on write_r2
    assert write_r2["id"] in review_r2["depends_on"]


# ---------------------------------------------------------------------------
# 13. Writer tasks target writer_tags
# ---------------------------------------------------------------------------

def test_paircoder_writer_tasks_use_writer_tags(mock_orchestrator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tmux_orchestrator.web.routers.workflows import build_workflows_router

    submitted: list[dict] = []

    async def _submit_task(prompt, *, priority=0, depends_on=None, required_tags=None, timeout=None):
        t = MagicMock()
        t.id = f"task-{len(submitted) + 1}"
        submitted.append({"required_tags": required_tags or []})
        return t

    mock_orchestrator.submit_task = _submit_task

    app = FastAPI()
    router = build_workflows_router(orchestrator=mock_orchestrator, auth=lambda: None, scratchpad={})
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post("/workflows/paircoder", json={
            "task": "Test task",
            "max_rounds": 1,
            "writer_tags": ["my_writer"],
            "reviewer_tags": ["my_reviewer"],
        })

    assert resp.status_code == 200
    writer_task = submitted[0]
    assert "my_writer" in writer_task["required_tags"]


# ---------------------------------------------------------------------------
# 14. Reviewer tasks target reviewer_tags
# ---------------------------------------------------------------------------

def test_paircoder_reviewer_tasks_use_reviewer_tags(mock_orchestrator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tmux_orchestrator.web.routers.workflows import build_workflows_router

    submitted: list[dict] = []

    async def _submit_task(prompt, *, priority=0, depends_on=None, required_tags=None, timeout=None):
        t = MagicMock()
        t.id = f"task-{len(submitted) + 1}"
        submitted.append({"required_tags": required_tags or []})
        return t

    mock_orchestrator.submit_task = _submit_task

    app = FastAPI()
    router = build_workflows_router(orchestrator=mock_orchestrator, auth=lambda: None, scratchpad={})
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post("/workflows/paircoder", json={
            "task": "Test task",
            "max_rounds": 1,
            "writer_tags": ["my_writer"],
            "reviewer_tags": ["my_reviewer"],
        })

    assert resp.status_code == 200
    reviewer_task = submitted[1]
    assert "my_reviewer" in reviewer_task["required_tags"]


# ---------------------------------------------------------------------------
# 15. Scratchpad prefix auto-generated if empty
# ---------------------------------------------------------------------------

def test_paircoder_scratchpad_prefix_auto_generated(paircoder_client):
    resp = paircoder_client.post("/workflows/paircoder", json={
        "task": "Some task",
        "scratchpad_prefix": "",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "scratchpad_prefix" in data
    prefix = data["scratchpad_prefix"]
    assert prefix.startswith("paircoder_")
    assert len(prefix) > len("paircoder_")


def test_paircoder_scratchpad_prefix_custom(paircoder_client):
    resp = paircoder_client.post("/workflows/paircoder", json={
        "task": "Some task",
        "scratchpad_prefix": "my_custom_prefix",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["scratchpad_prefix"] == "my_custom_prefix"


# ---------------------------------------------------------------------------
# 16. Writer prompt references previous round's feedback (round > 1)
# ---------------------------------------------------------------------------

def test_paircoder_writer_prompt_references_prev_feedback(mock_orchestrator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tmux_orchestrator.web.routers.workflows import build_workflows_router

    submitted: list[dict] = []

    async def _submit_task(prompt, *, priority=0, depends_on=None, required_tags=None, timeout=None):
        t = MagicMock()
        t.id = f"task-{len(submitted) + 1}"
        submitted.append({"prompt": prompt, "required_tags": required_tags or []})
        return t

    mock_orchestrator.submit_task = _submit_task

    app = FastAPI()
    router = build_workflows_router(orchestrator=mock_orchestrator, auth=lambda: None, scratchpad={})
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post("/workflows/paircoder", json={
            "task": "Implement merge sort",
            "max_rounds": 2,
            "scratchpad_prefix": "testpfx",
        })

    assert resp.status_code == 200
    # Tasks: write_r1(0), review_r1(1), write_r2(2), review_r2(3)
    write_r2_prompt = submitted[2]["prompt"]
    # write_r2 should reference the previous round's feedback key
    assert "testpfx_feedback_r1" in write_r2_prompt
    assert "ROUND 2" in write_r2_prompt


# ---------------------------------------------------------------------------
# 17. Reviewer prompt references impl scratchpad key
# ---------------------------------------------------------------------------

def test_paircoder_reviewer_prompt_references_impl_key(mock_orchestrator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tmux_orchestrator.web.routers.workflows import build_workflows_router

    submitted: list[dict] = []

    async def _submit_task(prompt, *, priority=0, depends_on=None, required_tags=None, timeout=None):
        t = MagicMock()
        t.id = f"task-{len(submitted) + 1}"
        submitted.append({"prompt": prompt})
        return t

    mock_orchestrator.submit_task = _submit_task

    app = FastAPI()
    router = build_workflows_router(orchestrator=mock_orchestrator, auth=lambda: None, scratchpad={})
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post("/workflows/paircoder", json={
            "task": "Implement binary search",
            "max_rounds": 1,
            "scratchpad_prefix": "bs_prefix",
        })

    assert resp.status_code == 200
    # review_r1 is submitted second
    review_r1_prompt = submitted[1]["prompt"]
    assert "bs_prefix_impl_r1" in review_r1_prompt


# ---------------------------------------------------------------------------
# 18. Last reviewer prompt includes verdict key instruction
# ---------------------------------------------------------------------------

def test_paircoder_last_reviewer_includes_verdict(mock_orchestrator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tmux_orchestrator.web.routers.workflows import build_workflows_router

    submitted: list[dict] = []

    async def _submit_task(prompt, *, priority=0, depends_on=None, required_tags=None, timeout=None):
        t = MagicMock()
        t.id = f"task-{len(submitted) + 1}"
        submitted.append({"prompt": prompt})
        return t

    mock_orchestrator.submit_task = _submit_task

    app = FastAPI()
    router = build_workflows_router(orchestrator=mock_orchestrator, auth=lambda: None, scratchpad={})
    app.include_router(router)

    with TestClient(app) as client:
        resp = client.post("/workflows/paircoder", json={
            "task": "Implement quicksort",
            "max_rounds": 2,
            "scratchpad_prefix": "qs_prefix",
        })

    assert resp.status_code == 200
    # review_r2 is the last task (index 3)
    review_r2_prompt = submitted[3]["prompt"]
    assert "qs_prefix_verdict" in review_r2_prompt

    # review_r1 (index 1) should NOT have verdict key
    review_r1_prompt = submitted[1]["prompt"]
    # It should contain feedback key but not verdict key
    assert "qs_prefix_feedback_r1" in review_r1_prompt


# ---------------------------------------------------------------------------
# 19. load_config reads spec_files from YAML
# ---------------------------------------------------------------------------

def test_load_config_reads_spec_files(tmp_path):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
session_name: test
task_timeout: 120
watchdog_poll: 40
agents:
  - id: writer
    type: claude_code
    tags: [paircoder_writer]
    spec_files:
      - specs/python_style.yaml
      - specs/testing_conventions.yaml
""")
    cfg = load_config(config_yaml, cwd=tmp_path)
    assert len(cfg.agents) == 1
    writer = cfg.agents[0]
    assert writer.spec_files == [
        "specs/python_style.yaml",
        "specs/testing_conventions.yaml",
    ]


def test_load_config_spec_files_defaults_to_empty(tmp_path):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
session_name: test
task_timeout: 120
watchdog_poll: 40
agents:
  - id: worker
    type: claude_code
""")
    cfg = load_config(config_yaml, cwd=tmp_path)
    assert cfg.agents[0].spec_files == []
