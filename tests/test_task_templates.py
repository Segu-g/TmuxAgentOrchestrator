"""Tests for v1.2.17 — Task Templates + Preset Library.

Covers:
- TemplateStore CRUD operations
- TemplateStore.render() success and failure paths
- REST endpoints: POST /templates, GET /templates, GET /templates/{id},
  DELETE /templates/{id}, POST /templates/{id}/render,
  POST /templates/{id}/submit

Design reference: DESIGN.md §10.93 (v1.2.17)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from tmux_orchestrator.application.template_store import TaskTemplate, TemplateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_template(
    tmpl_id: str = "hello",
    name: str = "Hello",
    prompt_template: str = "Hello, {name}!",
    variables: list[str] | None = None,
) -> TaskTemplate:
    return TaskTemplate(
        id=tmpl_id,
        name=name,
        prompt_template=prompt_template,
        variables=variables if variables is not None else ["name"],
    )


def _make_app(template_store: TemplateStore | None = None):
    """Build a minimal FastAPI app with only the templates router for testing."""
    from fastapi import FastAPI
    from tmux_orchestrator.web.routers.templates import build_templates_router

    if template_store is None:
        template_store = TemplateStore()

    # Minimal mock orchestrator with a submit_task method.
    mock_orch = MagicMock()
    task_mock = MagicMock()
    task_mock.id = "task-001"
    task_mock.prompt = "rendered prompt"
    task_mock.priority = 0
    task_mock.submitted_at = "2026-03-14T00:00:00+00:00"
    task_mock.required_tags = []
    task_mock.target_agent = None
    task_mock.reply_to = None
    mock_orch.submit_task = AsyncMock(return_value=task_mock)

    app = FastAPI()

    async def no_auth():
        pass

    app.include_router(
        build_templates_router(mock_orch, no_auth, template_store=template_store)
    )
    return app, template_store, mock_orch


# ---------------------------------------------------------------------------
# TemplateStore unit tests
# ---------------------------------------------------------------------------


class TestTemplateStoreRegister:
    def test_register_stores_template(self):
        store = TemplateStore()
        tmpl = _make_template()
        store.register(tmpl)
        assert store.get("hello") is tmpl

    def test_register_overwrites_existing(self):
        store = TemplateStore()
        t1 = _make_template(name="Original")
        t2 = _make_template(name="Updated")
        store.register(t1)
        store.register(t2)
        assert store.get("hello").name == "Updated"


class TestTemplateStoreGet:
    def test_get_returns_correct_template(self):
        store = TemplateStore()
        tmpl = _make_template(tmpl_id="mytemplate")
        store.register(tmpl)
        result = store.get("mytemplate")
        assert result is tmpl

    def test_get_returns_none_for_unknown_id(self):
        store = TemplateStore()
        assert store.get("nonexistent") is None


class TestTemplateStoreListAll:
    def test_list_all_returns_all_templates(self):
        store = TemplateStore()
        t1 = _make_template("t1")
        t2 = _make_template("t2")
        store.register(t1)
        store.register(t2)
        ids = {t.id for t in store.list_all()}
        assert ids == {"t1", "t2"}

    def test_list_all_empty_when_no_templates(self):
        store = TemplateStore()
        assert store.list_all() == []


class TestTemplateStoreRender:
    def test_render_substitutes_variables_correctly(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="greet",
                name="Greet",
                prompt_template="Hello, {name}! You are a {role}.",
                variables=["name", "role"],
            )
        )
        result = store.render("greet", {"name": "Alice", "role": "developer"})
        assert result == "Hello, Alice! You are a developer."

    def test_render_raises_key_error_for_unknown_template(self):
        store = TemplateStore()
        with pytest.raises(KeyError, match="unknown"):
            store.render("unknown", {})

    def test_render_raises_value_error_for_missing_variables(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="t",
                name="T",
                prompt_template="Hello, {name}! Review {aspect}.",
                variables=["name", "aspect"],
            )
        )
        with pytest.raises(ValueError, match="Missing required variables"):
            store.render("t", {"name": "Alice"})  # missing "aspect"

    def test_render_raises_value_error_lists_all_missing(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="t",
                name="T",
                prompt_template="Fix {bug} in {language} with {approach}.",
                variables=["bug", "language", "approach"],
            )
        )
        with pytest.raises(ValueError) as exc_info:
            store.render("t", {})
        msg = str(exc_info.value)
        assert "approach" in msg
        assert "bug" in msg
        assert "language" in msg


class TestTemplateStoreDelete:
    def test_delete_removes_template_returns_true(self):
        store = TemplateStore()
        store.register(_make_template())
        assert store.delete("hello") is True
        assert store.get("hello") is None

    def test_delete_returns_false_for_unknown_id(self):
        store = TemplateStore()
        assert store.delete("nonexistent") is False


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------


class TestPostTemplates:
    def test_create_template_returns_201(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/templates",
            json={
                "id": "test_tmpl",
                "name": "Test Template",
                "prompt_template": "Do {action} for {target}.",
                "variables": ["action", "target"],
                "description": "A test template",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "test_tmpl"
        assert data["name"] == "Test Template"
        assert data["variables"] == ["action", "target"]

    def test_create_template_is_retrievable(self):
        app, store, _ = _make_app()
        client = TestClient(app)
        client.post(
            "/templates",
            json={
                "id": "my_tmpl",
                "name": "My Template",
                "prompt_template": "Hello {world}.",
                "variables": ["world"],
            },
        )
        assert store.get("my_tmpl") is not None


class TestGetTemplates:
    def test_list_templates_returns_all(self):
        store = TemplateStore()
        store.register(_make_template("t1", name="T1"))
        store.register(_make_template("t2", name="T2"))
        app, _, _ = _make_app(template_store=store)
        client = TestClient(app)
        resp = client.get("/templates")
        assert resp.status_code == 200
        ids = {t["id"] for t in resp.json()}
        assert {"t1", "t2"}.issubset(ids)

    def test_list_templates_empty_initially(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.get("/templates")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetTemplateById:
    def test_get_template_by_id_returns_detail(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="my_review",
                name="My Review",
                prompt_template="Review {code} for {aspect}.",
                variables=["code", "aspect"],
                description="Review template",
            )
        )
        app, _, _ = _make_app(template_store=store)
        client = TestClient(app)
        resp = client.get("/templates/my_review")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "my_review"
        assert data["description"] == "Review template"

    def test_get_template_by_id_404_when_not_found(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.get("/templates/nonexistent")
        assert resp.status_code == 404


class TestDeleteTemplate:
    def test_delete_template_returns_success(self):
        store = TemplateStore()
        store.register(_make_template("to_delete"))
        app, _, _ = _make_app(template_store=store)
        client = TestClient(app)
        resp = client.delete("/templates/to_delete")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert store.get("to_delete") is None

    def test_delete_template_404_when_not_found(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.delete("/templates/nonexistent")
        assert resp.status_code == 404


class TestRenderTemplate:
    def test_render_returns_rendered_prompt(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="greet",
                name="Greet",
                prompt_template="Hello, {name}!",
                variables=["name"],
            )
        )
        app, _, _ = _make_app(template_store=store)
        client = TestClient(app)
        resp = client.post("/templates/greet/render", json={"variables": {"name": "World"}})
        assert resp.status_code == 200
        data = resp.json()
        assert data["rendered_prompt"] == "Hello, World!"
        assert data["template_id"] == "greet"

    def test_render_422_when_variables_missing(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="greet",
                name="Greet",
                prompt_template="Hello, {name}!",
                variables=["name"],
            )
        )
        app, _, _ = _make_app(template_store=store)
        client = TestClient(app)
        resp = client.post("/templates/greet/render", json={"variables": {}})
        assert resp.status_code == 422

    def test_render_404_when_template_not_found(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.post("/templates/nonexistent/render", json={"variables": {}})
        assert resp.status_code == 404


class TestSubmitTemplate:
    def test_submit_renders_and_submits_task(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="review",
                name="Review",
                prompt_template="Review {code} for {aspect}.",
                variables=["code", "aspect"],
            )
        )
        app, _, mock_orch = _make_app(template_store=store)
        client = TestClient(app)
        resp = client.post(
            "/templates/review/submit",
            json={"variables": {"code": "x=1", "aspect": "correctness"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "task-001"
        assert data["template_id"] == "review"
        # Verify submit_task was called
        mock_orch.submit_task.assert_called_once()
        call_args = mock_orch.submit_task.call_args
        assert "Review x=1 for correctness." == call_args.args[0]

    def test_submit_404_when_template_not_found(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/templates/nonexistent/submit",
            json={"variables": {}},
        )
        assert resp.status_code == 404

    def test_submit_422_when_variables_missing(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="review",
                name="Review",
                prompt_template="Review {code} for {aspect}.",
                variables=["code", "aspect"],
            )
        )
        app, _, _ = _make_app(template_store=store)
        client = TestClient(app)
        resp = client.post(
            "/templates/review/submit",
            json={"variables": {"code": "x=1"}},  # missing "aspect"
        )
        assert resp.status_code == 422

    def test_submit_uses_default_priority_when_not_overridden(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="review",
                name="Review",
                prompt_template="Review {code}.",
                variables=["code"],
                default_priority=5,
            )
        )
        app, _, mock_orch = _make_app(template_store=store)
        client = TestClient(app)
        client.post(
            "/templates/review/submit",
            json={"variables": {"code": "x=1"}},
        )
        call_kwargs = mock_orch.submit_task.call_args.kwargs
        assert call_kwargs["priority"] == 5

    def test_submit_overrides_priority_when_specified(self):
        store = TemplateStore()
        store.register(
            TaskTemplate(
                id="review",
                name="Review",
                prompt_template="Review {code}.",
                variables=["code"],
                default_priority=5,
            )
        )
        app, _, mock_orch = _make_app(template_store=store)
        client = TestClient(app)
        client.post(
            "/templates/review/submit",
            json={"variables": {"code": "x=1"}, "priority": 1},
        )
        call_kwargs = mock_orch.submit_task.call_args.kwargs
        assert call_kwargs["priority"] == 1


# ---------------------------------------------------------------------------
# Built-in templates (smoke tests via app.py _init_template_store)
# ---------------------------------------------------------------------------


class TestBuiltinTemplates:
    def test_builtin_templates_are_registered(self):
        from tmux_orchestrator.web.app import _init_template_store, TemplateStore

        store = TemplateStore()
        _init_template_store(store)
        ids = {t.id for t in store.list_all()}
        assert "code_review" in ids
        assert "bug_fix" in ids
        assert "write_tests" in ids

    def test_init_template_store_is_idempotent(self):
        from tmux_orchestrator.web.app import _init_template_store, TemplateStore

        store = TemplateStore()
        _init_template_store(store)
        _init_template_store(store)  # Second call must not duplicate
        code_review_count = sum(
            1 for t in store.list_all() if t.id == "code_review"
        )
        assert code_review_count == 1

    def test_code_review_template_renders(self):
        from tmux_orchestrator.web.app import _init_template_store, TemplateStore

        store = TemplateStore()
        _init_template_store(store)
        rendered = store.render(
            "code_review",
            {"language": "Python", "aspect": "correctness", "code": "x=1"},
        )
        assert "Python" in rendered
        assert "correctness" in rendered
        assert "x=1" in rendered

    def test_write_tests_template_renders(self):
        from tmux_orchestrator.web.app import _init_template_store, TemplateStore

        store = TemplateStore()
        _init_template_store(store)
        rendered = store.render(
            "write_tests",
            {
                "framework": "pytest",
                "language": "Python",
                "code": "def add(a,b): return a+b",
                "module": "calculator",
            },
        )
        assert "pytest" in rendered
        assert "test_calculator.py" in rendered
