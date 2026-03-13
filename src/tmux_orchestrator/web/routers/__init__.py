"""APIRouter modules for the TmuxAgentOrchestrator web layer.

Each sub-module exports a ``build_<domain>_router()`` factory that returns a
configured :class:`fastapi.APIRouter`.  The factory accepts the shared
``orchestrator`` object and the ``auth`` dependency callable so that each
router can enforce authentication without coupling to the main ``create_app``
closure.

Reference: FastAPI "Bigger Applications - Multiple Files"
https://fastapi.tiangolo.com/tutorial/bigger-applications/
DESIGN.md §10.42 (v1.1.6)
"""
from tmux_orchestrator.web.routers.agents import build_agents_router
from tmux_orchestrator.web.routers.groups import build_groups_router
from tmux_orchestrator.web.routers.memory import build_memory_router
from tmux_orchestrator.web.routers.scratchpad import build_scratchpad_router
from tmux_orchestrator.web.routers.staging import build_staging_router
from tmux_orchestrator.web.routers.system import build_system_router
from tmux_orchestrator.web.routers.tasks import build_tasks_router
from tmux_orchestrator.web.routers.templates import build_templates_router
from tmux_orchestrator.web.routers.webhooks import build_webhooks_router
from tmux_orchestrator.web.routers.workflows import build_workflows_router

__all__ = [
    "build_agents_router",
    "build_groups_router",
    "build_memory_router",
    "build_scratchpad_router",
    "build_staging_router",
    "build_system_router",
    "build_tasks_router",
    "build_templates_router",
    "build_webhooks_router",
    "build_workflows_router",
]
