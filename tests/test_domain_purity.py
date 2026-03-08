"""Tests that domain/ contains only pure stdlib types.

Verifies:
1. Each domain module imports only Python stdlib modules (no third-party libs).
2. Shim re-exports from old locations still work.
3. The domain package itself re-exports all public types.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

DOMAIN_DIR = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "domain"
DOMAIN_FILES = ["agent.py", "task.py", "message.py"]

# sys.stdlib_module_names is available from Python 3.10+
STDLIB_NAMES: frozenset[str] = frozenset(sys.stdlib_module_names)

# These are always allowed (internal package imports within domain/)
INTERNAL_PREFIX = "tmux_orchestrator.domain"


def _extract_top_level_imports(filepath: Path) -> list[str]:
    """Parse a Python file and return the top-level module names imported."""
    tree = ast.parse(filepath.read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module.split(".")[0])
    return modules


# ------------------------------------------------------------------
# Purity tests: domain/ must import only stdlib
# ------------------------------------------------------------------


@pytest.mark.parametrize("filename", DOMAIN_FILES)
def test_domain_module_imports_only_stdlib(filename: str) -> None:
    """Each domain/*.py file must import only Python stdlib modules."""
    filepath = DOMAIN_DIR / filename
    assert filepath.exists(), f"Domain file missing: {filepath}"

    imported_top_levels = _extract_top_level_imports(filepath)
    non_stdlib = [
        name
        for name in imported_top_levels
        if name not in STDLIB_NAMES
        and name != "__future__"
        and not name.startswith("tmux_orchestrator")
    ]
    assert non_stdlib == [], (
        f"{filename} imports non-stdlib modules: {non_stdlib}. "
        "domain/ must be free of third-party dependencies."
    )


# ------------------------------------------------------------------
# Shim tests: old import paths must still work
# ------------------------------------------------------------------


def test_agent_status_shim() -> None:
    """AgentStatus can be imported from agents.base (backward compat)."""
    from tmux_orchestrator.agents.base import AgentStatus

    assert AgentStatus.IDLE == "IDLE"
    assert AgentStatus.BUSY == "BUSY"
    assert AgentStatus.ERROR == "ERROR"
    assert AgentStatus.STOPPED == "STOPPED"
    assert AgentStatus.DRAINING == "DRAINING"


def test_agent_role_shim() -> None:
    """AgentRole can be imported from config (backward compat)."""
    from tmux_orchestrator.config import AgentRole

    assert AgentRole.WORKER == "worker"
    assert AgentRole.DIRECTOR == "director"


def test_task_shim() -> None:
    """Task can be imported from agents.base (backward compat)."""
    from tmux_orchestrator.agents.base import Task

    t = Task(id="x", prompt="hello")
    assert t.id == "x"
    assert t.prompt == "hello"
    assert t.priority == 0


def test_message_type_shim() -> None:
    """MessageType can be imported from bus (backward compat)."""
    from tmux_orchestrator.bus import MessageType

    assert MessageType.TASK == "TASK"
    assert MessageType.RESULT == "RESULT"
    assert MessageType.PEER_MSG == "PEER_MSG"


def test_message_shim() -> None:
    """Message can be imported from bus (backward compat)."""
    from tmux_orchestrator.bus import Message, MessageType

    msg = Message(type=MessageType.STATUS)
    assert msg.type == MessageType.STATUS
    d = msg.to_dict()
    assert d["type"] == "STATUS"


def test_broadcast_shim() -> None:
    """BROADCAST sentinel can be imported from bus (backward compat)."""
    from tmux_orchestrator.bus import BROADCAST

    assert BROADCAST == "*"


# ------------------------------------------------------------------
# Domain package re-export tests
# ------------------------------------------------------------------


def test_domain_package_exports_agent_status() -> None:
    from tmux_orchestrator.domain import AgentStatus

    assert AgentStatus.IDLE == "IDLE"


def test_domain_package_exports_agent_role() -> None:
    from tmux_orchestrator.domain import AgentRole

    assert AgentRole.WORKER == "worker"


def test_domain_package_exports_task() -> None:
    from tmux_orchestrator.domain import Task

    t = Task(id="t1", prompt="p")
    assert t.id == "t1"


def test_domain_package_exports_message_type() -> None:
    from tmux_orchestrator.domain import MessageType

    assert MessageType.CONTROL == "CONTROL"


def test_domain_package_exports_message() -> None:
    from tmux_orchestrator.domain import Message, MessageType

    msg = Message(type=MessageType.TASK)
    assert msg.type == MessageType.TASK


def test_domain_package_exports_broadcast() -> None:
    from tmux_orchestrator.domain import BROADCAST

    assert BROADCAST == "*"


# ------------------------------------------------------------------
# Identity tests: shim and domain types must be the SAME object
# ------------------------------------------------------------------


def test_agent_status_is_same_class() -> None:
    """The shim at agents.base and domain.agent must be the same class."""
    from tmux_orchestrator.agents.base import AgentStatus as ShimStatus
    from tmux_orchestrator.domain.agent import AgentStatus as DomainStatus

    assert ShimStatus is DomainStatus


def test_agent_role_is_same_class() -> None:
    from tmux_orchestrator.config import AgentRole as ShimRole
    from tmux_orchestrator.domain.agent import AgentRole as DomainRole

    assert ShimRole is DomainRole


def test_task_is_same_class() -> None:
    from tmux_orchestrator.agents.base import Task as ShimTask
    from tmux_orchestrator.domain.task import Task as DomainTask

    assert ShimTask is DomainTask


def test_message_type_is_same_class() -> None:
    from tmux_orchestrator.bus import MessageType as ShimMT
    from tmux_orchestrator.domain.message import MessageType as DomainMT

    assert ShimMT is DomainMT


def test_message_is_same_class() -> None:
    from tmux_orchestrator.bus import Message as ShimMsg
    from tmux_orchestrator.domain.message import Message as DomainMsg

    assert ShimMsg is DomainMsg
