"""Tests that application/ layer has no direct infrastructure imports.

Verifies:
1. Each application/ module does NOT import from infrastructure packages
   (tmux_interface, libtmux, subprocess, fastapi, httpx, etc.).
2. Each application/ module MAY import from:
   - Python stdlib
   - tmux_orchestrator.domain (pure domain types)
   - tmux_orchestrator.application (sibling modules)
3. Backward-compat shims (supervision.py, workflow.py) still re-export
   from application/ so old imports continue to work.
4. Newly moved classes can be imported from the application/ package.

Layer rule (Clean Architecture — Martin, 2017):
  domain/ ← application/ ← infrastructure/

Reference:
    - Martin "Clean Architecture" (2017) — The Dependency Rule
    - PEP 544 — Protocols
    - DESIGN.md §10.N (v1.0.15 — application/ layer extraction)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "application"

# All application/ Python files (excluding __pycache__)
APP_FILES = [p.name for p in APP_DIR.glob("*.py") if not p.name.startswith("_")]

# sys.stdlib_module_names is available from Python 3.10+
STDLIB_NAMES: frozenset[str] = frozenset(sys.stdlib_module_names)

# Modules that MUST NOT appear as direct imports in application/ code
FORBIDDEN_MODULES = frozenset([
    "tmux_interface",
    "libtmux",
    "subprocess",
    "fastapi",
    "httpx",
    "uvicorn",
    "textual",
    "pyyaml",
    "yaml",
    "typer",
])

# Internal prefixes that are ALLOWED in application/ imports
ALLOWED_INTERNAL_PREFIXES = (
    "tmux_orchestrator.domain",
    "tmux_orchestrator.application",
)


def _extract_imports(filepath: Path) -> list[str]:
    """Parse a Python file and return the top-level module names imported.

    Handles both ``import foo`` and ``from foo import bar`` forms.
    Returns the first dotted component (e.g. "tmux_orchestrator") plus the
    full dotted prefix for internal imports so we can check sub-packages.
    """
    tree = ast.parse(filepath.read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (level > 0)
            if node.level == 0 and node.module:
                modules.append(node.module)
    return modules


def _is_allowed(module: str) -> bool:
    """Return True if *module* is allowed inside application/."""
    # stdlib
    top = module.split(".")[0]
    if top in STDLIB_NAMES or top == "__future__":
        return True
    # tmux_orchestrator.domain and tmux_orchestrator.application siblings
    for prefix in ALLOWED_INTERNAL_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


# ---------------------------------------------------------------------------
# Purity tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", APP_FILES)
def test_application_module_has_no_infrastructure_imports(filename: str) -> None:
    """Each application/*.py must not import from forbidden infrastructure modules."""
    filepath = APP_DIR / filename
    assert filepath.exists(), f"Application file missing: {filepath}"

    imported = _extract_imports(filepath)

    # Check for forbidden modules
    violations = [
        mod
        for mod in imported
        if mod.split(".")[0] in FORBIDDEN_MODULES
    ]
    assert violations == [], (
        f"{filename} imports forbidden infrastructure modules: {violations}. "
        "application/ must not depend on infrastructure."
    )


@pytest.mark.parametrize("filename", APP_FILES)
def test_application_module_imports_are_valid(filename: str) -> None:
    """Each application/*.py may only import stdlib or domain/application sub-packages."""
    filepath = APP_DIR / filename
    assert filepath.exists(), f"Application file missing: {filepath}"

    imported = _extract_imports(filepath)

    # Filter to only tmux_orchestrator.* imports (ignore stdlib)
    internal = [mod for mod in imported if "tmux_orchestrator" in mod]

    violations = [mod for mod in internal if not _is_allowed(mod)]
    assert violations == [], (
        f"{filename} imports from disallowed tmux_orchestrator sub-packages: {violations}. "
        "application/ may only import from domain/ and application/ siblings."
    )


# ---------------------------------------------------------------------------
# Shim backward-compatibility tests
# ---------------------------------------------------------------------------


def test_supervised_task_shim_import() -> None:
    """supervised_task can still be imported from the old path (supervision.py)."""
    from tmux_orchestrator.supervision import supervised_task

    assert callable(supervised_task)


def test_workflow_shim_import() -> None:
    """Workflow, WorkflowStep, _topological_sort still importable from workflow.py."""
    from tmux_orchestrator.workflow import Workflow, WorkflowStep, _topological_sort

    assert callable(_topological_sort)
    assert WorkflowStep is not None
    assert Workflow is not None


def test_orchestrator_still_exports_protocols() -> None:
    """orchestrator.py still exports ContextMonitorProtocol etc. (backward compat)."""
    from tmux_orchestrator.orchestrator import (
        ContextMonitorProtocol,
        DriftMonitorProtocol,
        NullContextMonitor,
        NullDriftMonitor,
    )

    null_ctx = NullContextMonitor()
    assert isinstance(null_ctx, ContextMonitorProtocol)
    null_drift = NullDriftMonitor()
    assert isinstance(null_drift, DriftMonitorProtocol)


# ---------------------------------------------------------------------------
# application/ package re-export tests
# ---------------------------------------------------------------------------


def test_application_package_exports_supervised_task() -> None:
    from tmux_orchestrator.application import supervised_task

    assert callable(supervised_task)


def test_application_package_exports_workflow() -> None:
    from tmux_orchestrator.application import Workflow, WorkflowStep

    assert WorkflowStep is not None
    assert Workflow is not None


def test_application_package_exports_monitor_protocols() -> None:
    from tmux_orchestrator.application import (
        ContextMonitorProtocol,
        DriftMonitorProtocol,
        NullContextMonitor,
        NullDriftMonitor,
    )

    null_ctx = NullContextMonitor()
    assert isinstance(null_ctx, ContextMonitorProtocol)
    null_drift = NullDriftMonitor()
    assert isinstance(null_drift, DriftMonitorProtocol)


def test_application_package_exports_task_submitter() -> None:
    from tmux_orchestrator.application import TaskSubmitter
    from typing import runtime_checkable, Protocol

    assert TaskSubmitter is not None


def test_application_package_exports_infra_protocols() -> None:
    """application/__init__ re-exports infra protocols and Null Objects (v1.0.35)."""
    from tmux_orchestrator.application import (
        AutoScalerProtocol,
        CheckpointStoreProtocol,
        NullAutoScaler,
        NullCheckpointStore,
        NullResultStore,
        ResultStoreProtocol,
    )

    assert isinstance(NullResultStore(), ResultStoreProtocol)
    assert isinstance(NullCheckpointStore(), CheckpointStoreProtocol)
    assert isinstance(NullAutoScaler(), AutoScalerProtocol)


def test_orchestrator_exports_infra_protocols() -> None:
    """orchestrator.py re-exports infra protocols for backward compat (v1.0.35)."""
    from tmux_orchestrator.orchestrator import (
        AutoScalerProtocol,
        CheckpointStoreProtocol,
        NullAutoScaler,
        NullCheckpointStore,
        NullResultStore,
        ResultStoreProtocol,
    )

    assert isinstance(NullResultStore(), ResultStoreProtocol)
    assert isinstance(NullCheckpointStore(), CheckpointStoreProtocol)
    assert isinstance(NullAutoScaler(), AutoScalerProtocol)


# ---------------------------------------------------------------------------
# Identity tests: shim and application types must be the SAME object
# ---------------------------------------------------------------------------


def test_supervised_task_is_same_function() -> None:
    """supervision.py shim and application.supervision must be the same function."""
    from tmux_orchestrator.supervision import supervised_task as shim_fn
    from tmux_orchestrator.application.supervision import supervised_task as app_fn

    assert shim_fn is app_fn


def test_workflow_is_same_class() -> None:
    """workflow.py shim and application.workflow_service must be the same class."""
    from tmux_orchestrator.workflow import Workflow as ShimWf
    from tmux_orchestrator.application.workflow_service import Workflow as AppWf

    assert ShimWf is AppWf


def test_workflow_step_is_same_class() -> None:
    from tmux_orchestrator.workflow import WorkflowStep as ShimStep
    from tmux_orchestrator.application.workflow_service import WorkflowStep as AppStep

    assert ShimStep is AppStep


def test_null_context_monitor_is_same_class() -> None:
    from tmux_orchestrator.orchestrator import NullContextMonitor as OrchestratorNull
    from tmux_orchestrator.application.monitor_protocols import NullContextMonitor as AppNull

    assert OrchestratorNull is AppNull


def test_null_drift_monitor_is_same_class() -> None:
    from tmux_orchestrator.orchestrator import NullDriftMonitor as OrchestratorNull
    from tmux_orchestrator.application.monitor_protocols import NullDriftMonitor as AppNull

    assert OrchestratorNull is AppNull


# ---------------------------------------------------------------------------
# Phase 2 — bus.py, circuit_breaker.py, registry.py, workflow_manager.py
# ---------------------------------------------------------------------------

# --- Shim backward-compatibility ---


def test_bus_shim_import() -> None:
    """Bus can still be imported from the old root path."""
    from tmux_orchestrator.bus import Bus

    bus = Bus()
    assert hasattr(bus, "publish")
    assert hasattr(bus, "subscribe")


def test_circuit_breaker_shim_import() -> None:
    """CircuitBreaker and BreakerState can still be imported from root circuit_breaker."""
    from tmux_orchestrator.circuit_breaker import BreakerState, CircuitBreaker

    cb = CircuitBreaker("agent-x")
    assert cb.state == BreakerState.CLOSED
    assert cb.is_allowed() is True


def test_registry_shim_import() -> None:
    """AgentRegistry can still be imported from root registry."""
    from tmux_orchestrator.registry import AgentRegistry

    reg = AgentRegistry(p2p_permissions=[])
    assert reg.find_idle_worker() is None


def test_workflow_manager_shim_import() -> None:
    """WorkflowManager and validate_dag can still be imported from root workflow_manager."""
    from tmux_orchestrator.workflow_manager import WorkflowManager, validate_dag

    wm = WorkflowManager()
    assert wm.list_all() == []
    assert callable(validate_dag)


# --- Identity (shim → application canonical class) ---


def test_bus_is_same_class() -> None:
    """bus.Bus shim and application.bus.Bus must be the same class."""
    from tmux_orchestrator.bus import Bus as ShimBus
    from tmux_orchestrator.application.bus import Bus as AppBus

    assert ShimBus is AppBus


def test_circuit_breaker_is_same_class() -> None:
    from tmux_orchestrator.circuit_breaker import CircuitBreaker as ShimCB
    from tmux_orchestrator.application.circuit_breaker import CircuitBreaker as AppCB

    assert ShimCB is AppCB


def test_breaker_state_is_same_class() -> None:
    from tmux_orchestrator.circuit_breaker import BreakerState as ShimBS
    from tmux_orchestrator.application.circuit_breaker import BreakerState as AppBS

    assert ShimBS is AppBS


def test_agent_registry_is_same_class() -> None:
    from tmux_orchestrator.registry import AgentRegistry as ShimReg
    from tmux_orchestrator.application.registry import AgentRegistry as AppReg

    assert ShimReg is AppReg


def test_workflow_manager_is_same_class() -> None:
    from tmux_orchestrator.workflow_manager import WorkflowManager as ShimWM
    from tmux_orchestrator.application.workflow_manager import WorkflowManager as AppWM

    assert ShimWM is AppWM


def test_validate_dag_is_same_function() -> None:
    from tmux_orchestrator.workflow_manager import validate_dag as shim_fn
    from tmux_orchestrator.application.workflow_manager import validate_dag as app_fn

    assert shim_fn is app_fn


# --- application/__init__ package re-exports ---


def test_application_package_exports_bus() -> None:
    from tmux_orchestrator.application import Bus

    bus = Bus()
    assert hasattr(bus, "publish")


def test_application_package_exports_circuit_breaker() -> None:
    from tmux_orchestrator.application import BreakerState, CircuitBreaker

    cb = CircuitBreaker("test-agent")
    assert cb.state == BreakerState.CLOSED


def test_application_package_exports_agent_registry() -> None:
    from tmux_orchestrator.application import AgentRegistry

    reg = AgentRegistry(p2p_permissions=[])
    assert reg.list_all() == []


def test_application_package_exports_workflow_manager() -> None:
    from tmux_orchestrator.application import WorkflowManager, validate_dag

    wm = WorkflowManager()
    assert wm.list_all() == []
    assert callable(validate_dag)


def test_application_package_exports_workflow_run() -> None:
    from tmux_orchestrator.application import WorkflowRun

    run = WorkflowRun.create("wf", ["t1"])
    assert run.name == "wf"
