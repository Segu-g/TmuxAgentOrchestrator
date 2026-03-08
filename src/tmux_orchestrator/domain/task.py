"""Pure domain type for task representation.

This module has ZERO external dependencies — only Python stdlib is imported.
It is the authoritative definition of the Task dataclass.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Task:
    id: str
    prompt: str
    priority: int = 0  # lower = higher priority
    metadata: dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: secrets.token_hex(8))
    depends_on: list[str] = field(default_factory=list)  # task IDs that must complete first
    # When set, the RESULT for this task is delivered directly to this agent's
    # mailbox in addition to being broadcast on the bus.  Implements the
    # request-reply pattern for hierarchical parent→child result routing.
    # Reference: "Learning Notes #15 – Request Reply Pattern | RabbitMQ" (2024)
    reply_to: str | None = None  # agent_id that should receive the RESULT in its mailbox
    # When set, the task is ONLY dispatched to this specific agent.
    # The dispatch loop skips other idle agents and waits until the named
    # agent becomes idle.  Unknown target_agent IDs are dead-lettered.
    # Reference: Hohpe & Woolf "Enterprise Integration Patterns" (2003) — Message Router.
    target_agent: str | None = None
    # Capability tags: ALL listed tags must be present in the target agent's
    # ``tags`` list.  Empty list = no constraint (any idle worker matches).
    # Reference: FIPA Directory Facilitator (2002); Kubernetes nodeSelector.
    required_tags: list[str] = field(default_factory=list)
    # Named agent group: when set, task is only dispatched to agents in this group.
    # Acts as an AND-filter with required_tags (both conditions must be satisfied).
    # Reference: Kubernetes Node Pools; AWS Auto Scaling Groups; DESIGN.md §10.26 (v0.31.0)
    target_group: str | None = None
    # Per-task retry semantics: how many times this task may be re-enqueued on
    # failure before it is dead-lettered.  ``retry_count`` is incremented each
    # time the orchestrator retries on an errored RESULT.
    # Reference: AWS SQS maxReceiveCount / Redrive policy; Netflix Hystrix retry;
    # Polly .NET resilience library; Erlang OTP supervisor restart strategies.
    # DESIGN.md §10.21 (v0.26.0)
    max_retries: int = 0
    retry_count: int = 0
    # Priority inheritance: when True and the task has depends_on, the effective
    # priority is min(own_priority, min(priority of all direct parents)).
    # Prevents high-priority sub-tasks from being blocked by lower-priority work
    # already in the queue.
    # Reference: Liu & Layland (1973) Priority Inheritance Protocol;
    # Apache Airflow priority_weight upstream/downstream rules;
    # DESIGN.md §10.27 (v0.32.0)
    inherit_priority: bool = True
    # TTL (Time-to-Live): when set, the task expires *ttl* seconds after
    # submission.  None = never expires.  *submitted_at* records wall-clock
    # submission time (time.time()) and is set automatically.  *expires_at* is
    # computed once at submit_task() time as submitted_at + ttl; both fields
    # are read-only after that point.
    # Reference: RabbitMQ TTL (https://www.rabbitmq.com/docs/ttl);
    # Azure Service Bus message expiration (Microsoft Docs 2024);
    # AWS SQS MessageRetentionPeriod; Dapr pubsub-message-ttl;
    # DESIGN.md §10.28 (v0.33.0)
    ttl: float | None = None
    submitted_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def __lt__(self, other: "Task") -> bool:
        return self.priority < other.priority

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation of this task."""
        d: dict = {
            "task_id": self.id,
            "prompt": self.prompt,
            "priority": self.priority,
            "trace_id": self.trace_id,
            "depends_on": self.depends_on,
            "reply_to": self.reply_to,
            "target_agent": self.target_agent,
            "required_tags": self.required_tags,
            "target_group": self.target_group,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "inherit_priority": self.inherit_priority,
            "submitted_at": self.submitted_at,
            "ttl": self.ttl,
            "expires_at": self.expires_at,
            **({"metadata": self.metadata} if self.metadata else {}),
        }
        return d
