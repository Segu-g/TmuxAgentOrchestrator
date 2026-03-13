"""Templates APIRouter — /templates/* endpoints.

Provides CRUD for task templates plus render-preview and render+submit shortcuts.

Design reference: DESIGN.md §10.93 (v1.2.17)

References:
- Paul Serban "Engineering a Scalable Prompt Library" (paulserban.eu, 2025)
- Salesforce Einstein Prompt Templates REST API (developer.salesforce.com, 2024)
- PromptLayer Template Variables (docs.promptlayer.com, 2025)
- FastAPI "Bigger Applications": https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from tmux_orchestrator.application.template_store import TaskTemplate, TemplateStore
from tmux_orchestrator.web.schemas import (
    TaskTemplateCreate,
    TaskTemplateRender,
    TaskTemplateSubmit,
)


def _tmpl_to_dict(tmpl: TaskTemplate) -> dict:
    """Serialize a TaskTemplate to a JSON-compatible dict."""
    return {
        "id": tmpl.id,
        "name": tmpl.name,
        "prompt_template": tmpl.prompt_template,
        "variables": tmpl.variables,
        "default_priority": tmpl.default_priority,
        "default_tags": tmpl.default_tags,
        "default_timeout": tmpl.default_timeout,
        "description": tmpl.description,
    }


def build_templates_router(
    orchestrator: Any,
    auth: Callable,
    *,
    template_store: TemplateStore,
) -> APIRouter:
    """Build and return the templates APIRouter.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
        Used by the ``submit`` endpoint to enqueue the rendered prompt.
    auth:
        Authentication dependency callable.
    template_store:
        Shared :class:`~tmux_orchestrator.application.template_store.TemplateStore`
        instance.
    """
    router = APIRouter()

    # ------------------------------------------------------------------
    # POST /templates — register a new template
    # ------------------------------------------------------------------

    @router.post(
        "/templates",
        summary="Register a new task template",
        status_code=201,
        dependencies=[Depends(auth)],
    )
    async def create_template(body: TaskTemplateCreate) -> dict:
        """Register a parameterized task prompt template.

        The ``variables`` list declares all ``{placeholder}`` names that must
        be supplied when rendering or submitting this template.

        Returns the created template object with HTTP 201 Created.

        Design reference: DESIGN.md §10.93 (v1.2.17)
        """
        tmpl = TaskTemplate(
            id=body.id,
            name=body.name,
            prompt_template=body.prompt_template,
            variables=list(body.variables),
            default_priority=body.default_priority,
            default_tags=list(body.default_tags),
            default_timeout=body.default_timeout,
            description=body.description,
        )
        template_store.register(tmpl)
        return _tmpl_to_dict(tmpl)

    # ------------------------------------------------------------------
    # GET /templates — list all templates
    # ------------------------------------------------------------------

    @router.get(
        "/templates",
        summary="List all registered task templates",
        dependencies=[Depends(auth)],
    )
    async def list_templates() -> list[dict]:
        """Return all registered task templates.

        Design reference: DESIGN.md §10.93 (v1.2.17)
        """
        return [_tmpl_to_dict(t) for t in template_store.list_all()]

    # ------------------------------------------------------------------
    # GET /templates/{id} — get a template by ID
    # ------------------------------------------------------------------

    @router.get(
        "/templates/{template_id}",
        summary="Get a task template by ID",
        dependencies=[Depends(auth)],
    )
    async def get_template(template_id: str) -> dict:
        """Return the template with *template_id*.

        Raises 404 if the template is not registered.

        Design reference: DESIGN.md §10.93 (v1.2.17)
        """
        tmpl = template_store.get(template_id)
        if tmpl is None:
            raise HTTPException(
                status_code=404,
                detail=f"Template {template_id!r} not found",
            )
        return _tmpl_to_dict(tmpl)

    # ------------------------------------------------------------------
    # DELETE /templates/{id} — delete a template
    # ------------------------------------------------------------------

    @router.delete(
        "/templates/{template_id}",
        summary="Delete a task template",
        dependencies=[Depends(auth)],
    )
    async def delete_template(template_id: str) -> dict:
        """Remove *template_id* from the registry.

        Returns ``{"deleted": true}`` on success or 404 if not found.

        Design reference: DESIGN.md §10.93 (v1.2.17)
        """
        removed = template_store.delete(template_id)
        if not removed:
            raise HTTPException(
                status_code=404,
                detail=f"Template {template_id!r} not found",
            )
        return {"deleted": True, "template_id": template_id}

    # ------------------------------------------------------------------
    # POST /templates/{id}/render — render preview (no submit)
    # ------------------------------------------------------------------

    @router.post(
        "/templates/{template_id}/render",
        summary="Render a template with variables (preview only — no task submission)",
        dependencies=[Depends(auth)],
    )
    async def render_template(template_id: str, body: TaskTemplateRender) -> dict:
        """Render *template_id* with the provided *variables* and return the result.

        This is a dry-run endpoint — no task is submitted.  Use it to preview
        the rendered prompt before submitting.

        Returns ``{"template_id": ..., "rendered_prompt": ...}``.

        Raises 404 if the template is not found; 422 if required variables
        are missing.

        Design reference: DESIGN.md §10.93 (v1.2.17)
        """
        tmpl = template_store.get(template_id)
        if tmpl is None:
            raise HTTPException(
                status_code=404,
                detail=f"Template {template_id!r} not found",
            )
        try:
            rendered = template_store.render(template_id, body.variables)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "template_id": template_id,
            "rendered_prompt": rendered,
        }

    # ------------------------------------------------------------------
    # POST /templates/{id}/submit — render + submit as a task
    # ------------------------------------------------------------------

    @router.post(
        "/templates/{template_id}/submit",
        summary="Render a template and submit the result as a new task",
        dependencies=[Depends(auth)],
    )
    async def submit_template(template_id: str, body: TaskTemplateSubmit) -> dict:
        """Render *template_id* with *variables*, then enqueue as a task.

        Priority, tags, and timeout can be overridden per-submission;
        when omitted, the template's defaults are used.

        Returns the submitted task record (same shape as ``POST /tasks``).

        Raises 404 if the template is not found; 422 if required variables
        are missing or substitution fails.

        Design reference: DESIGN.md §10.93 (v1.2.17)
        """
        tmpl = template_store.get(template_id)
        if tmpl is None:
            raise HTTPException(
                status_code=404,
                detail=f"Template {template_id!r} not found",
            )
        try:
            rendered = template_store.render(template_id, body.variables)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Resolve submission parameters: per-request overrides win over defaults.
        priority = body.priority if body.priority is not None else tmpl.default_priority
        required_tags = body.required_tags if body.required_tags else list(tmpl.default_tags)
        timeout = body.timeout if body.timeout is not None else tmpl.default_timeout

        task = await orchestrator.submit_task(
            rendered,
            priority=priority,
            reply_to=body.reply_to,
            target_agent=body.target_agent,
            required_tags=required_tags or None,
            timeout=timeout,
        )
        return {
            "task_id": task.id,
            "prompt": task.prompt,
            "priority": task.priority,
            "template_id": template_id,
            "submitted_at": task.submitted_at,
            **({"required_tags": task.required_tags} if task.required_tags else {}),
            **({"target_agent": task.target_agent} if task.target_agent else {}),
            **({"reply_to": task.reply_to} if task.reply_to else {}),
        }

    return router
