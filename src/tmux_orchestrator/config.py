"""YAML config loader and dataclasses for TmuxAgentOrchestrator."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class AgentConfig:
    id: str
    type: Literal["claude_code"]
    isolate: bool = True  # False → share main repo working tree
    role: str = "worker"  # "worker" | "director"


@dataclass
class OrchestratorConfig:
    session_name: str = "orchestrator"
    agents: list[AgentConfig] = field(default_factory=list)
    # Each entry is a pair [agent_id_a, agent_id_b] — bidirectional permission
    p2p_permissions: list[tuple[str, str]] = field(default_factory=list)
    task_timeout: int = 120
    mailbox_dir: str = "~/.tmux_orchestrator"
    web_base_url: str = "http://localhost:8000"


def load_config(path: str | Path) -> OrchestratorConfig:
    """Load and validate an orchestrator config from a YAML file."""
    data = yaml.safe_load(Path(path).read_text())

    agents = [
        AgentConfig(
            id=a["id"],
            type=a["type"],
            isolate=a.get("isolate", True),
            role=a.get("role", "worker"),
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
    )
