"""Groups APIRouter — /groups/* endpoints.

Named agent pools for targeted task dispatch.

Design reference:
- Kubernetes Node Pools / Node Groups
- AWS Auto Scaling Groups
- DESIGN.md §10.26 (v0.31.0)
- DESIGN.md §10.42 (v1.1.6)
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from tmux_orchestrator.web.schemas import GroupAddAgent, GroupCreate


def build_groups_router(
    orchestrator: Any,
    auth: Callable,
) -> APIRouter:
    """Build and return the groups APIRouter."""
    router = APIRouter()

    @router.post(
        "/groups",
        summary="Create a named agent group",
        dependencies=[Depends(auth)],
    )
    async def create_group(body: GroupCreate) -> dict:
        """Create a new named agent group (logical pool).
    
        Tasks may target this group via ``target_group`` in POST /tasks,
        POST /tasks/batch, or POST /workflows.
    
        Returns 409 Conflict if a group with the same name already exists.
    
        Design references:
        - Kubernetes Node Pools / Node Groups — logical grouping of cluster nodes.
        - AWS Auto Scaling Groups — named pools of homogeneous EC2 instances.
        - Apache Mesos Roles — cluster resource partitioning by name.
        - HashiCorp Nomad Task Groups — co-located task scheduling units.
        - DESIGN.md §10.26 (v0.31.0)
        """
        gm = orchestrator.get_group_manager()
        created = gm.create(body.name, body.agent_ids)
        if not created:
            raise HTTPException(
                status_code=409,
                detail=f"Group {body.name!r} already exists",
            )
        return {"name": body.name, "agent_ids": body.agent_ids}
    
    @router.get(
        "/groups",
        summary="List all agent groups",
        dependencies=[Depends(auth)],
    )
    async def list_groups() -> list:
        """Return all named agent groups with member agent IDs and their statuses.
    
        Each entry contains:
        - ``name``: group name
        - ``agent_ids``: sorted list of member agent IDs
        - ``agents``: list of ``{id, status}`` dicts for each member
    
        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        all_agents = {a["id"]: a for a in orchestrator.list_agents()}
        result = []
        for entry in gm.list_all():
            agents_detail = [
                {"id": aid, "status": all_agents[aid]["status"]}
                if aid in all_agents
                else {"id": aid, "status": "unknown"}
                for aid in entry["agent_ids"]
            ]
            result.append({
                "name": entry["name"],
                "agent_ids": entry["agent_ids"],
                "agents": agents_detail,
            })
        return result
    
    @router.get(
        "/groups/{group_name}",
        summary="Get a specific agent group",
        dependencies=[Depends(auth)],
    )
    async def get_group(group_name: str) -> dict:
        """Return details for *group_name*: member agent IDs and their statuses.
    
        Returns 404 if the group is unknown.
    
        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        members = gm.get(group_name)
        if members is None:
            raise HTTPException(
                status_code=404,
                detail=f"Group {group_name!r} not found",
            )
        all_agents = {a["id"]: a for a in orchestrator.list_agents()}
        agents_detail = [
            {"id": aid, "status": all_agents[aid]["status"]}
            if aid in all_agents
            else {"id": aid, "status": "unknown"}
            for aid in sorted(members)
        ]
        return {
            "name": group_name,
            "agent_ids": sorted(members),
            "agents": agents_detail,
        }
    
    @router.delete(
        "/groups/{group_name}",
        summary="Delete an agent group",
        dependencies=[Depends(auth)],
    )
    async def delete_group(group_name: str) -> dict:
        """Remove a named agent group.
    
        Returns 404 if the group is unknown.  Does not affect the agents
        themselves — only the group registration is removed.
    
        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        deleted = gm.delete(group_name)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Group {group_name!r} not found",
            )
        return {"deleted": True, "name": group_name}
    
    @router.post(
        "/groups/{group_name}/agents",
        summary="Add an agent to a group",
        dependencies=[Depends(auth)],
    )
    async def add_agent_to_group(group_name: str, body: GroupAddAgent) -> dict:
        """Add *agent_id* to the named group.
    
        Returns 404 if the group does not exist.  Adding an agent that is
        already a member is idempotent (no error).
    
        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        added = gm.add_agent(group_name, body.agent_id)
        if not added:
            raise HTTPException(
                status_code=404,
                detail=f"Group {group_name!r} not found",
            )
        return {"name": group_name, "agent_id": body.agent_id, "added": True}
    
    @router.delete(
        "/groups/{group_name}/agents/{agent_id}",
        summary="Remove an agent from a group",
        dependencies=[Depends(auth)],
    )
    async def remove_agent_from_group(group_name: str, agent_id: str) -> dict:
        """Remove *agent_id* from the named group.
    
        Returns 404 if the group does not exist or the agent is not a member.
    
        Design reference: DESIGN.md §10.26 (v0.31.0).
        """
        gm = orchestrator.get_group_manager()
        removed = gm.remove_agent(group_name, agent_id)
        if not removed:
            # Distinguish between group-not-found and agent-not-member
            if gm.get(group_name) is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Group {group_name!r} not found",
                )
            raise HTTPException(
                status_code=404,
                detail=f"Agent {agent_id!r} is not a member of group {group_name!r}",
            )
        return {"name": group_name, "agent_id": agent_id, "removed": True}

    return router
