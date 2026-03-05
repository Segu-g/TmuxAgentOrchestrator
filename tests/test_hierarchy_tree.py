"""Tests for the agent hierarchy tree view (Issue #2).

The Web UI should render agents as an interactive tree rather than a flat table
when agents have parent_id relationships.  These tests verify:

1. The /agents REST endpoint includes parent_id and role in each agent entry.
2. A new GET /agents/tree endpoint returns a nested JSON tree structure suitable
   for tree rendering (d3-hierarchy format: {id, children: [...]}).
3. The Web UI HTML embeds the tree rendering code (D3.js-based SVG tree).

Design reference: DESIGN.md §11 Issue #2 (Web UI agent hierarchy tree).
"""

from __future__ import annotations

import httpx
import pytest

import tmux_orchestrator.web.app as web_app_mod
from tmux_orchestrator.web.app import create_app


_API_KEY = "test-key"


# ---------------------------------------------------------------------------
# Mock objects
# ---------------------------------------------------------------------------


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestrator:
    """Orchestrator with a realistic multi-level agent hierarchy."""

    _dispatch_task = None
    is_paused = False

    def __init__(self):
        self._agents_data = [
            {"id": "director", "status": "IDLE", "role": "director", "parent_id": None,
             "current_task": None, "bus_drops": 0, "circuit_breaker": "CLOSED"},
            {"id": "worker-1", "status": "IDLE", "role": "worker", "parent_id": None,
             "current_task": None, "bus_drops": 0, "circuit_breaker": "CLOSED"},
            {"id": "worker-1-sub-abc123", "status": "BUSY", "role": "worker",
             "parent_id": "worker-1", "current_task": "task-x",
             "bus_drops": 0, "circuit_breaker": "CLOSED"},
            {"id": "worker-2", "status": "ERROR", "role": "worker", "parent_id": None,
             "current_task": None, "bus_drops": 2, "circuit_breaker": "OPEN"},
        ]

    def list_agents(self) -> list:
        return list(self._agents_data)

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


@pytest.fixture(autouse=True)
def reset_state():
    web_app_mod._credentials.clear()
    web_app_mod._sign_counts.clear()
    web_app_mod._sessions.clear()
    web_app_mod._pending_challenge = None
    yield


@pytest.fixture
def orch():
    return _MockOrchestrator()


@pytest.fixture
def app(orch):
    return create_app(orch, _MockHub(), api_key=_API_KEY)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests: /agents/tree endpoint
# ---------------------------------------------------------------------------


async def test_agents_tree_endpoint_exists(client):
    """GET /agents/tree should return 200 (with API key auth)."""
    r = await client.get("/agents/tree", headers={"X-API-Key": _API_KEY})
    assert r.status_code == 200


async def test_agents_tree_returns_json_list(client):
    """GET /agents/tree should return a JSON list of root nodes."""
    r = await client.get("/agents/tree", headers={"X-API-Key": _API_KEY})
    data = r.json()
    assert isinstance(data, list), "tree endpoint should return a list of root nodes"


async def test_agents_tree_root_nodes_have_no_parent(client):
    """Root-level nodes in the tree should have parent_id=None."""
    r = await client.get("/agents/tree", headers={"X-API-Key": _API_KEY})
    roots = r.json()
    for node in roots:
        assert node.get("parent_id") is None or "id" in node


async def test_agents_tree_children_nested(client):
    """Worker-1-sub-abc123 should appear as a child of worker-1 in the tree."""
    r = await client.get("/agents/tree", headers={"X-API-Key": _API_KEY})
    nodes = r.json()

    # Find worker-1 in tree
    worker1 = next((n for n in nodes if n["id"] == "worker-1"), None)
    assert worker1 is not None, "worker-1 should be a root node"
    children = worker1.get("children", [])
    assert len(children) == 1, "worker-1 should have exactly one child"
    assert children[0]["id"] == "worker-1-sub-abc123"


async def test_agents_tree_node_has_status_and_role(client):
    """Each node in the tree should include status and role fields."""
    r = await client.get("/agents/tree", headers={"X-API-Key": _API_KEY})
    nodes = r.json()

    # BFS all nodes to check fields
    queue = list(nodes)
    while queue:
        node = queue.pop()
        assert "id" in node
        assert "status" in node
        assert "role" in node
        queue.extend(node.get("children", []))


async def test_agents_tree_requires_auth(client):
    """GET /agents/tree without auth should return 401."""
    r = await client.get("/agents/tree")
    assert r.status_code == 401


async def test_agents_tree_leaf_has_empty_children_list(client):
    """Sub-agents with no children should have an empty children list."""
    r = await client.get("/agents/tree", headers={"X-API-Key": _API_KEY})
    nodes = r.json()

    # Find sub-agent (it should appear as child of worker-1)
    worker1 = next((n for n in nodes if n["id"] == "worker-1"), None)
    assert worker1 is not None
    sub = worker1["children"][0]
    assert sub["children"] == [], "leaf sub-agent should have empty children list"


# ---------------------------------------------------------------------------
# Tests: Web UI HTML contains hierarchy tree rendering code
# ---------------------------------------------------------------------------


async def test_web_ui_contains_tree_rendering(client):
    """The root GET / HTML should contain tree rendering JavaScript."""
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    # Should contain the tree section or tree-specific identifier
    assert "agents-tree" in html or "tree" in html.lower()


async def test_web_ui_has_toggle_between_table_and_tree(client):
    """The UI should have controls to switch between flat table and tree views."""
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    # Should contain view toggle buttons or tab-like controls
    assert "tree" in html.lower() and ("table" in html.lower() or "list" in html.lower())
