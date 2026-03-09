"""Tests for POST /workflows/competition — Best-of-N competitive solver workflow.

The competition workflow builds a (N+1)-agent DAG:

  solver_strategy_0 ──┐
  solver_strategy_1 ──┼─→ judge
  solver_strategy_2 ──┘

- solver_{strategy}: receives the same problem + a strategy hint, produces a
  solution + numeric SCORE line, stores result in scratchpad.
- judge: reads all solver results, compares scores, declares WINNER, writes
  COMPETITION_RESULT.md to scratchpad.

Design references:
- "Making, not Taking, the Best of N" (FusioN), arXiv:2510.00931, 2025.
- M-A-P "Multi-Agent Parallel Test-Time Scaling", arXiv:2506.12928, 2025.
- "When AIs Judge AIs: Agent-as-a-Judge", arXiv:2508.02994, 2025.
- MultiAgentBench, arXiv:2503.01935, 2025.
- DESIGN.md §10.36 (v1.1.0)
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.web.app import create_app, CompetitionWorkflowSubmit
import tmux_orchestrator.web.app as web_app_mod


# ---------------------------------------------------------------------------
# Fixtures
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


def _make_app():
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    app = create_app(orch, _StubHub(), api_key="test-key")  # type: ignore[arg-type]
    return app, orch


_API_KEY = "test-key"

_TWO_STRATEGIES = ["greedy", "dynamic_programming"]
_THREE_STRATEGIES = ["greedy", "dynamic_programming", "random_restart"]


@pytest.fixture()
def client():
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def client_and_orch():
    app, orch = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, orch


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state before each test."""
    web_app_mod._scratchpad.clear()
    yield


def auth_headers() -> dict:
    return {"X-API-Key": _API_KEY}


def _get_tasks(client) -> dict:
    """Fetch all queued tasks from /tasks and return as {task_id: task_dict}."""
    tasks_resp = client.get("/tasks", headers=auth_headers())
    assert tasks_resp.status_code == 200
    return {t["task_id"]: t for t in tasks_resp.json()}


# ---------------------------------------------------------------------------
# CompetitionWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_competition_submit_empty_problem_rejected():
    """Empty problem should raise ValueError."""
    with pytest.raises(Exception):
        CompetitionWorkflowSubmit(problem="", strategies=_TWO_STRATEGIES)


def test_competition_submit_whitespace_problem_rejected():
    """Whitespace-only problem should raise ValueError."""
    with pytest.raises(Exception):
        CompetitionWorkflowSubmit(problem="   ", strategies=_TWO_STRATEGIES)


def test_competition_submit_one_strategy_rejected():
    """Less than 2 strategies should raise ValueError."""
    with pytest.raises(Exception):
        CompetitionWorkflowSubmit(problem="solve knapsack", strategies=["greedy"])


def test_competition_submit_eleven_strategies_rejected():
    """More than 10 strategies should raise ValueError."""
    with pytest.raises(Exception):
        CompetitionWorkflowSubmit(
            problem="solve knapsack",
            strategies=[f"strategy_{i}" for i in range(11)],
        )


def test_competition_submit_blank_strategy_name_rejected():
    """Blank strategy names should raise ValueError."""
    with pytest.raises(Exception):
        CompetitionWorkflowSubmit(
            problem="solve knapsack",
            strategies=["greedy", ""],
        )


def test_competition_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = CompetitionWorkflowSubmit(
        problem="sort a list of integers",
        strategies=["quicksort", "mergesort"],
    )
    assert obj.problem == "sort a list of integers"
    assert obj.strategies == ["quicksort", "mergesort"]
    assert obj.scoring_criterion == "correctness and efficiency"
    assert obj.solver_tags == []
    assert obj.judge_tags == []
    assert obj.reply_to is None


def test_competition_submit_with_custom_scoring_criterion():
    """Custom scoring_criterion should be accepted."""
    obj = CompetitionWorkflowSubmit(
        problem="solve knapsack",
        strategies=["greedy", "dp"],
        scoring_criterion="maximize total value",
    )
    assert obj.scoring_criterion == "maximize total value"


def test_competition_submit_with_tags():
    """solver_tags and judge_tags should be accepted."""
    obj = CompetitionWorkflowSubmit(
        problem="sort a list",
        strategies=["bubble", "merge"],
        solver_tags=["solver-role"],
        judge_tags=["judge-role"],
    )
    assert obj.solver_tags == ["solver-role"]
    assert obj.judge_tags == ["judge-role"]


def test_competition_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = CompetitionWorkflowSubmit(
        problem="sort a list",
        strategies=["a", "b"],
        reply_to="director-1",
    )
    assert obj.reply_to == "director-1"


def test_competition_submit_ten_strategies_accepted():
    """Exactly 10 strategies should be accepted (boundary)."""
    obj = CompetitionWorkflowSubmit(
        problem="some problem",
        strategies=[f"s{i}" for i in range(10)],
    )
    assert len(obj.strategies) == 10


def test_competition_submit_two_strategies_accepted():
    """Exactly 2 strategies should be accepted (boundary)."""
    obj = CompetitionWorkflowSubmit(
        problem="some problem",
        strategies=["a", "b"],
    )
    assert len(obj.strategies) == 2


# ---------------------------------------------------------------------------
# POST /workflows/competition — HTTP auth
# ---------------------------------------------------------------------------


def test_competition_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": _TWO_STRATEGIES},
    )
    assert resp.status_code == 401


def test_competition_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": _TWO_STRATEGIES},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_competition_workflow_empty_problem_returns_422(client):
    """Empty problem should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_competition_workflow_missing_problem_returns_422(client):
    """Missing problem field should return 422."""
    resp = client.post(
        "/workflows/competition",
        json={"strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_competition_workflow_missing_strategies_returns_422(client):
    """Missing strategies field should return 422."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_competition_workflow_one_strategy_returns_422(client):
    """Single strategy should return 422."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": ["greedy"]},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_competition_workflow_returns_200(client):
    """Valid request with 2 strategies should return 200."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list of integers", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /workflows/competition — response structure
# ---------------------------------------------------------------------------


def test_competition_workflow_response_fields(client):
    """Response must contain workflow_id, name, task_ids, scratchpad_prefix."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve 0-1 knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_competition_workflow_name_starts_with_competition(client):
    """Workflow name should start with 'competition/'."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    assert data["name"].startswith("competition/")


def test_competition_workflow_name_contains_problem_start(client):
    """Workflow name should contain the beginning of the problem."""
    problem = "sort a list of integers using efficient algorithms"
    resp = client.post(
        "/workflows/competition",
        json={"problem": problem, "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    # Name suffix is problem[:40]
    assert "sort a list of integers" in data["name"]


def test_competition_workflow_task_count_two_strategies(client):
    """2 strategies → 3 tasks: 2 solvers + 1 judge."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    assert len(data["task_ids"]) == 3


def test_competition_workflow_task_count_three_strategies(client):
    """3 strategies → 4 tasks: 3 solvers + 1 judge."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _THREE_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    assert len(data["task_ids"]) == 4


def test_competition_workflow_has_judge_key(client):
    """task_ids must contain 'judge' key."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    assert "judge" in data["task_ids"]


def test_competition_workflow_has_solver_keys(client):
    """task_ids must contain 'solver_{strategy}' for each strategy."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        assert f"solver_{safe}" in data["task_ids"], (
            f"solver_{safe} not in task_ids: {list(data['task_ids'].keys())}"
        )


def test_competition_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    for role, tid in data["task_ids"].items():
        assert isinstance(tid, str), f"task_id for {role} is not a string"
        assert len(tid) > 0, f"task_id for {role} is empty"


def test_competition_workflow_task_ids_distinct(client):
    """All task IDs must be distinct."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _THREE_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_competition_scratchpad_prefix_format(client):
    """Scratchpad prefix should match 'competition_XXXXXXXX' pattern."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    assert re.match(r"^competition_[0-9a-f]{8}$", data["scratchpad_prefix"]), (
        f"unexpected prefix: {data['scratchpad_prefix']}"
    )


def test_competition_scratchpad_prefix_unique_across_runs(client):
    """Two workflow submissions should produce distinct scratchpad prefixes."""
    resp1 = client.post(
        "/workflows/competition",
        json={"problem": "problem A", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/competition",
        json={"problem": "problem B", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    assert resp1.json()["scratchpad_prefix"] != resp2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task dependency structure
# ---------------------------------------------------------------------------


def test_competition_solvers_have_no_dependencies(client):
    """All solver tasks should have no depends_on (run in parallel)."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        solver_id = data["task_ids"][f"solver_{safe}"]
        assert solver_id in tasks, f"solver_{safe} task not found in /tasks"
        assert tasks[solver_id].get("depends_on", []) == [], (
            f"solver_{safe} should have no dependencies"
        )


def test_competition_judge_depends_on_all_solvers(client):
    """Judge task must depend on ALL solver tasks."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _THREE_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    assert judge_id in tasks, "judge task not found in /tasks"
    judge_deps = set(tasks[judge_id].get("depends_on", []))

    for strategy in _THREE_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        solver_id = data["task_ids"][f"solver_{safe}"]
        assert solver_id in judge_deps, (
            f"judge does not depend on solver_{safe}: deps={judge_deps}"
        )


def test_competition_judge_depends_on_exactly_n_solvers(client):
    """Judge depends_on length equals the number of strategies."""
    strategies = _THREE_STRATEGIES
    resp = client.post(
        "/workflows/competition",
        json={"problem": "problem", "strategies": strategies},
        headers=auth_headers(),
    )
    data = resp.json()
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    judge_deps = tasks[judge_id].get("depends_on", [])
    assert len(judge_deps) == len(strategies), (
        f"judge.depends_on has {len(judge_deps)} entries, expected {len(strategies)}"
    )


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_competition_reply_to_forwarded_to_judge_only():
    """reply_to should be forwarded to the judge task only."""
    app, orch = _make_app()
    reply_tos: list = []
    original_submit = orch.submit_task

    async def capture_submit(*args, **kwargs):
        reply_tos.append(kwargs.get("reply_to"))
        return await original_submit(*args, **kwargs)

    orch.submit_task = capture_submit  # type: ignore[method-assign]

    with TestClient(app) as c:
        c.post(
            "/workflows/competition",
            json={
                "problem": "solve knapsack",
                "strategies": _TWO_STRATEGIES,
                "reply_to": "director-1",
            },
            headers=auth_headers(),
        )

    # 2 solvers + 1 judge = 3 submit_task calls
    assert len(reply_tos) == 3, f"Expected 3 submit_task calls, got {len(reply_tos)}"
    # Solvers should NOT have reply_to
    assert reply_tos[0] is None, f"solver 0 should not have reply_to: {reply_tos[0]}"
    assert reply_tos[1] is None, f"solver 1 should not have reply_to: {reply_tos[1]}"
    # Only judge (last) should have reply_to
    assert reply_tos[2] == "director-1", (
        f"reply_to not propagated to judge: {reply_tos}"
    )


def test_competition_reply_to_none_by_default(client):
    """reply_to=None should result in no reply_to on any task."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)
    for role, tid in data["task_ids"].items():
        rt = tasks[tid].get("reply_to")
        assert rt is None, f"{role} task has unexpected reply_to={rt!r}"


# ---------------------------------------------------------------------------
# Tag routing
# ---------------------------------------------------------------------------


def test_competition_solver_tags_forwarded(client):
    """solver_tags should appear in all solver task required_tags."""
    resp = client.post(
        "/workflows/competition",
        json={
            "problem": "solve knapsack",
            "strategies": _TWO_STRATEGIES,
            "solver_tags": ["solver-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        solver_id = data["task_ids"][f"solver_{safe}"]
        tags = tasks[solver_id].get("required_tags", [])
        assert "solver-role" in tags, (
            f"solver_tags not forwarded to solver_{safe}: {tags}"
        )


def test_competition_judge_tags_forwarded(client):
    """judge_tags should appear in the judge task required_tags."""
    resp = client.post(
        "/workflows/competition",
        json={
            "problem": "solve knapsack",
            "strategies": _TWO_STRATEGIES,
            "judge_tags": ["judge-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    tags = tasks[judge_id].get("required_tags", [])
    assert "judge-role" in tags, f"judge_tags not forwarded to judge: {tags}"


def test_competition_empty_tags_result_in_none(client):
    """Empty tags lists should result in no required_tags constraint."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)
    for role, tid in data["task_ids"].items():
        tags = tasks[tid].get("required_tags")
        assert not tags, f"{role} should have no required_tags, got: {tags}"


# ---------------------------------------------------------------------------
# Prompt content checks
# ---------------------------------------------------------------------------


def test_competition_solver_prompt_mentions_problem(client):
    """Each solver task prompt should mention the problem description."""
    problem = "solve the 0-1 knapsack problem with n=100 items"
    resp = client.post(
        "/workflows/competition",
        json={"problem": problem, "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        solver_id = data["task_ids"][f"solver_{safe}"]
        prompt = tasks[solver_id].get("prompt", "")
        assert problem in prompt, (
            f"problem not found in solver_{safe} prompt: {prompt[:200]}"
        )


def test_competition_solver_prompt_mentions_strategy(client):
    """Each solver task prompt should mention its assigned strategy."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        solver_id = data["task_ids"][f"solver_{safe}"]
        prompt = tasks[solver_id].get("prompt", "")
        assert strategy in prompt, (
            f"strategy '{strategy}' not found in solver_{safe} prompt"
        )


def test_competition_solver_prompt_mentions_score_line(client):
    """Each solver prompt should instruct writing a SCORE: line."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        solver_id = data["task_ids"][f"solver_{safe}"]
        prompt = tasks[solver_id].get("prompt", "")
        assert "SCORE:" in prompt, (
            f"SCORE: instruction missing from solver_{safe} prompt"
        )


def test_competition_judge_prompt_mentions_problem(client):
    """Judge task prompt should mention the problem description."""
    problem = "solve the 0-1 knapsack problem"
    resp = client.post(
        "/workflows/competition",
        json={"problem": problem, "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    prompt = tasks[judge_id].get("prompt", "")
    assert problem in prompt, f"problem not found in judge prompt: {prompt[:200]}"


def test_competition_judge_prompt_mentions_all_strategies(client):
    """Judge prompt should mention all competing strategies."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _THREE_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    prompt = tasks[judge_id].get("prompt", "")
    for strategy in _THREE_STRATEGIES:
        assert strategy in prompt, (
            f"strategy '{strategy}' not found in judge prompt"
        )


def test_competition_judge_prompt_mentions_winner_line(client):
    """Judge prompt should instruct writing a WINNER: line."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "solve knapsack", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    prompt = tasks[judge_id].get("prompt", "")
    assert "WINNER:" in prompt, "WINNER: instruction missing from judge prompt"


def test_competition_judge_prompt_mentions_scoring_criterion(client):
    """Judge prompt should mention the scoring criterion."""
    criterion = "maximize total value within weight limit"
    resp = client.post(
        "/workflows/competition",
        json={
            "problem": "solve knapsack",
            "strategies": _TWO_STRATEGIES,
            "scoring_criterion": criterion,
        },
        headers=auth_headers(),
    )
    data = resp.json()
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    prompt = tasks[judge_id].get("prompt", "")
    assert criterion in prompt, (
        f"scoring_criterion not found in judge prompt: {prompt[:200]}"
    )


def test_competition_solver_prompts_mention_scratchpad_key(client):
    """Each solver prompt should contain its scratchpad key."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    tasks = _get_tasks(client)
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        solver_id = data["task_ids"][f"solver_{safe}"]
        prompt = tasks[solver_id].get("prompt", "")
        expected_key = f"{prefix}_solver_{safe}"
        assert expected_key in prompt, (
            f"scratchpad key '{expected_key}' not found in solver_{safe} prompt"
        )


def test_competition_judge_prompt_mentions_all_solver_keys(client):
    """Judge prompt should mention the scratchpad key for each solver."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    judge_id = data["task_ids"]["judge"]
    tasks = _get_tasks(client)
    prompt = tasks[judge_id].get("prompt", "")
    for strategy in _TWO_STRATEGIES:
        safe = strategy.replace(" ", "_").replace("/", "_")
        expected_key = f"{prefix}_solver_{safe}"
        assert expected_key in prompt, (
            f"scratchpad key '{expected_key}' not found in judge prompt"
        )


# ---------------------------------------------------------------------------
# Workflow ID
# ---------------------------------------------------------------------------


def test_competition_workflow_id_is_uuid(client):
    """workflow_id should be a valid UUID string."""
    resp = client.post(
        "/workflows/competition",
        json={"problem": "sort a list", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    uuid.UUID(data["workflow_id"])


def test_competition_two_runs_different_workflow_ids(client):
    """Two workflow submissions should have distinct workflow IDs."""
    resp1 = client.post(
        "/workflows/competition",
        json={"problem": "problem X", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/competition",
        json={"problem": "problem Y", "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    assert resp1.json()["workflow_id"] != resp2.json()["workflow_id"]


# ---------------------------------------------------------------------------
# Strategy name sanitisation (spaces / slashes)
# ---------------------------------------------------------------------------


def test_competition_strategy_with_spaces_sanitised(client):
    """Spaces in strategy names should be replaced with underscores."""
    strategies = ["random restart", "hill climbing"]
    resp = client.post(
        "/workflows/competition",
        json={"problem": "optimise function", "strategies": strategies},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "solver_random_restart" in data["task_ids"]
    assert "solver_hill_climbing" in data["task_ids"]


def test_competition_strategy_with_slashes_sanitised(client):
    """Slashes in strategy names should be replaced with underscores."""
    strategies = ["branch/bound", "dynamic_programming"]
    resp = client.post(
        "/workflows/competition",
        json={"problem": "optimise function", "strategies": strategies},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "solver_branch_bound" in data["task_ids"]


# ---------------------------------------------------------------------------
# OpenAPI schema
# ---------------------------------------------------------------------------


def test_competition_workflow_registered_in_openapi(client):
    """The /workflows/competition endpoint should be listed in the OpenAPI schema."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = schema.get("paths", {})
    assert "/workflows/competition" in paths, (
        f"/workflows/competition not found in OpenAPI paths: {list(paths.keys())}"
    )


def test_competition_workflow_openapi_has_post_method(client):
    """The /workflows/competition endpoint should support POST."""
    resp = client.get("/openapi.json")
    schema = resp.json()
    assert "post" in schema["paths"]["/workflows/competition"]


# ---------------------------------------------------------------------------
# Long problem truncation in name
# ---------------------------------------------------------------------------


def test_competition_long_problem_truncated_in_name(client):
    """Workflow name suffix should be at most 40 chars (from problem[:40].strip())."""
    long_problem = (
        "implement a highly optimised solver for the travelling salesman problem"
    )
    resp = client.post(
        "/workflows/competition",
        json={"problem": long_problem, "strategies": _TWO_STRATEGIES},
        headers=auth_headers(),
    )
    data = resp.json()
    name_suffix = data["name"][len("competition/"):]
    assert len(name_suffix) <= 40, f"name suffix too long: {len(name_suffix)} chars"
