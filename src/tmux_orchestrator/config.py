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
    # --- Capability tags for smart dispatch ---
    # Tasks with required_tags are only dispatched to agents whose tags include
    # ALL required tags.  Reference: FIPA Directory Facilitator (2002),
    # Kubernetes Node Affinity (nodeSelector pattern).
    tags: list[str] = field(default_factory=list)
    # --- Worktree lifecycle ---
    # When True and isolate=True, the orchestrator squash-merges the agent's
    # worktree branch into the main repo HEAD before teardown.  Commits made
    # by the agent inside its worktree therefore land on the original branch
    # automatically.  Set to False (default) to delete commits on stop.
    merge_on_stop: bool = False
    merge_target: str | None = None  # target branch for merge_on_stop; None = merge into current HEAD


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
    # --- Rate limiting (token bucket) ---
    # rate_limit_rps: token refill rate in tasks per second; 0 = unlimited (default)
    # rate_limit_burst: bucket capacity; defaults to 2× rps or 1 when rps == 0
    # Reference: Tanenbaum "Computer Networks" 5th ed. §5.3; DESIGN.md §10.16 (v0.20.0)
    rate_limit_rps: float = 0.0
    rate_limit_burst: int = 0
    # --- Context window monitoring ---
    # context_window_tokens: total context window size (default 200 000 for Claude Sonnet/Opus).
    # context_warn_threshold: fraction (0-1) at which context_warning is published (default 0.75).
    # context_auto_summarize: when True, /summarize is injected into agent pane at threshold.
    # context_monitor_poll: poll interval in seconds (default 5.0).
    # Reference: Liu et al. "Lost in the Middle" TACL 2024 https://arxiv.org/abs/2307.03172
    # Reference: Anthropic context windows docs https://platform.claude.com/docs/en/build-with-claude/context-windows
    context_window_tokens: int = 200_000
    context_warn_threshold: float = 0.75
    context_auto_summarize: bool = False
    context_monitor_poll: float = 5.0
    # --- Queue-depth autoscaling ---
    # autoscale_min: minimum number of autoscaled agents (0 = scale to zero).
    # autoscale_max: maximum number of autoscaled agents (0 = disabled).
    # autoscale_threshold: queue depth per idle agent before scaling up.
    # autoscale_cooldown: seconds of queue-empty before scaling down.
    # autoscale_poll: seconds between scale checks.
    # autoscale_agent_tags: capability tags assigned to auto-created agents.
    # autoscale_system_prompt: system prompt for auto-created agents.
    # References:
    #   Kubernetes HPA https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/
    #   Thijssen "Autonomic Computing" (MIT Press, 2009) — MAPE-K loop
    #   AWS Auto Scaling cooldowns https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-cooldowns.html
    autoscale_min: int = 0
    autoscale_max: int = 0          # 0 = autoscaling disabled
    autoscale_threshold: int = 3
    autoscale_cooldown: float = 30.0
    autoscale_poll: float = 5.0
    autoscale_agent_tags: list[str] = field(default_factory=list)
    autoscale_system_prompt: str | None = None
    # --- Task result persistence (Event Sourcing / CQRS pattern) ---
    # result_store_enabled: when True, every RESULT message is appended to a
    #   JSONL file on disk.  Disabled by default to avoid unexpected I/O.
    # result_store_dir: directory where JSONL files are written.
    #   Layout: {result_store_dir}/{session_name}/{YYYY-MM-DD}.jsonl
    # References:
    #   Martin Fowler "Event Sourcing" (2005)
    #   Greg Young "CQRS Documents" (2010)
    #   Rich Hickey "The Value of Values" (Datomic, 2012)
    result_store_enabled: bool = False
    result_store_dir: str = "~/.tmux_orchestrator/results"
    # --- Webhook notifications ---
    # webhook_timeout: HTTP timeout (seconds) per delivery attempt.
    # Reference: GitHub Webhooks; Stripe Webhooks; RFC 2104 HMAC;
    # Zalando RESTful API Guidelines §webhook; Shopify webhook verification.
    # DESIGN.md §10.25 (v0.30.0)
    webhook_timeout: float = 5.0


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
            tags=a.get("tags", []),
            merge_on_stop=a.get("merge_on_stop", False),
            merge_target=a.get("merge_target"),
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
        rate_limit_rps=data.get("rate_limit_rps", 0.0),
        rate_limit_burst=data.get("rate_limit_burst", 0),
        context_window_tokens=data.get("context_window_tokens", 200_000),
        context_warn_threshold=data.get("context_warn_threshold", 0.75),
        context_auto_summarize=data.get("context_auto_summarize", False),
        context_monitor_poll=data.get("context_monitor_poll", 5.0),
        autoscale_min=data.get("autoscale_min", 0),
        autoscale_max=data.get("autoscale_max", 0),
        autoscale_threshold=data.get("autoscale_threshold", 3),
        autoscale_cooldown=data.get("autoscale_cooldown", 30.0),
        autoscale_poll=data.get("autoscale_poll", 5.0),
        autoscale_agent_tags=data.get("autoscale_agent_tags", []),
        autoscale_system_prompt=data.get("autoscale_system_prompt"),
        result_store_enabled=data.get("result_store_enabled", False),
        result_store_dir=data.get("result_store_dir", "~/.tmux_orchestrator/results"),
        webhook_timeout=data.get("webhook_timeout", 5.0),
    )
