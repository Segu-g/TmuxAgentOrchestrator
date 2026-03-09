"""Tests for POST /workflows/ddd — DDD Bounded Context decomposition workflow.

The DDD workflow builds a 3-phase DAG:

  Phase 1 (sequential):
    context-mapper — EventStorming analysis → EVENTSTORMING.md + BOUNDED_CONTEXTS.md

  Phase 2 (parallel, one per context):
    domain-expert-{context} — domain model for assigned context → DOMAIN_{CONTEXT}.md

  Phase 3 (sequential, depends on ALL domain-experts):
    integration-designer — Context Map with mapping patterns → CONTEXT_MAP.md

All handoffs use the shared scratchpad (Blackboard pattern).

Design references:
- Evans, "Domain-Driven Design" (2003): Bounded Context + Ubiquitous Language.
- IJCSE V12I3P102 (2025): EventStorming maps directly to agent communication protocols.
- Russ Miles, "Domain-Driven Agent Design", Engineering Agents Substack, 2025.
- DESIGN.md §10.31 (v1.0.31)
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
from tmux_orchestrator.web.app import create_app, DDDWorkflowSubmit
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
# DDDWorkflowSubmit — model validation
# ---------------------------------------------------------------------------


def test_ddd_submit_empty_topic_rejected():
    """Empty topic should raise ValueError."""
    with pytest.raises(Exception):
        DDDWorkflowSubmit(topic="")


def test_ddd_submit_whitespace_topic_rejected():
    """Whitespace-only topic should raise ValueError."""
    with pytest.raises(Exception):
        DDDWorkflowSubmit(topic="   ")


def test_ddd_submit_blank_context_name_rejected():
    """Blank context name in contexts list should raise ValueError."""
    with pytest.raises(Exception):
        DDDWorkflowSubmit(topic="e-commerce", contexts=["Orders", ""])


def test_ddd_submit_valid_minimal():
    """Minimal valid request should construct with defaults."""
    obj = DDDWorkflowSubmit(topic="e-commerce platform")
    assert obj.topic == "e-commerce platform"
    assert obj.language == "python"
    assert obj.contexts == []
    assert obj.context_mapper_tags == []
    assert obj.domain_expert_tags == []
    assert obj.integration_designer_tags == []
    assert obj.reply_to is None


def test_ddd_submit_with_language():
    """Language field should be accepted."""
    obj = DDDWorkflowSubmit(topic="library management", language="typescript")
    assert obj.language == "typescript"


def test_ddd_submit_with_contexts():
    """Explicit contexts list should be accepted."""
    obj = DDDWorkflowSubmit(topic="e-commerce", contexts=["Orders", "Inventory", "Shipping"])
    assert obj.contexts == ["Orders", "Inventory", "Shipping"]


def test_ddd_submit_with_all_tags():
    """Tags should be accepted on all three roles."""
    obj = DDDWorkflowSubmit(
        topic="hospital system",
        context_mapper_tags=["mapper"],
        domain_expert_tags=["domain"],
        integration_designer_tags=["integration"],
    )
    assert obj.context_mapper_tags == ["mapper"]
    assert obj.domain_expert_tags == ["domain"]
    assert obj.integration_designer_tags == ["integration"]


def test_ddd_submit_with_reply_to():
    """reply_to field should be accepted."""
    obj = DDDWorkflowSubmit(topic="bank system", reply_to="director-1")
    assert obj.reply_to == "director-1"


# ---------------------------------------------------------------------------
# POST /workflows/ddd — HTTP auth
# ---------------------------------------------------------------------------


def test_ddd_workflow_requires_auth(client):
    """Endpoint should return 401 without API key."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "e-commerce"},
    )
    assert resp.status_code == 401


def test_ddd_workflow_wrong_api_key(client):
    """Endpoint should return 401 with wrong API key."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "e-commerce"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_ddd_workflow_empty_topic_returns_422(client):
    """Empty topic should return 422 Unprocessable Entity."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": ""},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_ddd_workflow_missing_topic_returns_422(client):
    """Missing topic field should return 422."""
    resp = client.post(
        "/workflows/ddd",
        json={},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


def test_ddd_workflow_returns_200_without_contexts(client):
    """Valid request without explicit contexts should return 200."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "e-commerce platform"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


def test_ddd_workflow_returns_200_with_contexts(client):
    """Valid request with explicit contexts should return 200."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "e-commerce", "contexts": ["Orders", "Inventory", "Shipping"]},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /workflows/ddd — response structure
# ---------------------------------------------------------------------------


def test_ddd_workflow_response_fields(client):
    """Response must contain workflow_id, name, task_ids, scratchpad_prefix."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "hospital management system"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "workflow_id" in data
    assert "name" in data
    assert "task_ids" in data
    assert "scratchpad_prefix" in data


def test_ddd_workflow_name_starts_with_ddd(client):
    """Workflow name should start with 'ddd/'."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "inventory tracking"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert data["name"].startswith("ddd/")


def test_ddd_workflow_name_contains_topic(client):
    """Workflow name should contain the topic."""
    topic = "supply chain management"
    resp = client.post(
        "/workflows/ddd",
        json={"topic": topic},
        headers=auth_headers(),
    )
    data = resp.json()
    assert topic in data["name"]


def test_ddd_workflow_task_ids_contain_context_mapper(client):
    """task_ids should always contain context_mapper."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "banking platform"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert "context_mapper" in data["task_ids"]


def test_ddd_workflow_task_ids_contain_integration_designer(client):
    """task_ids should always contain integration_designer."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "banking platform"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert "integration_designer" in data["task_ids"]


def test_ddd_workflow_with_contexts_has_domain_expert_keys(client):
    """Explicit contexts should produce domain_expert_{context} keys."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "e-commerce", "contexts": ["Orders", "Inventory"]},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert "domain_expert_orders" in task_ids
    assert "domain_expert_inventory" in task_ids


def test_ddd_workflow_without_contexts_has_auto_key(client):
    """Without explicit contexts, a domain_expert_auto key should be present."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "logistics platform"},
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    assert "domain_expert_auto" in task_ids


def test_ddd_workflow_task_ids_are_strings(client):
    """All task IDs should be non-empty strings."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "banking", "contexts": ["Accounts", "Payments"]},
        headers=auth_headers(),
    )
    data = resp.json()
    for role, tid in data["task_ids"].items():
        assert isinstance(tid, str), f"task_id for {role} is not a string"
        assert len(tid) > 0, f"task_id for {role} is empty"


def test_ddd_workflow_task_ids_distinct(client):
    """All task IDs must be distinct."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "hr system", "contexts": ["Employees", "Payroll"]},
        headers=auth_headers(),
    )
    data = resp.json()
    ids = list(data["task_ids"].values())
    assert len(ids) == len(set(ids)), "task IDs are not distinct"


# ---------------------------------------------------------------------------
# Scratchpad key naming
# ---------------------------------------------------------------------------


def test_ddd_scratchpad_prefix_format(client):
    """Scratchpad prefix should match 'ddd_XXXXXXXX' pattern."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "notification service"},
        headers=auth_headers(),
    )
    data = resp.json()
    assert re.match(r"^ddd_[0-9a-f]{8}$", data["scratchpad_prefix"]), (
        f"unexpected prefix: {data['scratchpad_prefix']}"
    )


def test_ddd_scratchpad_prefix_unique_across_runs(client):
    """Two workflow submissions should produce distinct scratchpad prefixes."""
    resp1 = client.post(
        "/workflows/ddd",
        json={"topic": "system A"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/ddd",
        json={"topic": "system B"},
        headers=auth_headers(),
    )
    assert resp1.json()["scratchpad_prefix"] != resp2.json()["scratchpad_prefix"]


# ---------------------------------------------------------------------------
# Task dependency chain
# ---------------------------------------------------------------------------


def test_ddd_context_mapper_has_no_dependencies(client):
    """context_mapper task should have no depends_on."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "event booking"},
        headers=auth_headers(),
    )
    data = resp.json()
    mapper_id = data["task_ids"]["context_mapper"]

    tasks = _get_tasks(client)
    assert mapper_id in tasks, "context_mapper task not found in /tasks"
    assert tasks[mapper_id].get("depends_on", []) == [], (
        "context_mapper should have no dependencies"
    )


def test_ddd_domain_expert_depends_on_context_mapper(client):
    """domain_expert tasks should depend on the context_mapper task."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "crm system", "contexts": ["Customers", "Sales"]},
        headers=auth_headers(),
    )
    data = resp.json()
    mapper_id = data["task_ids"]["context_mapper"]

    tasks = _get_tasks(client)
    for key, tid in data["task_ids"].items():
        if key.startswith("domain_expert_"):
            assert mapper_id in tasks[tid].get("depends_on", []), (
                f"{key}.depends_on does not contain context_mapper"
            )


def test_ddd_integration_designer_depends_on_domain_experts(client):
    """integration_designer should depend on ALL domain-expert tasks."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "banking", "contexts": ["Accounts", "Payments", "Risk"]},
        headers=auth_headers(),
    )
    data = resp.json()
    integration_id = data["task_ids"]["integration_designer"]
    expert_ids = {
        v for k, v in data["task_ids"].items() if k.startswith("domain_expert_")
    }

    tasks = _get_tasks(client)
    assert integration_id in tasks, "integration_designer task not found in /tasks"
    integration_depends = set(tasks[integration_id].get("depends_on", []))

    assert expert_ids.issubset(integration_depends), (
        f"integration_designer missing domain_expert deps: "
        f"{expert_ids - integration_depends}"
    )


def test_ddd_auto_domain_expert_depends_on_context_mapper(client):
    """Auto domain_expert (no contexts) should depend on context_mapper."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "logistics"},
        headers=auth_headers(),
    )
    data = resp.json()
    mapper_id = data["task_ids"]["context_mapper"]
    auto_id = data["task_ids"]["domain_expert_auto"]

    tasks = _get_tasks(client)
    assert mapper_id in tasks[auto_id].get("depends_on", []), (
        "domain_expert_auto should depend on context_mapper"
    )


# ---------------------------------------------------------------------------
# reply_to propagation
# ---------------------------------------------------------------------------


def test_ddd_workflow_reply_to_passed_to_integration_designer():
    """reply_to should be forwarded to the integration_designer task only."""
    app, orch = _make_app()
    reply_tos: list = []
    original_submit = orch.submit_task

    async def capture_submit(*args, **kwargs):
        reply_tos.append(kwargs.get("reply_to"))
        return await original_submit(*args, **kwargs)

    orch.submit_task = capture_submit  # type: ignore[method-assign]

    with TestClient(app) as c:
        c.post(
            "/workflows/ddd",
            json={
                "topic": "subscription billing",
                "contexts": ["Billing", "Payments"],
                "reply_to": "director-1",
            },
            headers=auth_headers(),
        )

    # Tasks: context_mapper + 2 domain_experts + integration_designer = 4
    assert len(reply_tos) == 4, f"Expected 4 submit_task calls, got {len(reply_tos)}"
    # Only integration_designer (last) should have reply_to
    assert reply_tos[-1] == "director-1", (
        f"reply_to not propagated to integration_designer: {reply_tos}"
    )
    for rt in reply_tos[:-1]:
        assert rt is None, f"earlier task should not have reply_to: {rt}"


def test_ddd_workflow_reply_to_none_by_default(client):
    """reply_to=None should result in no reply_to on any task."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "file storage service", "contexts": ["Files", "Sharing"]},
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


def test_ddd_workflow_mapper_tags_forwarded(client):
    """context_mapper_tags should appear in the context_mapper task required_tags."""
    resp = client.post(
        "/workflows/ddd",
        json={
            "topic": "hr system",
            "contexts": ["Employees", "Payroll"],
            "context_mapper_tags": ["mapper-role"],
            "domain_expert_tags": ["domain-role"],
            "integration_designer_tags": ["integration-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)

    mapper_id = data["task_ids"]["context_mapper"]
    assert "mapper-role" in tasks[mapper_id].get("required_tags", [])


def test_ddd_workflow_domain_expert_tags_forwarded(client):
    """domain_expert_tags should appear in domain_expert task required_tags."""
    resp = client.post(
        "/workflows/ddd",
        json={
            "topic": "crm",
            "contexts": ["Customers", "Deals"],
            "domain_expert_tags": ["domain-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)

    for key, tid in data["task_ids"].items():
        if key.startswith("domain_expert_"):
            assert "domain-role" in tasks[tid].get("required_tags", []), (
                f"{key} missing domain-role tag"
            )


def test_ddd_workflow_integration_designer_tags_forwarded(client):
    """integration_designer_tags should appear in integration_designer required_tags."""
    resp = client.post(
        "/workflows/ddd",
        json={
            "topic": "messaging platform",
            "contexts": ["Users", "Messages"],
            "integration_designer_tags": ["integration-role"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)

    integration_id = data["task_ids"]["integration_designer"]
    assert "integration-role" in tasks[integration_id].get("required_tags", [])


def test_ddd_workflow_empty_tags_result_in_none(client):
    """Empty tag lists should result in no required_tags constraint."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "report generator"},
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


def test_ddd_context_mapper_prompt_mentions_topic(client):
    """Context-mapper prompt should mention the topic."""
    topic = "healthcare records management"
    resp = client.post(
        "/workflows/ddd",
        json={"topic": topic},
        headers=auth_headers(),
    )
    data = resp.json()
    mapper_id = data["task_ids"]["context_mapper"]

    tasks = _get_tasks(client)
    prompt = tasks[mapper_id].get("prompt", "")
    assert topic in prompt, f"topic not found in context_mapper prompt: {prompt[:200]}"


def test_ddd_context_mapper_prompt_mentions_eventstorming(client):
    """Context-mapper prompt should mention EventStorming."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "supply chain"},
        headers=auth_headers(),
    )
    data = resp.json()
    mapper_id = data["task_ids"]["context_mapper"]

    tasks = _get_tasks(client)
    prompt = tasks[mapper_id].get("prompt", "")
    assert "EventStorming" in prompt or "event" in prompt.lower(), (
        "context_mapper prompt should mention EventStorming"
    )


def test_ddd_context_mapper_prompt_mentions_bounded_context(client):
    """Context-mapper prompt should mention Bounded Context."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "ecommerce"},
        headers=auth_headers(),
    )
    data = resp.json()
    mapper_id = data["task_ids"]["context_mapper"]

    tasks = _get_tasks(client)
    prompt = tasks[mapper_id].get("prompt", "")
    assert "Bounded" in prompt or "bounded" in prompt.lower(), (
        "context_mapper prompt should mention Bounded Context"
    )


def test_ddd_domain_expert_prompt_mentions_context_name(client):
    """Domain-expert prompt should mention the assigned context name."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "banking", "contexts": ["Accounts", "Loans"]},
        headers=auth_headers(),
    )
    data = resp.json()
    tasks = _get_tasks(client)

    accounts_id = data["task_ids"]["domain_expert_accounts"]
    prompt = tasks[accounts_id].get("prompt", "")
    assert "Accounts" in prompt, (
        "domain_expert_accounts prompt should mention 'Accounts' context name"
    )


def test_ddd_integration_designer_prompt_mentions_context_map(client):
    """Integration-designer prompt should mention Context Map."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "retail", "contexts": ["Products", "Orders"]},
        headers=auth_headers(),
    )
    data = resp.json()
    integration_id = data["task_ids"]["integration_designer"]

    tasks = _get_tasks(client)
    prompt = tasks[integration_id].get("prompt", "")
    assert "Context Map" in prompt or "context map" in prompt.lower(), (
        "integration_designer prompt should mention Context Map"
    )


def test_ddd_integration_designer_prompt_mentions_acl(client):
    """Integration-designer prompt should mention Anti-Corruption Layer."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "crm", "contexts": ["Customers", "Support"]},
        headers=auth_headers(),
    )
    data = resp.json()
    integration_id = data["task_ids"]["integration_designer"]

    tasks = _get_tasks(client)
    prompt = tasks[integration_id].get("prompt", "")
    assert "ACL" in prompt or "Anti-Corruption" in prompt or "anti-corruption" in prompt.lower(), (
        "integration_designer prompt should mention Anti-Corruption Layer"
    )


def test_ddd_scratchpad_keys_in_prompts(client):
    """Scratchpad keys should appear in the relevant prompts."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "logistics", "contexts": ["Shipping", "Tracking"]},
        headers=auth_headers(),
    )
    data = resp.json()
    prefix = data["scratchpad_prefix"]
    task_ids = data["task_ids"]

    tasks = _get_tasks(client)

    bounded_contexts_key = f"{prefix}_bounded_contexts"
    mapper_prompt = tasks[task_ids["context_mapper"]].get("prompt", "")
    expert_shipping_prompt = tasks[task_ids["domain_expert_shipping"]].get("prompt", "")
    integration_prompt = tasks[task_ids["integration_designer"]].get("prompt", "")

    # bounded_contexts_key appears in context_mapper, domain_experts, integration_designer
    assert bounded_contexts_key in mapper_prompt, (
        "bounded_contexts_key missing from context_mapper prompt"
    )
    assert bounded_contexts_key in expert_shipping_prompt, (
        "bounded_contexts_key missing from domain_expert prompt"
    )
    assert bounded_contexts_key in integration_prompt, (
        "bounded_contexts_key missing from integration_designer prompt"
    )


# ---------------------------------------------------------------------------
# Three-context parallel fan-out
# ---------------------------------------------------------------------------


def test_ddd_three_contexts_creates_three_domain_expert_tasks(client):
    """Three contexts should create three domain_expert tasks."""
    resp = client.post(
        "/workflows/ddd",
        json={
            "topic": "ecommerce",
            "contexts": ["Orders", "Inventory", "Payments"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    task_ids = data["task_ids"]
    expert_keys = [k for k in task_ids if k.startswith("domain_expert_")]
    assert len(expert_keys) == 3, f"Expected 3 domain_expert tasks, got {len(expert_keys)}"


def test_ddd_three_contexts_all_depend_on_mapper(client):
    """All three domain_expert tasks should depend on context_mapper."""
    resp = client.post(
        "/workflows/ddd",
        json={
            "topic": "supply chain",
            "contexts": ["Procurement", "Warehouse", "Distribution"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    mapper_id = data["task_ids"]["context_mapper"]
    tasks = _get_tasks(client)

    for key, tid in data["task_ids"].items():
        if key.startswith("domain_expert_"):
            assert mapper_id in tasks[tid].get("depends_on", []), (
                f"{key} does not depend on context_mapper"
            )


def test_ddd_integration_depends_on_all_three_experts(client):
    """integration_designer should depend on all three domain_expert tasks."""
    resp = client.post(
        "/workflows/ddd",
        json={
            "topic": "airline",
            "contexts": ["Booking", "Fleet", "Operations"],
        },
        headers=auth_headers(),
    )
    data = resp.json()
    integration_id = data["task_ids"]["integration_designer"]
    expert_ids = {v for k, v in data["task_ids"].items() if k.startswith("domain_expert_")}

    tasks = _get_tasks(client)
    integration_depends = set(tasks[integration_id].get("depends_on", []))
    assert expert_ids.issubset(integration_depends), (
        f"integration_designer missing domain_expert deps: {expert_ids - integration_depends}"
    )


# ---------------------------------------------------------------------------
# Workflow ID
# ---------------------------------------------------------------------------


def test_ddd_workflow_id_is_uuid(client):
    """workflow_id should be a valid UUID string."""
    resp = client.post(
        "/workflows/ddd",
        json={"topic": "content management system"},
        headers=auth_headers(),
    )
    data = resp.json()
    uuid.UUID(data["workflow_id"])


def test_ddd_workflow_two_runs_different_workflow_ids(client):
    """Two workflow submissions should have distinct workflow IDs."""
    resp1 = client.post(
        "/workflows/ddd",
        json={"topic": "system X"},
        headers=auth_headers(),
    )
    resp2 = client.post(
        "/workflows/ddd",
        json={"topic": "system Y"},
        headers=auth_headers(),
    )
    assert resp1.json()["workflow_id"] != resp2.json()["workflow_id"]


# ---------------------------------------------------------------------------
# OpenAPI schema
# ---------------------------------------------------------------------------


def test_ddd_workflow_registered_in_openapi(client):
    """The /workflows/ddd endpoint should be listed in the OpenAPI schema."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    paths = schema.get("paths", {})
    assert "/workflows/ddd" in paths, (
        f"/workflows/ddd not found in OpenAPI paths: {list(paths.keys())}"
    )


def test_ddd_workflow_openapi_has_post_method(client):
    """The /workflows/ddd endpoint should support POST."""
    resp = client.get("/openapi.json")
    schema = resp.json()
    assert "post" in schema["paths"]["/workflows/ddd"]
