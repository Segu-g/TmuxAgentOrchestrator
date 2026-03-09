"""Backward-compatibility shim for the system factory.

The canonical implementation now lives in ``tmux_orchestrator.application.factory``.
This module re-exports everything so that old import paths continue to work:

    from tmux_orchestrator.factory import build_system   # still works

DESIGN.md §10.60 (v1.1.28 — Clean Architecture Phase 6: factory.py migration)
Note: Do NOT delete this shim — it is part of the Strangler Fig migration strategy.
"""
from tmux_orchestrator.application.factory import (  # noqa: F401
    build_system,
    patch_api_key,
    patch_web_url,
)

# Re-export the internal helper for any test that patches it via this module
from tmux_orchestrator.application.factory import _resolve_system_prompt  # noqa: F401

__all__ = ["build_system", "patch_api_key", "patch_web_url", "_resolve_system_prompt"]
