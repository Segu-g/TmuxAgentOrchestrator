"""Tests for POST /workflows/clean-arch — 4-agent Clean Architecture pipeline.

The clean-arch workflow builds a strictly sequential 4-agent DAG:

  domain-designer → usecase-designer → adapter-designer → framework-designer

- domain-designer:   defines domain Entities, Value Objects, Aggregates, and
  Domain Events without any framework dependency; writes DOMAIN.md and stores
  it in scratchpad ``{prefix}_domain``.
- usecase-designer:  reads domain layer; defines Use Cases, Input/Output DTOs,
  and Port interfaces; writes USECASES.md and stores it.
- adapter-designer:  reads domain + use-cases; defines concrete Interface
  Adapters (Repositories, Presenters, Controllers); writes ADAPTERS.md.
- framework-designer: reads all previous layers; synthesises ARCHITECTURE.md
  and writes a composition-root main.py skeleton.

Design references:
- Robert C. Martin, "Clean Architecture" (2017): Domain → Use Cases →
  Interface Adapters → Frameworks & Drivers.
- AgentMesh arXiv:2507.19902 (2025): 4-role artifact-centric pipeline.
- Muthu (2025-11) "The Architecture is the Prompt".
- DESIGN.md §10.30 (v1.0.30)
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
from tmux_orchestrator.web.app import create_app, CleanArchWorkflowSubmit
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
# CleanArchWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_clean_arch_submit_empty_feature_rejected():
    """Empty feature should raise ValueError."""
    with pytest.raises(Exception):
        CleanArchWorkflowSubmit(feature="")


def test_clean_arch_submit_whitespace_feature_rejected():
    """Whitespace-only feature should raise ValueError."""
    with pytest.raises(Exception):
        CleanArchWorkflowSubmit(feature="   ")


def test_clean_arch_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = CleanArchWorkflowSubmit(feature="user authentication")
    assert obj.feature == "user authentication"
    assert obj.language == "python"
    assert obj.domain_designer_tags == []
    assert obj.usecase_designer_tags == []
    assert obj.adapter_designer_tags == []
    assert obj.framework_designer_tags == []
    assert obj.reply_to is None


def test_clean_arch_submit_with_language():
    """Language field should be accepted."""
    obj = CleanArchWorkflowSubmit(feature="order management", language="typescript")
    assert obj.language == "typescript"


def test_clean_arch_submit_with_all_tags():
    """Tags should be accepted on all four roles."""
    obj = CleanArchWorkflowSubmit(
        feature="inventory system",
        domain_designer_tags=["domain"],
        usecase_designer_tags=["usecase"],
        adapter_designer_tags=["adapter"],
        framework_designer_tags=["framework"],
    )
    assert obj.domain_designer_tags == ["domain"]
    assert obj.usecase_designer_tags == ["usecase"]
    assert obj.adapter_designer_tags == ["adapter"]
    assert obj.framework_designer_tags == ["framework"]


def test_clean_arch_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = CleanArchWorkflowSubmit(feature="payment processing", reply_to="director-1")
    assert obj.reply_to == "director-1"


# ---------------------------------------------------------------------------
# POST /workflows/clean-arch — HTTP auth
# ---------------------------------------------------------------------------


def test_clean_arch_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "user authentication"},
    )
    assert resp.status_code == 401


def test_clean_arch_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "user authentication"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_clean_arch_workflow_empty_feature_returns_422(client):
    """Empty feature should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_clean_arch_workflow_missing_feature_returns_422(client):
    """Missing feature field should return 422."""
    resp = client.post(
        "/workflows/clean-arch",
        json={},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_clean_arch_workflow_returns_200(client):
    """Valid request should return 200."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "user authentication"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /workflows/clean-arch — response structure
# ---------------------------------------------------------------------------


def test_clean_arch_workflow_response_fields(client):
    """Response must contain workflow_id, name, task_ids, scratchpad_prefix."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "order management system"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_clean_arch_workflow_name_starts_with_clean_arch(client):
    """Workflow name should start with 'clean-arch/'."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "inventory tracking"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert data["name"].startswith("clean-arch/")


def test_clean_arch_workflow_name_contains_feature(client):
    """Workflow name should contain the feature description."""
    feature = "user profile management"
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": feature},
        headers=auth_headers(),
    )
    data = resp.json()
    assert feature in data["name"]


def test_clean_arch_workflow_task_count(client):
    """Workflow must create exactly 4 tasks."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "payment processing"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert len(task_ids) == 4
    assert "domain_designer" in task_ids
    assert "usecase_designer" in task_ids
    assert "adapter_designer" in task_ids
    assert "framework_designer" in task_ids


def test_clean_arch_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "shopping cart"},
        headers=auth_headers(),
    )
    data = resp.json()
    for role, tid in data["task_ids"].items():
        assert isinstance(tid, str), f"task_id for {role} is not a string"
        assert len(tid) > 0, f"task_id for {role} is empty"


def test_clean_arch_workflow_task_ids_distinct(client):
    """All four task IDs must be distinct."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "blog post management"},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_clean_arch_scratchpad_prefix_format(client):
    """Scratchpad prefix should match 'cleanarch_XXXXXXXX' pattern."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "notification service"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert re.match(r"^cleanarch_[0-9a-f]{8}$", data["scratchpad_prefix"]), (
        f"unexpected prefix: {data['scratchpad_prefix']}"
    )


def test_clean_arch_scratchpad_prefix_unique_across_runs(client):
    """Two workflow submissions should produce distinct scratchpad prefixes."""
    resp1 = client.post(
        "/workflows/clean-arch",
        json={"feature": "task manager A"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/clean-arch",
        json={"feature": "task manager B"},
        headers=auth_headers(),
    )
    assert resp1.json()["scratchpad_prefix"] != resp2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task dependency chain
# ---------------------------------------------------------------------------


def test_clean_arch_domain_has_no_dependencies(client):
    """domain_designer task should have no depends_on."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "event booking system"},
        headers=auth_headers(),
    )
    data = resp.json()
    domain_id = data["task_ids"]["domain_designer"]

    tasks = _get_tasks(client)
    assert domain_id in tasks, "domain_designer task not found in /tasks"
    assert tasks[domain_id].get("depends_on", []) == [], (
        "domain_designer should have no dependencies"
    )


def test_clean_arch_usecase_depends_on_domain(client):
    """usecase_designer task should depend on the domain_designer task."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "customer loyalty program"},
        headers=auth_headers(),
    )
    data = resp.json()
    domain_id = data["task_ids"]["domain_designer"]
    usecase_id = data["task_ids"]["usecase_designer"]

    tasks = _get_tasks(client)
    assert usecase_id in tasks, "usecase_designer task not found in /tasks"
    assert domain_id in tasks[usecase_id].get("depends_on", []), (
        f"usecase_designer.depends_on does not contain domain_designer"
    )


def test_clean_arch_adapter_depends_on_usecase(client):
    """adapter_designer task should depend on the usecase_designer task."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "product catalog"},
        headers=auth_headers(),
    )
    data = resp.json()
    usecase_id = data["task_ids"]["usecase_designer"]
    adapter_id = data["task_ids"]["adapter_designer"]

    tasks = _get_tasks(client)
    assert adapter_id in tasks, "adapter_designer task not found in /tasks"
    assert usecase_id in tasks[adapter_id].get("depends_on", []), (
        "adapter_designer.depends_on does not contain usecase_designer"
    )


def test_clean_arch_framework_depends_on_adapter(client):
    """framework_designer task should depend on the adapter_designer task."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "warehouse management"},
        headers=auth_headers(),
    )
    data = resp.json()
    adapter_id = data["task_ids"]["adapter_designer"]
    framework_id = data["task_ids"]["framework_designer"]

    tasks = _get_tasks(client)
    assert framework_id in tasks, "framework_designer task not found in /tasks"
    assert adapter_id in tasks[framework_id].get("depends_on", []), (
        "framework_designer.depends_on does not contain adapter_designer"
    )


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_clean_arch_workflow_reply_to_passed_to_framework_designer():
    """reply_to should be forwarded to the framework_designer task only."""
    app, orch = _make_app()
    reply_tos: list = []
    original_submit = orch.submit_task

    async def capture_submit(*args, **kwargs):
        reply_tos.append(kwargs.get("reply_to"))
        return await original_submit(*args, **kwargs)

    orch.submit_task = capture_submit  # type: ignore[method-assign]

    with TestClient(app) as c:
        c.post(
            "/workflows/clean-arch",
            json={"feature": "subscription billing", "reply_to": "director-1"},
            headers=auth_headers(),
        )

    # 4 tasks: domain, usecase, adapter, framework
    assert len(reply_tos) == 4, f"Expected 4 submit_task calls, got {len(reply_tos)}"
    # Only framework_designer (last task) should have reply_to
    assert reply_tos[3] == "director-1", (
        f"reply_to not propagated to framework_designer: {reply_tos}"
    )
    for i in range(3):
        assert reply_tos[i] is None, (
            f"task {i} should not have reply_to: {reply_tos[i]}"
        )


def test_clean_arch_workflow_reply_to_none_by_default(client):
    """reply_to=None should result in no reply_to on any task."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "file storage service"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)
    for role, tid in task_ids.items():
        rt = tasks[tid].get("reply_to")
        assert rt is None, f"{role} task has unexpected reply_to={rt!r}"


# ---------------------------------------------------------------------------
# Tag routing
# ---------------------------------------------------------------------------


def test_clean_arch_workflow_tags_forwarded(client):
    """All four role tags should appear in the corresponding task required_tags."""
    resp = client.post(
        "/workflows/clean-arch",
        json={
            "feature": "message queue",
            "domain_designer_tags": ["domain-role"],
            "usecase_designer_tags": ["usecase-role"],
            "adapter_designer_tags": ["adapter-role"],
            "framework_designer_tags": ["framework-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)

    assert "domain-role" in tasks[task_ids["domain_designer"]].get("required_tags", [])
    assert "usecase-role" in tasks[task_ids["usecase_designer"]].get("required_tags", [])
    assert "adapter-role" in tasks[task_ids["adapter_designer"]].get("required_tags", [])
    assert "framework-role" in tasks[task_ids["framework_designer"]].get("required_tags", [])


def test_clean_arch_workflow_empty_tags_result_in_none(client):
    """Empty tag lists should result in no required_tags constraint."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "report generator"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)
    for role, tid in task_ids.items():
        tags = tasks[tid].get("required_tags")
        assert not tags, f"{role} should have no required_tags, got: {tags}"


# ---------------------------------------------------------------------------
# Prompt content checks
# ---------------------------------------------------------------------------


def test_clean_arch_domain_prompt_mentions_feature(client):
    """Domain-designer prompt should mention the feature."""
    feature = "customer reward points system"
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": feature},
        headers=auth_headers(),
    )
    data = resp.json()
    domain_id = data["task_ids"]["domain_designer"]

    tasks = _get_tasks(client)
    prompt = tasks[domain_id].get("prompt", "")
    assert feature in prompt, f"feature not found in domain prompt: {prompt[:200]}"


def test_clean_arch_domain_prompt_mentions_role(client):
    """Domain-designer prompt should mention DOMAIN-DESIGNER role."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "booking system"},
        headers=auth_headers(),
    )
    data = resp.json()
    domain_id = data["task_ids"]["domain_designer"]

    tasks = _get_tasks(client)
    prompt = tasks[domain_id].get("prompt", "")
    assert "DOMAIN" in prompt or "domain" in prompt.lower(), (
        "domain prompt should mention DOMAIN role"
    )


def test_clean_arch_usecase_prompt_mentions_role(client):
    """Usecase-designer prompt should mention USECASE role."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "inventory tracking"},
        headers=auth_headers(),
    )
    data = resp.json()
    usecase_id = data["task_ids"]["usecase_designer"]

    tasks = _get_tasks(client)
    prompt = tasks[usecase_id].get("prompt", "")
    assert "USE" in prompt or "use" in prompt.lower(), (
        "usecase prompt should mention use-case role"
    )


def test_clean_arch_adapter_prompt_mentions_role(client):
    """Adapter-designer prompt should mention ADAPTER role."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "email notification"},
        headers=auth_headers(),
    )
    data = resp.json()
    adapter_id = data["task_ids"]["adapter_designer"]

    tasks = _get_tasks(client)
    prompt = tasks[adapter_id].get("prompt", "")
    assert "ADAPTER" in prompt or "adapter" in prompt.lower(), (
        "adapter prompt should mention ADAPTER role"
    )


def test_clean_arch_framework_prompt_mentions_role(client):
    """Framework-designer prompt should mention FRAMEWORK role."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "data export service"},
        headers=auth_headers(),
    )
    data = resp.json()
    framework_id = data["task_ids"]["framework_designer"]

    tasks = _get_tasks(client)
    prompt = tasks[framework_id].get("prompt", "")
    assert "FRAMEWORK" in prompt or "framework" in prompt.lower(), (
        "framework prompt should mention FRAMEWORK role"
    )


def test_clean_arch_scratchpad_keys_in_prompts(client):
    """All four scratchpad keys should appear in the relevant prompts."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "task scheduling"},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)

    domain_key = f"{prefix}_domain"
    usecases_key = f"{prefix}_usecases"
    adapters_key = f"{prefix}_adapters"
    arch_key = f"{prefix}_arch"

    domain_prompt = tasks[task_ids["domain_designer"]].get("prompt", "")
    usecase_prompt = tasks[task_ids["usecase_designer"]].get("prompt", "")
    adapter_prompt = tasks[task_ids["adapter_designer"]].get("prompt", "")
    framework_prompt = tasks[task_ids["framework_designer"]].get("prompt", "")

    # domain key: written by domain-designer, read by usecase and adapter
    assert domain_key in domain_prompt, "domain_key missing from domain prompt"
    assert domain_key in usecase_prompt, "domain_key missing from usecase prompt"
    assert domain_key in adapter_prompt, "domain_key missing from adapter prompt"
    assert domain_key in framework_prompt, "domain_key missing from framework prompt"

    # usecases key: written by usecase, read by adapter and framework
    assert usecases_key in usecase_prompt, "usecases_key missing from usecase prompt"
    assert usecases_key in adapter_prompt, "usecases_key missing from adapter prompt"
    assert usecases_key in framework_prompt, "usecases_key missing from framework prompt"

    # adapters key: written by adapter, read by framework
    assert adapters_key in adapter_prompt, "adapters_key missing from adapter prompt"
    assert adapters_key in framework_prompt, "adapters_key missing from framework prompt"

    # arch key: written by framework
    assert arch_key in framework_prompt, "arch_key missing from framework prompt"


# ---------------------------------------------------------------------------
# Workflow ID
# ---------------------------------------------------------------------------


def test_clean_arch_workflow_id_is_uuid(client):
    """workflow_id should be a valid UUID string."""
    resp = client.post(
        "/workflows/clean-arch",
        json={"feature": "content management system"},
        headers=auth_headers(),
    )
    data = resp.json()
    uuid.UUID(data["workflow_id"])


def test_clean_arch_workflow_two_runs_different_workflow_ids(client):
    """Two workflow submissions should have distinct workflow IDs."""
    resp1 = client.post(
        "/workflows/clean-arch",
        json={"feature": "task X"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/clean-arch",
        json={"feature": "task Y"},
        headers=auth_headers(),
    )
    assert resp1.json()["workflow_id"] != resp2.json()["workflow_id"]


# ---------------------------------------------------------------------------
# OpenAPI schema
# ---------------------------------------------------------------------------


def test_clean_arch_workflow_registered_in_openapi(client):
    """The /workflows/clean-arch endpoint should be listed in the OpenAPI schema."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = schema.get("paths", {})
    assert "/workflows/clean-arch" in paths, (
        f"/workflows/clean-arch not found in OpenAPI paths: {list(paths.keys())}"
    )


def test_clean_arch_workflow_openapi_has_post_method(client):
    """The /workflows/clean-arch endpoint should support POST."""
    resp = client.get("/openapi.json")
    schema = resp.json()
    assert "post" in schema["paths"]["/workflows/clean-arch"]
