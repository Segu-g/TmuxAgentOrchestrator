"""Agent group manager — named pools for targeted task dispatch.

Implements the concept of named agent groups (logical pools) that tasks can
target by group name instead of individual agent IDs or tags.

Design references:
- Kubernetes Node Pools / Node Groups: logical grouping of nodes for workload targeting.
  https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/
- AWS Auto Scaling Groups: groups of EC2 instances with shared configuration.
  https://docs.aws.amazon.com/autoscaling/ec2/userguide/auto-scaling-groups.html
- Apache Mesos Roles: partition cluster resources into named pools.
  https://mesos.apache.org/documentation/latest/roles/
- HashiCorp Nomad Task Groups: logical grouping of tasks on the same node.
  https://developer.hashicorp.com/nomad/docs/job-specification/group

DESIGN.md §10.26 (v0.31.0)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class GroupManager:
    """In-memory registry of named agent groups.

    A group is a named set of agent IDs.  Agents can belong to multiple groups.
    Tasks may declare a ``target_group`` to restrict dispatch to group members.

    Operations are O(1) to O(n) where n = number of agents or groups; the
    expected cardinalities are small (tens of groups, hundreds of agents).
    """

    def __init__(self) -> None:
        # name → set of agent_ids
        self._groups: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Group lifecycle
    # ------------------------------------------------------------------

    def create(self, name: str, agent_ids: list[str] | None = None) -> bool:
        """Create a group with the given *name* and optional initial *agent_ids*.

        Returns ``True`` if the group was created; ``False`` if it already exists.
        """
        if name in self._groups:
            return False
        self._groups[name] = set(agent_ids or [])
        logger.info("GroupManager: created group %r with agents %s", name, agent_ids)
        return True

    def delete(self, name: str) -> bool:
        """Delete a group by *name*.

        Returns ``True`` if the group existed and was removed; ``False`` if it
        was not found.
        """
        if name not in self._groups:
            return False
        del self._groups[name]
        logger.info("GroupManager: deleted group %r", name)
        return True

    def get(self, name: str) -> set[str] | None:
        """Return the set of agent IDs in *name*, or ``None`` if not found."""
        members = self._groups.get(name)
        if members is None:
            return None
        return set(members)  # return a copy

    def list_all(self) -> list[dict]:
        """Return a JSON-serialisable list of ``{name, agent_ids}`` dicts."""
        return [
            {"name": name, "agent_ids": sorted(members)}
            for name, members in sorted(self._groups.items())
        ]

    # ------------------------------------------------------------------
    # Membership management
    # ------------------------------------------------------------------

    def add_agent(self, name: str, agent_id: str) -> bool:
        """Add *agent_id* to the group *name*.

        Returns ``True`` if added; ``False`` if the group does not exist.
        """
        if name not in self._groups:
            return False
        self._groups[name].add(agent_id)
        logger.debug("GroupManager: added agent %r to group %r", agent_id, name)
        return True

    def remove_agent(self, name: str, agent_id: str) -> bool:
        """Remove *agent_id* from the group *name*.

        Returns ``True`` if removed (agent was a member); ``False`` if the
        group does not exist or the agent was not a member.
        """
        if name not in self._groups:
            return False
        if agent_id not in self._groups[name]:
            return False
        self._groups[name].discard(agent_id)
        logger.debug("GroupManager: removed agent %r from group %r", agent_id, name)
        return True

    def get_agent_groups(self, agent_id: str) -> list[str]:
        """Return a sorted list of group names that *agent_id* belongs to."""
        return sorted(
            name for name, members in self._groups.items() if agent_id in members
        )

    def __contains__(self, name: str) -> bool:
        return name in self._groups

    def __len__(self) -> int:
        return len(self._groups)
