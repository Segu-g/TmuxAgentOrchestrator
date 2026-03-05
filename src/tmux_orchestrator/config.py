"""YAML config loader and dataclasses for TmuxAgentOrchestrator."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal


class AgentRole(str, Enum):
    """Role assigned to an agent in the orchestrator hierarchy."""

    WORKER = "worker"
    DIRECTOR = "director"


@dataclass
class AgentConfig:
    id: str
    type: Literal["claude_code"]
    isolate: bool = True        # False → share main repo working tree
    role: AgentRole = AgentRole.WORKER
    task_timeout: int | None = None   # overrides OrchestratorConfig.task_timeout when set
    command: str | None = None  # custom command (defaults to claude CLI)
    # --- Context engineering fields ---
    system_prompt: str | None = None  # injected into agent's CLAUDE.md at startup
    context_files: list[str] = field(default_factory=list)  # paths (relative to cwd) to pre-load


@dataclass
class OrchestratorConfig:
    session_name: str = "orchestrator"
    agents: list[AgentConfig] = field(default_factory=list)
    # Each entry is a pair [agent_id_a, agent_id_b] — bidirectional permission
    p2p_permissions: list[tuple[str, str]] = field(default_factory=list)
    task_timeout: int = 120
    mailbox_dir: str = "~/.tmux_orchestrator"
    web_base_url: str = "http://localhost:8000"
    circuit_breaker_threshold: int = 3
    circuit_breaker_recovery: float = 60.0
    dlq_max_retries: int = 50  # re-queue attempts before dead-lettering a task
    task_queue_maxsize: int = 0  # 0 = unbounded; >0 = bounded (submit_task raises when full)
    watchdog_poll: float = 10.0  # seconds between watchdog checks (lower in tests)
    # --- ERROR state auto-recovery ---
    recovery_attempts: int = 3   # max restart attempts per agent before giving up
    recovery_backoff_base: float = 5.0  # seconds; attempt N waits backoff_base^N seconds
    recovery_poll: float = 2.0   # seconds between recovery checks


def load_config(path: str | Path) -> OrchestratorConfig:
    """Load and validate an orchestrator config from a YAML file."""
    data = yaml.safe_load(Path(path).read_text())

    agents = [
        AgentConfig(
            id=a["id"],
            type=a["type"],
            isolate=a.get("isolate", True),
            role=AgentRole(a.get("role", "worker")),
            task_timeout=a.get("task_timeout"),
            command=a.get("command"),
            system_prompt=a.get("system_prompt"),
            context_files=a.get("context_files", []),
        )
        for a in data.get("agents", [])
    ]

    p2p = [tuple(pair) for pair in data.get("p2p_permissions", [])]

    return OrchestratorConfig(
        session_name=data.get("session_name", "orchestrator"),
        agents=agents,
        p2p_permissions=p2p,  # type: ignore[arg-type]
        task_timeout=data.get("task_timeout", 120),
        mailbox_dir=data.get("mailbox_dir", "~/.tmux_orchestrator"),
        web_base_url=data.get("web_base_url", "http://localhost:8000"),
        circuit_breaker_threshold=data.get("circuit_breaker_threshold", 3),
        circuit_breaker_recovery=data.get("circuit_breaker_recovery", 60.0),
        dlq_max_retries=data.get("dlq_max_retries", 50),
        recovery_attempts=data.get("recovery_attempts", 3),
        recovery_backoff_base=data.get("recovery_backoff_base", 5.0),
        recovery_poll=data.get("recovery_poll", 2.0),
    )
