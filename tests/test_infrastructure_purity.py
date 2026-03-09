"""AST-based tests enforcing Clean Architecture dependency rule.

Verifies that ``domain/`` and ``application/`` sub-packages do NOT import
from ``infrastructure/``.  This is the core constraint of Clean Architecture
(Martin, 2017, Ch. 22): source-code dependencies must point inward only.

    domain/ ← application/ ← infrastructure/

References:
    - Martin, Robert C. "Clean Architecture" (2017) Ch. 22
    - Sourcery AI, "Maintain A Clean Architecture With Dependency Rules"
      https://www.sourcery.ai/blog/dependency-rules
    - DESIGN.md §10.N (v1.0.16 — infrastructure/ layer extraction)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SRC = Path(__file__).parent.parent / "src" / "tmux_orchestrator"
INFRA_PREFIX = "tmux_orchestrator.infrastructure"


def _collect_imports(path: Path) -> list[tuple[str, int, str]]:
    """Return list of (module, lineno, source_file) for all imports in *path*.

    Walks all .py files under *path* and collects both ``import X`` and
    ``from X import Y`` statements.
    """
    results: list[tuple[str, int, str]] = []
    for py_file in sorted(path.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    results.append((alias.name, node.lineno, str(py_file)))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                results.append((module, node.lineno, str(py_file)))
    return results


def _infra_violations(layer_path: Path) -> list[str]:
    """Return human-readable violation strings for infrastructure imports."""
    violations = []
    for module, lineno, src_file in _collect_imports(layer_path):
        if module == INFRA_PREFIX or module.startswith(INFRA_PREFIX + "."):
            rel = Path(src_file).relative_to(SRC.parent.parent)
            violations.append(f"  {rel}:{lineno} imports '{module}'")
    return violations


# ---------------------------------------------------------------------------
# Tests: domain/ must not import infrastructure/
# ---------------------------------------------------------------------------

class TestDomainPurity:
    def test_domain_does_not_import_infrastructure(self) -> None:
        """domain/ modules must not import from infrastructure/."""
        domain_path = SRC / "domain"
        assert domain_path.is_dir(), f"domain/ not found at {domain_path}"
        violations = _infra_violations(domain_path)
        assert not violations, (
            "domain/ imports infrastructure/ — violates Clean Architecture:\n"
            + "\n".join(violations)
        )

    def test_domain_files_exist(self) -> None:
        """Sanity check: domain/ has Python files to inspect."""
        domain_path = SRC / "domain"
        py_files = list(domain_path.rglob("*.py"))
        assert py_files, "domain/ contains no .py files — test would be vacuously true"


# ---------------------------------------------------------------------------
# Tests: application/ must not import infrastructure/
# ---------------------------------------------------------------------------

class TestApplicationPurity:
    def test_application_does_not_import_infrastructure(self) -> None:
        """application/ modules must not import from infrastructure/."""
        app_path = SRC / "application"
        assert app_path.is_dir(), f"application/ not found at {app_path}"
        violations = _infra_violations(app_path)
        assert not violations, (
            "application/ imports infrastructure/ — violates Clean Architecture:\n"
            + "\n".join(violations)
        )

    def test_application_files_exist(self) -> None:
        """Sanity check: application/ has Python files to inspect."""
        app_path = SRC / "application"
        py_files = list(app_path.rglob("*.py"))
        assert py_files, "application/ contains no .py files — test would be vacuously true"


# ---------------------------------------------------------------------------
# Tests: infrastructure/ package structure
# ---------------------------------------------------------------------------

class TestInfrastructurePackage:
    def test_infrastructure_package_exists(self) -> None:
        """infrastructure/ sub-package must exist."""
        infra_path = SRC / "infrastructure"
        assert infra_path.is_dir(), f"infrastructure/ not found at {infra_path}"

    def test_infrastructure_init_exists(self) -> None:
        """infrastructure/__init__.py must exist."""
        init = SRC / "infrastructure" / "__init__.py"
        assert init.is_file(), f"infrastructure/__init__.py not found at {init}"

    def test_infrastructure_tmux_exists(self) -> None:
        """infrastructure/tmux.py must exist (canonical TmuxInterface home)."""
        tmux_py = SRC / "infrastructure" / "tmux.py"
        assert tmux_py.is_file(), f"infrastructure/tmux.py not found at {tmux_py}"

    def test_infrastructure_claude_trust_exists(self) -> None:
        """infrastructure/claude_trust.py must exist (canonical pre_trust_worktree home)."""
        trust_py = SRC / "infrastructure" / "claude_trust.py"
        assert trust_py.is_file(), f"infrastructure/claude_trust.py not found at {trust_py}"

    def test_tmux_interface_is_importable(self) -> None:
        """TmuxInterface is importable from both canonical and shim paths."""
        from tmux_orchestrator.infrastructure.tmux import TmuxInterface as T1
        from tmux_orchestrator.tmux_interface import TmuxInterface as T2
        assert T1 is T2, "shim re-export must be the same class as the canonical one"

    def test_pre_trust_worktree_is_importable(self) -> None:
        """pre_trust_worktree is importable from both canonical and shim paths."""
        from tmux_orchestrator.infrastructure.claude_trust import pre_trust_worktree as f1
        from tmux_orchestrator.trust import pre_trust_worktree as f2
        assert f1 is f2, "shim re-export must be the same function as the canonical one"

    def test_pane_output_event_is_importable(self) -> None:
        """PaneOutputEvent is importable from infrastructure."""
        from tmux_orchestrator.infrastructure.tmux import PaneOutputEvent
        assert PaneOutputEvent is not None

    def test_poll_interval_is_importable(self) -> None:
        """POLL_INTERVAL constant is importable from infrastructure."""
        from tmux_orchestrator.infrastructure.tmux import POLL_INTERVAL
        assert isinstance(POLL_INTERVAL, float)


# ---------------------------------------------------------------------------
# Tests: shim files re-export correctly
# ---------------------------------------------------------------------------

class TestShims:
    def test_tmux_interface_shim_re_exports_all_public_names(self) -> None:
        """tmux_interface shim exposes TmuxInterface, PaneOutputEvent, POLL_INTERVAL."""
        import tmux_orchestrator.tmux_interface as shim
        assert hasattr(shim, "TmuxInterface")
        assert hasattr(shim, "PaneOutputEvent")
        assert hasattr(shim, "POLL_INTERVAL")

    def test_trust_shim_re_exports_pre_trust_worktree(self) -> None:
        """trust shim exposes pre_trust_worktree."""
        import tmux_orchestrator.trust as shim
        assert hasattr(shim, "pre_trust_worktree")

    def test_infrastructure_init_re_exports(self) -> None:
        """infrastructure.__init__ re-exports the four key public names."""
        from tmux_orchestrator import infrastructure
        assert hasattr(infrastructure, "TmuxInterface")
        assert hasattr(infrastructure, "PaneOutputEvent")
        assert hasattr(infrastructure, "POLL_INTERVAL")
        assert hasattr(infrastructure, "pre_trust_worktree")

    def test_worktree_shim_re_exports_worktree_manager(self) -> None:
        """worktree shim exposes WorktreeManager."""
        import tmux_orchestrator.worktree as shim
        assert hasattr(shim, "WorktreeManager")

    def test_process_port_shim_re_exports_all_public_names(self) -> None:
        """process_port shim exposes ProcessPort, TmuxProcessAdapter, StdioProcessAdapter."""
        import tmux_orchestrator.process_port as shim
        assert hasattr(shim, "ProcessPort")
        assert hasattr(shim, "TmuxProcessAdapter")
        assert hasattr(shim, "StdioProcessAdapter")

    def test_messaging_shim_re_exports_mailbox(self) -> None:
        """messaging shim exposes Mailbox."""
        import tmux_orchestrator.messaging as shim
        assert hasattr(shim, "Mailbox")

    def test_infrastructure_init_re_exports_v1017_additions(self) -> None:
        """infrastructure.__init__ re-exports WorktreeManager, ProcessPort, Mailbox (v1.0.17)."""
        from tmux_orchestrator import infrastructure
        assert hasattr(infrastructure, "WorktreeManager")
        assert hasattr(infrastructure, "ProcessPort")
        assert hasattr(infrastructure, "TmuxProcessAdapter")
        assert hasattr(infrastructure, "StdioProcessAdapter")
        assert hasattr(infrastructure, "Mailbox")


# ---------------------------------------------------------------------------
# Tests: canonical infrastructure module locations (v1.0.17)
# ---------------------------------------------------------------------------

class TestInfrastructureCanonicalModulesV1017:
    def test_infrastructure_worktree_exists(self) -> None:
        """infrastructure/worktree.py must exist (canonical WorktreeManager home)."""
        worktree_py = SRC / "infrastructure" / "worktree.py"
        assert worktree_py.is_file(), f"infrastructure/worktree.py not found at {worktree_py}"

    def test_infrastructure_process_port_exists(self) -> None:
        """infrastructure/process_port.py must exist (canonical ProcessPort home)."""
        pp_py = SRC / "infrastructure" / "process_port.py"
        assert pp_py.is_file(), f"infrastructure/process_port.py not found at {pp_py}"

    def test_infrastructure_messaging_exists(self) -> None:
        """infrastructure/messaging.py must exist (canonical Mailbox home)."""
        msg_py = SRC / "infrastructure" / "messaging.py"
        assert msg_py.is_file(), f"infrastructure/messaging.py not found at {msg_py}"

    def test_worktree_manager_canonical_import(self) -> None:
        """WorktreeManager is importable from both canonical and shim paths."""
        from tmux_orchestrator.infrastructure.worktree import WorktreeManager as W1
        from tmux_orchestrator.worktree import WorktreeManager as W2
        assert W1 is W2, "shim re-export must be the same class as the canonical one"

    def test_process_port_canonical_import(self) -> None:
        """ProcessPort is importable from both canonical and shim paths."""
        from tmux_orchestrator.infrastructure.process_port import ProcessPort as P1
        from tmux_orchestrator.process_port import ProcessPort as P2
        assert P1 is P2, "shim re-export must be the same class as the canonical one"

    def test_stdio_process_adapter_canonical_import(self) -> None:
        """StdioProcessAdapter is importable from both canonical and shim paths."""
        from tmux_orchestrator.infrastructure.process_port import StdioProcessAdapter as A1
        from tmux_orchestrator.process_port import StdioProcessAdapter as A2
        assert A1 is A2, "shim re-export must be the same class as the canonical one"

    def test_mailbox_canonical_import(self) -> None:
        """Mailbox is importable from both canonical and shim paths."""
        from tmux_orchestrator.infrastructure.messaging import Mailbox as M1
        from tmux_orchestrator.messaging import Mailbox as M2
        assert M1 is M2, "shim re-export must be the same class as the canonical one"
