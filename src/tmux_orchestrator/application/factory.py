"""Application-layer re-export of the system factory.

The factory implementation lives in ``tmux_orchestrator.factory`` (root) to
preserve test-patch compatibility (many tests patch
``tmux_orchestrator.factory.TmuxInterface``).  This module re-exports the
public API so that callers can use the canonical application-layer path:

    from tmux_orchestrator.application.factory import build_system

DESIGN.md §10.59 (v1.1.27 — Clean Architecture Phase 5)
Note: factory.py is a Composition Root and intentionally crosses layers.
      Implementation kept at root for test backward-compatibility.
      Full migration to application/ will occur when tests are updated.
"""
from tmux_orchestrator.factory import (  # noqa: F401
    build_system,
    patch_api_key,
    patch_web_url,
)

__all__ = ["build_system", "patch_api_key", "patch_web_url"]
