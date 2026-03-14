"""Workflows APIRouter — /workflows/* endpoints.

Design reference: DESIGN.md §10.42 (v1.1.6)
FastAPI "Bigger Applications": https://fastapi.tiangolo.com/tutorial/bigger-applications/
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from tmux_orchestrator.web.schemas import (
    AdrWorkflowSubmit,
    AgentmeshWorkflowSubmit,
    CleanArchWorkflowSubmit,
    CodeAuditWorkflowSubmit,
    CompetitionWorkflowSubmit,
    DDDWorkflowSubmit,
    DebateWorkflowSubmit,
    DelphiWorkflowSubmit,
    FulldevWorkflowSubmit,
    IterativeReviewWorkflowSubmit,
    LoopBlockModel,
    MobReviewWorkflowSubmit,
    MutationTestWorkflowSubmit,
    PairCoderWorkflowSubmit,
    PairWorkflowSubmit,
    ParallelBlockModel,
    PdcaWorkflowSubmit,
    PeerReviewWorkflowSubmit,
    RedBlueWorkflowSubmit,
    RefactorWorkflowSubmit,
    SequenceBlockModel,
    SkipConditionModel,
    SpecFirstTddWorkflowSubmit,
    SpecFirstWorkflowSubmit,
    SocraticWorkflowSubmit,
    TddWorkflowSubmit,
    WorkflowFromTemplateSubmit,
    WorkflowSubmit,
)


def build_workflows_router(
    orchestrator: Any,
    auth: Callable,
    scratchpad: dict | None = None,
    templates_dir: Any = None,
) -> APIRouter:
    """Build and return the workflows APIRouter.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    auth:
        Authentication dependency callable (combined session + API key).
    scratchpad:
        Shared scratchpad store (for loop-until condition evaluation).
    templates_dir:
        ``pathlib.Path`` pointing to the workflow templates directory
        (e.g. ``examples/workflows/``).  When ``None``, the loader falls
        back to the package-relative default
        (``<repo_root>/examples/workflows/``).
        Design reference: DESIGN.md §10.103 (v1.2.28)
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    # Resolve templates_dir: prefer the caller-supplied value; otherwise
    # fall back to the package-relative ``examples/workflows/`` directory.
    if templates_dir is not None:
        _templates_dir: _Path = _Path(templates_dir)
    else:
        # Walk up from this file: routers/ → web/ → tmux_orchestrator/ → src/ → repo/
        _this_file = _Path(__file__).resolve()
        _repo_root = _this_file.parents[4]  # TmuxAgentOrchestrator/
        _templates_dir = _repo_root / "examples" / "workflows"

    router = APIRouter()

    @router.post(
        "/workflows",
        summary="Submit a multi-step workflow DAG",
        dependencies=[Depends(auth)],
    )
    async def submit_workflow(body: WorkflowSubmit) -> dict:
        """Submit a named workflow as a directed acyclic graph of tasks.
    
        Supports two submission modes:
    
        **tasks= (legacy DAG mode)**:
        Each task in ``tasks`` may reference other tasks in the same submission
        via ``depends_on`` (a list of ``local_id`` strings).
    
        **phases= (declarative phase mode)**:
        A list of :class:`PhaseSpecModel` objects where each phase has a
        ``pattern`` (single | parallel | competitive | debate) and an
        ``agents`` selector.  The server expands phases into a task DAG
        automatically.  Sequential phases are chained via ``depends_on``;
        parallel/competitive phases fan out; debate phases build an
        advocate/critic/judge chain.
    
        In both modes the handler:
        1. Validates the DAG for unknown ``local_id`` references and cycles.
        2. Assigns a global orchestrator task ID to each local node.
        3. Submits tasks to the orchestrator in topological order, translating
           ``depends_on`` local IDs to global task IDs.
        4. Registers all task IDs with the ``WorkflowManager`` for status
           tracking.
        5. Returns the workflow ID and a ``local_id → global_task_id`` mapping.
    
        Returns 400 on invalid DAG (unknown dependency or cycle).
        Returns 422 on schema validation failure (neither tasks nor phases provided).
    
        Design references:
        - Apache Airflow DAG model — directed acyclic graph of tasks
        - AWS Step Functions — state machine workflow definition
        - Tomasulo's algorithm — register renaming == local_id → task_id mapping
        - Prefect "Modern Data Stack" — submit pipeline as a unit
        - arXiv:2512.19769 (PayPal DSL 2025): declarative pattern → 60% dev-time reduction
        - §12「ワークフロー設計の層構造」層1 宣言的モード
        - DESIGN.md §10.20 (v0.25.0), §10.15 (v0.48.0)
        """
        from tmux_orchestrator.workflow_manager import validate_dag  # noqa: PLC0415
    
        # ------------------------------------------------------------------
        # Phase expansion path (new declarative mode)
        # ------------------------------------------------------------------
        phase_statuses = None
        if body.phases is not None:
            from tmux_orchestrator.domain.phase_strategy import (  # noqa: PLC0415
                CompetitiveConfig,
                DebateConfig,
                LoopBlock,
                LoopSpec,
                ParallelBlock,
                ParallelConfig,
                SequenceBlock,
                SingleConfig,
            )
            from tmux_orchestrator.phase_executor import (  # noqa: PLC0415
                AgentSelector,
                PhaseSpec,
                SkipCondition,
                expand_phase_items_with_status,
                expand_phases_with_status,
            )

            def _to_domain_skip_condition(m: Any) -> Any:
                """Convert a SkipConditionModel → domain SkipCondition dataclass."""
                if m is None:
                    return None
                return SkipCondition(
                    key=m.key,
                    value=m.value,
                    negate=m.negate,
                )

            def _to_domain_strategy_config(m: Any) -> Any:
                """Convert a StrategyConfigModel → domain StrategyConfig dataclass."""
                if m is None:
                    return None
                t = getattr(m, "type", None)
                if t == "single":
                    return SingleConfig()
                if t == "parallel":
                    return ParallelConfig(merge_strategy=m.merge_strategy)
                if t == "competitive":
                    return CompetitiveConfig(
                        scorer=m.scorer,
                        top_k=m.top_k,
                        timeout_per_agent=m.timeout_per_agent,
                        judge_prompt_template=getattr(m, "judge_prompt_template", ""),
                    )
                if t == "debate":
                    return DebateConfig(
                        rounds=m.rounds,
                        require_consensus=m.require_consensus,
                        judge_criteria=m.judge_criteria,
                        early_stop_signal=getattr(m, "early_stop_signal", ""),
                    )
                return None

            def _to_domain_phase_spec(p: Any) -> PhaseSpec:
                """Convert a PhaseSpecModel → domain PhaseSpec dataclass."""
                return PhaseSpec(
                    name=p.name,
                    pattern=p.pattern,  # type: ignore[arg-type]
                    agents=AgentSelector(
                        tags=p.agents.tags,
                        count=p.agents.count,
                        target_agent=p.agents.target_agent,
                        target_group=p.agents.target_group,
                    ),
                    critic_agents=AgentSelector(
                        tags=p.critic_agents.tags,
                        count=p.critic_agents.count,
                        target_agent=p.critic_agents.target_agent,
                        target_group=p.critic_agents.target_group,
                    ),
                    judge_agents=AgentSelector(
                        tags=p.judge_agents.tags,
                        count=p.judge_agents.count,
                        target_agent=p.judge_agents.target_agent,
                        target_group=p.judge_agents.target_group,
                    ),
                    debate_rounds=p.debate_rounds,
                    context=p.context,
                    required_tags=p.required_tags,
                    timeout=p.timeout,
                    strategy_config=_to_domain_strategy_config(p.strategy_config),
                    skip_condition=_to_domain_skip_condition(
                        getattr(p, "skip_condition", None)
                    ),
                    agent_template=getattr(p, "agent_template", None),
                    chain_branch=getattr(p, "chain_branch", False),
                )

            def _to_domain_phase_item(item: Any) -> Any:
                """Convert a PhaseItemModel → domain PhaseSpec, LoopBlock, SequenceBlock, or ParallelBlock."""
                # Detect LoopBlockModel by presence of 'loop' attribute.
                if isinstance(item, LoopBlockModel):
                    loop_model = item.loop
                    until = _to_domain_skip_condition(loop_model.until)
                    inner_phases = [_to_domain_phase_item(p) for p in item.phases]
                    return LoopBlock(
                        name=item.name,
                        loop=LoopSpec(max=loop_model.max, until=until),
                        phases=inner_phases,
                    )
                # Detect SequenceBlockModel by presence of 'sequence' structure.
                if isinstance(item, SequenceBlockModel):
                    inner_phases = [_to_domain_phase_item(p) for p in item.phases]
                    return SequenceBlock(name=item.name, phases=inner_phases)
                # Detect ParallelBlockModel by presence of 'parallel' structure.
                if isinstance(item, ParallelBlockModel):
                    inner_phases = [_to_domain_phase_item(p) for p in item.phases]
                    return ParallelBlock(name=item.name, phases=inner_phases)
                # Otherwise treat as PhaseSpecModel.
                # If it arrived as a dict (e.g. from list[Any]), parse it first.
                if isinstance(item, dict):
                    if "loop" in item:
                        from tmux_orchestrator.web.schemas import LoopBlockModel as LBM  # noqa: PLC0415
                        return _to_domain_phase_item(LBM.model_validate(item))
                    if "sequence" in item:
                        from tmux_orchestrator.web.schemas import SequenceBlockModel as SQM  # noqa: PLC0415
                        return _to_domain_phase_item(SQM.model_validate(item["sequence"] if isinstance(item.get("sequence"), dict) else item))
                    if "parallel" in item:
                        from tmux_orchestrator.web.schemas import ParallelBlockModel as PBM  # noqa: PLC0415
                        return _to_domain_phase_item(PBM.model_validate(item["parallel"] if isinstance(item.get("parallel"), dict) else item))
                    from tmux_orchestrator.web.schemas import PhaseSpecModel as PSM  # noqa: PLC0415
                    return _to_domain_phase_spec(PSM.model_validate(item))
                return _to_domain_phase_spec(item)

            run_id_prefix = uuid.uuid4().hex[:8]
            phase_sp = f"wf/{run_id_prefix}"

            # Apply phase_defaults (if any) to each phase before conversion.
            # effective_phases() returns phases with phase_defaults merged in;
            # phase-level values always take priority (DESIGN.md §10.98 v1.2.23).
            effective_phases = body.effective_phases()

            # Check whether any item is a block type (LoopBlock, SequenceBlock, ParallelBlock).
            # Any block type triggers the block-aware expander path.
            has_loop = any(
                isinstance(p, (LoopBlockModel, SequenceBlockModel, ParallelBlockModel))
                or (isinstance(p, dict) and ("loop" in p or "sequence" in p or "parallel" in p))
                for p in effective_phases
            )

            # Convert all phase items to domain objects (handles both dicts and
            # Pydantic models, plus LoopBlockModel detection).
            try:
                domain_items = [_to_domain_phase_item(p) for p in effective_phases]
            except Exception as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            if has_loop:
                # Use the loop-aware expander.
                task_specs, phase_statuses, _loop_terminals = expand_phase_items_with_status(
                    domain_items,
                    context=body.context,
                    scratchpad_prefix=phase_sp,
                    scratchpad=scratchpad,
                )

                # Resolve depends_on references to block names (loop/sequence/parallel)
                # in phase tasks.  When a PhaseSpec (outer) declares
                # depends_on: [block_name], that local_id will not exist in
                # task_specs — we need to wire it to the block's terminal task IDs.
                block_names = set(_loop_terminals.keys())
                if block_names:
                    for spec in task_specs:
                        new_deps = []
                        for dep in spec.get("depends_on", []):
                            if dep in block_names:
                                new_deps.extend(_loop_terminals[dep])
                            else:
                                new_deps.append(dep)
                        spec["depends_on"] = new_deps
            else:
                # Fast path: no loop blocks — use original expand_phases_with_status.
                phase_specs: list[PhaseSpec] = domain_items  # type: ignore[assignment]
                task_specs, phase_statuses = expand_phases_with_status(
                    phase_specs,
                    context=body.context,
                    scratchpad_prefix=phase_sp,
                    scratchpad=scratchpad,
                )
        else:
            # Legacy tasks= path
            task_specs = [t.model_dump() for t in body.tasks]  # type: ignore[union-attr]
    
        # ------------------------------------------------------------------
        # DAG validation + submission (shared by both paths)
        # ------------------------------------------------------------------
        try:
            ordered = validate_dag(task_specs, local_id_key="local_id", deps_key="depends_on")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    
        # Assign global IDs and submit in dependency order
        local_to_global: dict[str, str] = {}
        global_task_ids: list[str] = []
        # Branch-chain handoff (v1.2.5): tracks local_id → ephemeral_agent_id
        # for agents spawned immediately (non-chain-branch phases).  Chain-branch
        # phases defer spawning to dispatch time (see _route_loop) so that the
        # predecessor's worktree branch already has the committed files when the
        # successor's worktree is created.
        # Design reference: DESIGN.md §10.81
        local_id_to_ephemeral: dict[str, str] = {}

        for spec in ordered:
            global_deps = [local_to_global[lid] for lid in spec.get("depends_on", [])]
            # Dynamic ephemeral agent spawning: when a task spec carries an
            # ``agent_template`` key (set by phase strategies when
            # ``PhaseSpec.agent_template`` is non-None), spawn a fresh ephemeral
            # agent from that template and route the task directly to it.
            # Design reference: DESIGN.md §10.79 (v1.2.3)
            effective_target_agent = spec.get("target_agent")
            agent_template = spec.get("agent_template")
            task_metadata: dict = {}

            if agent_template:
                if spec.get("chain_branch"):
                    # Deferred spawning for chain_branch phases (v1.2.5):
                    # Store the template ID and predecessor global task IDs in
                    # task metadata.  The orchestrator's _route_loop spawns the
                    # ephemeral agent at dispatch time (after depends_on tasks
                    # complete), ensuring create_from_branch sees the predecessor's
                    # committed files.  We store the predecessor global task IDs
                    # so _route_loop can look up which ephemeral agent ran them
                    # via Orchestrator._task_ephemeral_agent.
                    # Design reference: DESIGN.md §10.81
                    pred_global_task_ids = [
                        local_to_global[dep_lid]
                        for dep_lid in spec.get("depends_on", [])
                        if dep_lid in local_to_global
                    ]
                    task_metadata["_ephemeral_template"] = agent_template
                    task_metadata["_chain_branch"] = True
                    task_metadata["_chain_pred_task_ids"] = pred_global_task_ids
                    # No immediate spawn; target_agent stays None.
                    # _route_loop will spawn and set target_agent at dispatch time.
                    # workflow_id is provided to spawn_ephemeral_agent via
                    # Orchestrator._task_workflow_id (populated below after run.id).
                    # Design reference: DESIGN.md §10.84 (v1.2.8 — branch tracking)
                    effective_target_agent = None
                else:
                    # Immediate spawn for non-chain-branch ephemeral phases (v1.2.3).
                    try:
                        effective_target_agent = await orchestrator.spawn_ephemeral_agent(
                            agent_template
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=str(exc))
                    local_id_to_ephemeral[spec["local_id"]] = effective_target_agent
            task = await orchestrator.submit_task(
                spec["prompt"],
                priority=spec.get("priority", 0),
                metadata=task_metadata or None,
                depends_on=global_deps or None,
                target_agent=effective_target_agent,
                required_tags=spec.get("required_tags") or None,
                target_group=spec.get("target_group"),
                max_retries=spec.get("max_retries", 0),
                inherit_priority=spec.get("inherit_priority", True),
                ttl=spec.get("ttl"),
                timeout=spec.get("timeout"),
            )
            local_to_global[spec["local_id"]] = task.id
            global_task_ids.append(task.id)
    
        # Register with WorkflowManager for status tracking.
        # Compute dag_edges from the ordered task specs: for each spec, emit one
        # (from, to) edge per entry in depends_on.  This stores the topology
        # persistently on WorkflowRun for GET /workflows/{id}/dag (v1.2.14).
        wm = orchestrator.get_workflow_manager()
        dag_edges: list[tuple[str, str]] = [
            (local_to_global[dep_lid], local_to_global[spec["local_id"]])
            for spec in ordered
            for dep_lid in spec.get("depends_on", [])
            if dep_lid in local_to_global and spec["local_id"] in local_to_global
        ]
        run = wm.submit(name=body.name, task_ids=global_task_ids, dag_edges=dag_edges)

        # Workflow branch cleanup wiring (v1.2.8):
        # Now that run.id is known, register branch tracking for immediately-spawned
        # ephemeral agents (local_id_to_ephemeral) and configure the cleanup callback.
        _eph_branches = getattr(orchestrator, "_ephemeral_agent_branches", {})
        _wf_branches = getattr(orchestrator, "_workflow_branches", None)
        if local_id_to_ephemeral and _wf_branches is not None:
            # Register branches for immediately-spawned ephemeral agents.
            for eph_id in local_id_to_ephemeral.values():
                branch = _eph_branches.get(eph_id, "")
                if branch:
                    _wf_branches.setdefault(run.id, []).append(branch)
        # Register workflow_id → task_id mapping for ALL deferred chain_branch tasks
        # (tasks that have _ephemeral_template in metadata).  This allows _route_loop
        # to pass workflow_id to spawn_ephemeral_agent() regardless of whether the task
        # is in _waiting_tasks or already in the priority queue.
        # Design reference: DESIGN.md §10.84 (v1.2.8)
        _task_wf_id = getattr(orchestrator, "_task_workflow_id", None)
        if _task_wf_id is not None:
            for global_tid in global_task_ids:
                # We only need to track chain_branch tasks, but recording all
                # workflow tasks is safe (spawn_ephemeral_agent will only use it
                # when called for ephemeral agents with isolate=True).
                _task_wf_id[global_tid] = run.id
        # Wire the branch cleanup callback to WorkflowManager (if cleanup enabled).
        _config = getattr(orchestrator, "config", None)
        _branch_cleanup_enabled = getattr(_config, "workflow_branch_cleanup", True) if _config is not None else False
        _cleanup_fn = getattr(orchestrator, "cleanup_workflow_branches", None)
        if _branch_cleanup_enabled and _cleanup_fn is not None:
            merge_final = getattr(body, "merge_to_main_on_complete", False)
            async def _branch_cleanup(wf_id: str, _mf: bool = merge_final) -> None:
                await orchestrator.cleanup_workflow_branches(wf_id, merge_final_to_main=_mf)
            wm.set_branch_cleanup_fn(_branch_cleanup)

        # Attach phase status trackers to the workflow run (if phases were used)
        if phase_statuses is not None:
            # Remap local_id → global_task_id for each phase's task_ids
            for ps in phase_statuses:
                ps.task_ids = [local_to_global[lid] for lid in ps.task_ids]
            run.phases = phase_statuses
            # Register phase-task mappings for phase completion tracking (v1.1.38).
            # Must be called AFTER run.phases is assigned so register_phases() can
            # iterate over the phases with their remapped global task_ids.
            wm.register_phases(run.id)
            # Register loop until runtime evaluation (v1.2.7).
            # For each LoopBlock with an until condition, build per-iteration
            # task ID lists from the local_to_global mapping and register them
            # with the WorkflowManager for server-side condition evaluation.
            if has_loop:
                from tmux_orchestrator.domain.phase_strategy import LoopBlock as _LB  # noqa: PLC0415

                def _register_loop_items(items: list, wf_run_id: str, sp: str) -> None:
                    """Recursively register LoopBlocks with until conditions."""
                    for dom_item in items:
                        if not isinstance(dom_item, _LB):
                            continue
                        loop_b = dom_item
                        if loop_b.loop.until is None:
                            continue
                        # Build per-iteration global task ID lists by matching
                        # the naming pattern used by expand_loop_iter:
                        # local_id = f"{loop_name}_i{iter_num}_{original_local_id}"
                        iterations_global: list[list[str]] = []
                        for iter_num in range(1, loop_b.loop.max + 1):
                            prefix = f"{loop_b.name}_i{iter_num}_"
                            iter_tids = [
                                global_tid
                                for local_id, global_tid in local_to_global.items()
                                if local_id.startswith(prefix)
                            ]
                            if iter_tids:
                                iterations_global.append(iter_tids)
                        if iterations_global:
                            wm.register_loop(
                                wf_run_id,
                                loop_b.name,
                                loop_b.loop,
                                iterations_global,
                                sp,
                            )

                _register_loop_items(domain_items, run.id, phase_sp)

        response: dict = {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": local_to_global,
        }
        if phase_statuses is not None:
            response["phases"] = [ps.to_dict() for ps in phase_statuses]
        return response
    
    @router.post(
        "/workflows/tdd",
        summary="Submit a 3-agent TDD workflow (Red→Green→Refactor)",
        dependencies=[Depends(auth)],
    )
    async def submit_tdd_workflow(body: TddWorkflowSubmit) -> dict:
        """Submit a context-isolated 3-agent TDD workflow DAG.
    
        Automatically builds and submits a 3-step Workflow DAG:
    
        1. **test-writer** (RED): writes failing pytest tests for *feature*
           and stores the test file path in the scratchpad.
        2. **implementer** (GREEN): reads the test file path from scratchpad,
           writes the minimal implementation that makes tests pass, and stores
           the implementation file path.
        3. **refactorer** (REFACTOR): reads both paths from scratchpad, verifies
           tests still pass, and improves code quality.
    
        The scratchpad acts as a Blackboard — agents communicate via shared
        state without direct P2P messaging.  This enforces context isolation:
        each agent starts with exactly the information it needs.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``tdd/{feature}``)
        - ``task_ids``: dict with keys ``test_writer``, ``implementer``,
          ``refactorer`` mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``tdd/{workflow_id[:8]}``)
    
        Design references:
        - TDFlow arXiv:2510.23761 (2025): context isolation via sub-agents
          achieves 88.8% on SWE-Bench Lite.
        - alexop.dev "Forcing Claude Code to TDD" (2025): genuine TDD
          requires separate context windows for each phase.
        - Blackboard pattern (Buschmann 1996): shared working memory.
        - DESIGN.md §10.31 (v0.36.0)
        """

    
        lang = body.language
        feature_slug = body.feature.replace(" ", "_")
    
        # Register workflow first to get a stable run ID for the scratchpad prefix.
        # We submit tasks in a second pass with the prefix baked into prompts.
        wm = orchestrator.get_workflow_manager()
        wf_name = f"tdd/{body.feature}"
    
        # Pre-generate a workflow run UUID so we can derive the scratchpad prefix
        # before submitting tasks.  We call wm.submit() after task creation with
        # the actual task IDs.
        # NOTE: scratchpad keys must NOT contain '/' since the REST route uses
        # a plain {key} path parameter, not {key:path}.  Underscores are used
        # as the namespace separator instead.
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"tdd_{pre_run_id[:8]}"
        tests_path_key = f"{scratchpad_prefix}_tests_path"
        impl_path_key = f"{scratchpad_prefix}_impl_path"
    
        # --- Scratchpad helper snippet (shared across all 3 prompts) ---
        # Agents read web_base_url + api_key from __orchestrator_context__.json
        # and __orchestrator_api_key__ (see CLAUDE.md "API Key for Authenticated Requests").
        # Scratchpad keys use underscores (NOT slashes) because the REST route
        # /scratchpad/{key} treats '/' as a URL path separator.
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        # --- Prompt templates (context isolation) ---
        # test-writer gets: feature name + language + where to store artifacts
        # It must NOT know how the feature will be implemented.
        test_writer_prompt = (
            f"You are the TEST-WRITER agent in a Red→Green→Refactor TDD workflow.\n"
            f"\n"
            f"**Feature to test:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (RED phase):\n"
            f"1. Write failing {lang} pytest tests for '{body.feature}'.\n"
            f"   - Focus on behaviour, not implementation.\n"
            f"   - Tests must fail when run against an empty/stub implementation.\n"
            f"   - Use descriptive test function names (they are the specification).\n"
            f"2. Save the test file as `test_{feature_slug}.py` in your working directory.\n"
            f"3. Verify the tests fail: run `python -m pytest test_{feature_slug}.py -v` and confirm failures.\n"
            f"4. Write the ABSOLUTE PATH to the test file to the shared scratchpad.\n"
            f"   Scratchpad key: `{tests_path_key}`\n"
            f"   Get your web_base_url and api_key from `__orchestrator_context__.json` and `__orchestrator_api_key__`.\n"
            f"   Then run:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{tests_path_key}\" \\\n"
            f"     -H 'Content-Type: application/json' \\\n"
            f"     -d '{{\"value\": \"'$(pwd)/test_{feature_slug}.py'\"}}'\n"
            f"   ```\n"
            f"\n"
            f"Do NOT write any implementation code. Focus only on tests."
        )
    
        # implementer gets: feature name + language + where to READ test path
        # It must NOT see the test-writer's rationale — only the test file.
        implementer_prompt = (
            f"You are the IMPLEMENTER agent in a Red→Green→Refactor TDD workflow.\n"
            f"\n"
            f"**Feature to implement:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (GREEN phase):\n"
            f"1. Get the test file path from the shared scratchpad.\n"
            f"   Scratchpad key: `{tests_path_key}`\n"
            f"   Get your web_base_url and api_key from `__orchestrator_context__.json` and `__orchestrator_api_key__`.\n"
            f"   Then run:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   TEST_FILE=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{tests_path_key}\" \\\n"
            f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            f"   echo \"Test file: $TEST_FILE\"\n"
            f"   ```\n"
            f"2. Read the test file to understand what needs to be implemented.\n"
            f"3. Write the MINIMAL {lang} implementation of '{body.feature}' that makes all tests pass.\n"
            f"   - Write only enough code to pass the tests — no over-engineering.\n"
            f"4. Verify: run `python -m pytest $TEST_FILE -v` and confirm all tests pass.\n"
            f"5. Write the ABSOLUTE PATH to your implementation file to the scratchpad.\n"
            f"   Scratchpad key: `{impl_path_key}`\n"
            f"   ```bash\n"
            f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{impl_path_key}\" \\\n"
            f"     -H 'Content-Type: application/json' \\\n"
            f"     -d '{{\"value\": \"<absolute_path_to_impl_file>\"}}'\n"
            f"   ```\n"
            f"\n"
            f"Implement '{body.feature}' to make the tests pass."
        )
    
        # refactorer gets: feature name + where to READ both paths
        refactorer_prompt = (
            f"You are the REFACTORER agent in a Red→Green→Refactor TDD workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (REFACTOR phase):\n"
            f"1. Get both artifact paths from the shared scratchpad.\n"
            f"   Get your web_base_url and api_key from `__orchestrator_context__.json` and `__orchestrator_api_key__`.\n"
            f"   Then run:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   TEST_FILE=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{tests_path_key}\" \\\n"
            f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            f"   IMPL_FILE=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
            f"     \"$WEB_BASE_URL/scratchpad/{impl_path_key}\" \\\n"
            f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            f"   echo \"Test: $TEST_FILE  Impl: $IMPL_FILE\"\n"
            f"   ```\n"
            f"2. Verify tests still pass: `python -m pytest $TEST_FILE -v`\n"
            f"3. Refactor and improve the implementation:\n"
            f"   - Remove duplication, improve naming, add docstrings.\n"
            f"   - Do NOT change behaviour — all tests must remain green.\n"
            f"4. Run tests again to confirm: `python -m pytest $TEST_FILE -v`\n"
            f"5. Write a brief summary of improvements to stdout.\n"
            f"\n"
            f"Refactor '{body.feature}' to improve code quality while keeping tests green."
        )
    
        # --- Submit the 3-step DAG ---
        tw_task = await orchestrator.submit_task(
            test_writer_prompt,
            required_tags=body.test_writer_tags or None,
        )
        impl_task = await orchestrator.submit_task(
            implementer_prompt,
            required_tags=body.implementer_tags or None,
            depends_on=[tw_task.id],
        )
        refactorer_task = await orchestrator.submit_task(
            refactorer_prompt,
            required_tags=body.refactorer_tags or None,
            depends_on=[impl_task.id],
            reply_to=body.reply_to,
        )
    
        # Register with WorkflowManager
        run = wm.submit(
            name=wf_name,
            task_ids=[tw_task.id, impl_task.id, refactorer_task.id],
        )
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": {
                "test_writer": tw_task.id,
                "implementer": impl_task.id,
                "refactorer": refactorer_task.id,
            },
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/debate",
        summary="Submit a multi-round Advocate/Critic/Judge debate workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_debate_workflow(body: DebateWorkflowSubmit) -> dict:
        """Submit a structured multi-agent debate Workflow DAG.
    
        Builds and submits a ``max_rounds``-round debate DAG:
    
        For each round n in 1..max_rounds:
          1. **advocate_r{n}**: presents or refines the affirmative argument for
             *topic*, reading the previous critic's rebuttal from scratchpad
             (round > 1).  Stores argument to scratchpad.
          2. **critic_r{n}**: challenges the advocate's argument using a
             Devil's Advocate persona, reading from scratchpad.  Stores rebuttal.
    
        Final step:
          - **judge**: reads all rounds from scratchpad, synthesizes the debate,
            and writes ``DECISION.md`` content to ``{scratchpad_prefix}_decision``.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``debate/{topic}``)
        - ``task_ids``: dict mapping role keys to global task IDs.
          Keys: ``advocate_r1``, ``critic_r1``, ..., ``advocate_rN``,
          ``critic_rN``, ``judge``
        - ``scratchpad_prefix``: scratchpad namespace for this run
    
        Design references:
        - Du et al. ICML 2024 (arXiv:2305.14325): 2-3 rounds is optimal.
        - DEBATE ACL 2024 (arXiv:2405.09935): Devil's Advocate prevents bias.
        - ChatEval ICLR 2024 (arXiv:2308.07201): role diversity is critical.
        - DESIGN.md §10.32 (v0.37.0)
        """

    
        wm = orchestrator.get_workflow_manager()
        wf_name = f"debate/{body.topic}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"debate_{pre_run_id[:8]}"
    
        # Scratchpad helper snippet (shared across all prompts)
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _scratchpad_key(suffix: str) -> str:
            """Derive a flat scratchpad key (no slashes)."""
            return f"{scratchpad_prefix}_{suffix}"
    
        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            """Bash snippet that writes $var to the scratchpad key."""
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )
    
        def _read_snippet(key: str, varname: str) -> str:
            """Bash snippet that reads scratchpad key into $varname."""
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )
    
        all_tasks: list = []  # list of (role_key, task_coroutine_args)
        prev_task_id: str | None = None
        task_ids_map: dict[str, str] = {}
    
        # Build tasks for each round
        for rn in range(1, body.max_rounds + 1):
            adv_key = _scratchpad_key(f"r{rn}_advocate")
            crit_key = _scratchpad_key(f"r{rn}_critic")
    
            # --- Advocate prompt ---
            if rn == 1:
                advocate_preamble = (
                    f"You are the ADVOCATE agent in a structured debate workflow.\n"
                    f"\n"
                    f"**Debate topic:** {body.topic}\n"
                    f"**Round:** {rn} of {body.max_rounds}\n"
                    f"\n"
                    f"Your task:\n"
                    f"1. Present a clear, well-structured argument IN FAVOUR of one "
                    f"position on '{body.topic}'.\n"
                    f"   - Choose the stronger/more practical position.\n"
                    f"   - Support your argument with concrete reasons, examples, "
                    f"and trade-offs.\n"
                    f"   - Be specific and technical where appropriate.\n"
                )
            else:
                prev_crit_key = _scratchpad_key(f"r{rn - 1}_critic")
                advocate_preamble = (
                    f"You are the ADVOCATE agent in a structured debate workflow.\n"
                    f"\n"
                    f"**Debate topic:** {body.topic}\n"
                    f"**Round:** {rn} of {body.max_rounds} (rebuttal round)\n"
                    f"\n"
                    f"Your task:\n"
                    f"1. Read the critic's previous rebuttal from the scratchpad:\n"
                    f"   ```bash\n"
                    f"   {_ctx_snippet}\n"
                    + _read_snippet(prev_crit_key, "CRITIC_ARG")
                    + f"   echo \"Critic said: $CRITIC_ARG\"\n"
                    f"   ```\n"
                    f"2. Respond to the critic's points — defend your original position,\n"
                    f"   concede weak points if warranted, and strengthen your argument.\n"
                    f"   Be specific and address each critique directly.\n"
                )
            advocate_prompt = (
                advocate_preamble
                + f"\n"
                f"3. Write your argument to a file `advocate_r{rn}.md` in your "
                f"working directory.\n"
                f"4. Store your argument in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                f"   CONTENT=$(cat advocate_r{rn}.md)\n"
                + _write_snippet(adv_key)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Write a structured, technical argument. Be concise (max 400 words)."
            )
    
            # --- Critic prompt ---
            critic_prompt = (
                f"You are the CRITIC agent (Devil's Advocate) in a structured "
                f"debate workflow.\n"
                f"\n"
                f"**Debate topic:** {body.topic}\n"
                f"**Round:** {rn} of {body.max_rounds}\n"
                f"\n"
                f"Your task:\n"
                f"1. Read the advocate's argument from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(adv_key, "ADVOCATE_ARG")
                + f"   echo \"Advocate said: $ADVOCATE_ARG\"\n"
                f"   ```\n"
                f"2. Challenge the advocate's argument rigorously.\n"
                f"   - Identify logical flaws, missing trade-offs, and counterexamples.\n"
                f"   - Present the strongest possible counter-argument.\n"
                f"   - Do NOT agree unless truly warranted — your role is to stress-test "
                f"the argument.\n"
                f"   - Be specific and technical.\n"
                f"3. Write your rebuttal to a file `critic_r{rn}.md` in your "
                f"working directory.\n"
                f"4. Store your rebuttal in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   CONTENT=$(cat critic_r{rn}.md)\n"
                + _write_snippet(crit_key)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Write a rigorous critique. Be concise (max 400 words)."
            )
    
            # Determine dependencies
            adv_depends = [prev_task_id] if prev_task_id else []
            adv_task = await orchestrator.submit_task(
                advocate_prompt,
                required_tags=body.advocate_tags or None,
                depends_on=adv_depends,
            )
            prev_task_id = adv_task.id
            task_ids_map[f"advocate_r{rn}"] = adv_task.id
    
            crit_task = await orchestrator.submit_task(
                critic_prompt,
                required_tags=body.critic_tags or None,
                depends_on=[adv_task.id],
            )
            prev_task_id = crit_task.id
            task_ids_map[f"critic_r{rn}"] = crit_task.id
    
        # --- Judge prompt ---
        # Collect all scratchpad keys for judge to read
        round_keys_desc = "\n".join(
            f"   - Round {rn} Advocate: key `{_scratchpad_key(f'r{rn}_advocate')}`\n"
            f"   - Round {rn} Critic:   key `{_scratchpad_key(f'r{rn}_critic')}`"
            for rn in range(1, body.max_rounds + 1)
        )
        decision_key = _scratchpad_key("decision")
    
        judge_prompt = (
            f"You are the JUDGE agent in a structured debate workflow.\n"
            f"\n"
            f"**Debate topic:** {body.topic}\n"
            f"**Rounds completed:** {body.max_rounds}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read all debate rounds from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   ```\n"
            f"   Keys to read:\n"
            f"{round_keys_desc}\n"
            f"   Use curl to read each key: "
            f"`curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
            f"\n"
            f"2. Write `DECISION.md` in your working directory containing:\n"
            f"   - **Topic**: {body.topic}\n"
            f"   - **Summary of advocate's position** (2-3 sentences)\n"
            f"   - **Summary of critic's challenges** (2-3 sentences)\n"
            f"   - **Decision**: which position is stronger and why\n"
            f"   - **Rationale**: key factors that determined the decision\n"
            f"   - **Caveats**: important trade-offs or conditions to consider\n"
            f"\n"
            f"3. Store the DECISION.md content in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat DECISION.md)\n"
            + _write_snippet(decision_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Write a balanced, well-reasoned DECISION.md. Be objective and cite "
            f"specific arguments from the debate."
        )
    
        judge_task = await orchestrator.submit_task(
            judge_prompt,
            required_tags=body.judge_tags or None,
            depends_on=[prev_task_id] if prev_task_id else [],
            reply_to=body.reply_to,
        )
        task_ids_map["judge"] = judge_task.id
    
        # Register all tasks with WorkflowManager
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/adr",
        summary="Submit a Proposer/Reviewer/Synthesizer ADR generation workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_adr_workflow(body: AdrWorkflowSubmit) -> dict:
        """Submit a 3-agent Architecture Decision Record (ADR) Workflow DAG.
    
        Pipeline:
          1. **proposer**: analyses the topic, lists candidate options with
             technical pros/cons, and stores the analysis in the scratchpad.
          2. **reviewer**: reads the proposal and produces a technical critique
             — identifies gaps, missing trade-offs, and biases.
          3. **synthesizer**: reads both proposal and review, then produces a
             final MADR-format ``DECISION.md`` and writes it to the scratchpad.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``adr/<topic>``)
        - ``task_ids``: dict with keys ``proposer``, ``reviewer``, ``synthesizer``
        - ``scratchpad_prefix``: scratchpad namespace for this run
    
        Design references:
        - AgenticAKM arXiv:2602.04445 (2026): multi-agent decomposition.
        - Ochoa et al. arXiv:2507.05981 "MAD for RE" (2025).
        - MADR 4.0.0 (2024-09-17): Markdown ADR standard.
        - DESIGN.md §10.14 (v0.40.0)
        """

    
        wm = orchestrator.get_workflow_manager()
        wf_name = f"adr/{body.topic}"

        pre_run_id = str(uuid.uuid4())
        # If caller supplied a custom prefix, use it as-is; otherwise auto-generate
        # a collision-safe prefix by appending the first 8 hex chars of a UUID.
        if body.scratchpad_prefix and body.scratchpad_prefix != "adr":
            scratchpad_prefix = body.scratchpad_prefix
        else:
            scratchpad_prefix = f"adr_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        # Scratchpad keys — v1.1.40: use _draft/_review/_final for clarity
        draft_key = _scratchpad_key("draft")
        review_key = _scratchpad_key("review")
        final_key = _scratchpad_key("final")

        # Build optional context and criteria sections for prompts
        context_section = (
            f"\n**Context:** {body.context}\n" if body.context else ""
        )
        criteria_section = (
            f"\n**Evaluation Criteria:** {', '.join(body.criteria)}\n"
            if body.criteria
            else ""
        )

        # --- Proposer prompt ---
        proposer_prompt = (
            f"You are the PROPOSER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            + context_section
            + criteria_section
            + f"\n"
            f"Your task is to draft a complete Architecture Decision Record (ADR) for this topic.\n"
            f"\n"
            f"Steps:\n"
            f"1. Identify 2-3 concrete options for addressing '{body.topic}'.\n"
            f"2. For each option, document:\n"
            f"   - Brief description\n"
            f"   - Pros (technical advantages, performance, maintainability, cost)\n"
            f"   - Cons (drawbacks, risks, operational complexity)\n"
            f"   - When this option is most appropriate\n"
            f"3. Write your full ADR draft to `draft.md` using the Nygard format:\n"
            f"   # ADR: {body.topic}\n"
            f"   ## Status\n"
            f"   Proposed\n"
            f"   ## Context\n"
            f"   ## Decision\n"
            f"   ## Consequences\n"
            f"4. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat draft.md)\n"
            + _write_snippet(draft_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be technical, specific, and objective. Document all options before deciding. "
            f"The reviewer will critique your draft next."
        )

        # --- Reviewer prompt ---
        reviewer_prompt = (
            f"You are the REVIEWER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            + context_section
            + criteria_section
            + f"\n"
            f"Your task is to critically review the proposer's ADR draft.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the proposer's ADR draft from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(draft_key, "DRAFT")
            + f"   echo \"Draft: $DRAFT\"\n"
            f"   ```\n"
            f"2. Critically evaluate the draft:\n"
            f"   - Are any options missing or underrepresented?\n"
            f"   - Are the pros/cons accurate and complete?\n"
            f"   - Are there hidden risks or biases in the analysis?\n"
            f"   - What additional decision drivers should be considered?\n"
            f"     (e.g. team expertise, operational burden, vendor lock-in, scalability)\n"
            f"   - Is the ADR format correct and complete?\n"
            f"3. Write your structured critique to `review.md` in your working directory.\n"
            f"   Include: gaps, risks, recommended additions, verdict on each option.\n"
            f"4. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat review.md)\n"
            + _write_snippet(review_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be rigorous and independent. Do not simply agree with the proposer — "
            f"your role is to stress-test the analysis."
        )

        # --- Synthesizer prompt ---
        synthesizer_prompt = (
            f"You are the SYNTHESIZER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            + context_section
            + criteria_section
            + f"\n"
            f"Your task is to read the ADR draft and the reviewer's critique, then produce "
            f"a final polished DECISION.md.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read both artifacts from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(draft_key, "DRAFT")
            + _read_snippet(review_key, "REVIEW")
            + f"   ```\n"
            f"2. Write `DECISION.md` in MADR format with these sections:\n"
            f"   ```markdown\n"
            f"   # ADR: {body.topic}\n"
            f"   Status: Accepted\n"
            f"   Date: $(date +%Y-%m-%d)\n"
            f"   ## Context and Problem Statement\n"
            f"   ## Decision Drivers\n"
            f"   ## Considered Options\n"
            f"   ## Decision Outcome\n"
            f"   ### Consequences\n"
            f"   ## Pros and Cons of the Options\n"
            f"   ```\n"
            f"3. Store DECISION.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat DECISION.md)\n"
            + _write_snippet(final_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Synthesize both the draft and the reviewer's critique. "
            f"Choose the best option with clear rationale. "
            f"Acknowledge the reviewer's concerns in the Consequences section."
        )

        # Submit tasks in pipeline order (propose → review → synthesize)
        proposer_task = await orchestrator.submit_task(
            proposer_prompt,
            required_tags=body.proposer_tags or None,
            depends_on=[],
            timeout=body.agent_timeout,
        )

        reviewer_task = await orchestrator.submit_task(
            reviewer_prompt,
            required_tags=body.reviewer_tags or None,
            depends_on=[proposer_task.id],
            timeout=body.agent_timeout,
        )

        synthesizer_task = await orchestrator.submit_task(
            synthesizer_prompt,
            required_tags=body.synthesizer_tags or None,
            depends_on=[reviewer_task.id],
            reply_to=body.reply_to,
            timeout=body.agent_timeout,
        )

        task_ids_map = {
            "proposer": proposer_task.id,
            "reviewer": reviewer_task.id,
            "synthesizer": synthesizer_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/delphi",
        summary="Submit a multi-round Delphi expert-consensus workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_delphi_workflow(body: DelphiWorkflowSubmit) -> dict:
        """Submit a Delphi-style multi-round expert consensus Workflow DAG.
    
        For each round *n* in 1..max_rounds:
    
        1. **expert_{persona}_r{n}** (parallel, one per persona): Each expert
           agent reads the previous moderator's feedback (if round > 1) from the
           scratchpad, then independently produces an opinion on ``topic`` from
           their persona's perspective.  Writes ``expert_{persona}_r{n}.md`` and
           stores it to the scratchpad.
        2. **moderator_r{n}**: Reads all expert opinions for round *n*, synthesises
           them into a structured summary with convergence/divergence analysis, and
           writes ``delphi_round_{n}.md``.  Stores the summary to scratchpad.
    
        Final step:
    
        - **consensus**: Reads all moderator summaries, identifies points of
          consensus and remaining disagreements, and writes ``consensus.md``
          containing a structured final agreement.
    
        Returns:
    
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``delphi/<topic>``)
        - ``task_ids``: dict mapping role keys to global task IDs.
          Keys: ``expert_{e}_r{n}`` for each expert/round, ``moderator_r{n}``,
          ``consensus``
        - ``scratchpad_prefix``: scratchpad namespace for this run
    
        Design references:
    
        - DelphiAgent (ScienceDirect 2025): multiple LLM agents emulate the
          Delphi method with iterative feedback and synthesis.
        - RT-AID (ScienceDirect 2025): AI-assisted opinions accelerate Delphi
          convergence even with limited expert samples.
        - Du et al. ICML 2024 (arXiv:2305.14325): multi-round debate converges
          to correct answer even when all agents are initially wrong.
        - CONSENSAGENT ACL 2025: sycophancy-mitigation in multi-agent consensus.
        - DESIGN.md §10.22 (v1.0.23)
        """

    
        wm = orchestrator.get_workflow_manager()
        wf_name = f"delphi/{body.topic}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"delphi_{pre_run_id[:8]}"
    
        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _scratchpad_key(suffix: str) -> str:
            """Derive a flat scratchpad key (no slashes)."""
            return f"{scratchpad_prefix}_{suffix}"
    
        def _write_snippet(key: str, filename: str) -> str:
            """Python3-based snippet to safely write a file's content to scratchpad.
    
            Uses python3 json.dumps for correct escaping of quotes, newlines, and
            special characters — avoiding the shell-quoting fragility of:
                -d '{"value": "'$CONTENT'"}'
            which breaks when file content contains quotes or newlines.
    
            The ``_ctx_snippet`` must be run in the same shell session beforehand
            to set $API_KEY and $WEB_BASE_URL.
            """
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os, sys\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\"  "
            )
    
        def _read_snippet(key: str, varname: str) -> str:
            """Bash snippet that reads scratchpad key into $varname."""
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )
    
        task_ids_map: dict[str, str] = {}
        all_task_ids: list[str] = []
    
        # prev_moderator_task_id: the previous round's moderator task (or None for round 1)
        prev_moderator_task_id: str | None = None
    
        for rn in range(1, body.max_rounds + 1):
            expert_task_ids: list[str] = []
    
            for persona in body.experts:
                expert_key = _scratchpad_key(f"r{rn}_{persona}")
    
                if rn == 1:
                    feedback_section = (
                        f"This is Round 1 — there is no prior feedback. "
                        f"Present your initial expert opinion on the topic below.\n"
                    )
                else:
                    prev_mod_key = _scratchpad_key(f"r{rn - 1}_moderator")
                    feedback_section = (
                        f"This is Round {rn}. Read the moderator's Round {rn - 1} "
                        f"feedback from the scratchpad:\n"
                        f"   ```bash\n"
                        f"   {_ctx_snippet}\n"
                        + _read_snippet(prev_mod_key, "MODERATOR_FEEDBACK")
                        + f"   echo \"Moderator feedback: $MODERATOR_FEEDBACK\"\n"
                        f"   ```\n"
                        f"Revise your opinion taking the moderator's synthesis into account.\n"
                        f"Maintain your expert perspective — do NOT simply agree with everyone.\n"
                    )
    
                expert_prompt = (
                    f"You are an expert agent representing the **{persona.upper()}** "
                    f"perspective in a Delphi consensus workflow.\n"
                    f"\n"
                    f"**Topic:** {body.topic}\n"
                    f"**Round:** {rn} of {body.max_rounds}\n"
                    f"**Your persona:** {persona} specialist\n"
                    f"\n"
                    f"{feedback_section}\n"
                    f"Your tasks:\n"
                    f"1. Analyse '{body.topic}' strictly from the **{persona}** "
                    f"specialist perspective.\n"
                    f"   - Identify risks, trade-offs, requirements, and recommendations "
                    f"specific to your domain.\n"
                    f"   - Be concrete and technical. Avoid generic statements.\n"
                    f"   - Keep your opinion to 200-350 words.\n"
                    f"2. Write your opinion to `expert_{persona}_r{rn}.md`.\n"
                    f"3. Store your opinion in the shared scratchpad using this Python script:\n"
                    f"   ```python\n"
                    + _write_snippet(expert_key, f"expert_{persona}_r{rn}.md")
                    + f"\n"
                    f"   ```\n"
                )
    
                expert_task = await orchestrator.submit_task(
                    expert_prompt,
                    required_tags=body.expert_tags or None,
                    depends_on=[prev_moderator_task_id] if prev_moderator_task_id else [],
                )
                role_key = f"expert_{persona}_r{rn}"
                task_ids_map[role_key] = expert_task.id
                all_task_ids.append(expert_task.id)
                expert_task_ids.append(expert_task.id)
    
            # --- Moderator prompt ---
            mod_key = _scratchpad_key(f"r{rn}_moderator")
            round_expert_keys_desc = "\n".join(
                f"   - {persona} opinion: key `{_scratchpad_key(f'r{rn}_{persona}')}`"
                for persona in body.experts
            )
    
            moderator_prompt = (
                f"You are the MODERATOR agent in a Delphi consensus workflow.\n"
                f"\n"
                f"**Topic:** {body.topic}\n"
                f"**Round:** {rn} of {body.max_rounds}\n"
                f"**Expert personas:** {', '.join(body.experts)}\n"
                f"\n"
                f"Your tasks:\n"
                f"1. Read all expert opinions for Round {rn} from the scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                f"   ```\n"
                f"   Keys to read:\n"
                f"{round_expert_keys_desc}\n"
                f"   Use curl to read each key:\n"
                f"   `curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
                f"\n"
                f"2. Write `delphi_round_{rn}.md` containing:\n"
                f"   - **Round {rn} Summary**: 2-3 sentence overview of expert opinions\n"
                f"   - **Points of Convergence**: where experts agree (bullet list)\n"
                f"   - **Points of Divergence**: where experts disagree (bullet list)\n"
                f"   - **Key Insights per Persona**: one paragraph per expert persona\n"
            )
            if rn < body.max_rounds:
                moderator_prompt += (
                    f"   - **Feedback for Round {rn + 1}**: questions/areas for experts "
                    f"to refine in the next round (3-5 bullet points)\n"
                )
            moderator_prompt += (
                f"\n"
                f"3. Store the round summary in the shared scratchpad using this Python script:\n"
                f"   ```python\n"
                + _write_snippet(mod_key, f"delphi_round_{rn}.md")
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Be objective. Accurately represent each expert's position. "
                f"Do not favour any single perspective."
            )
    
            moderator_task = await orchestrator.submit_task(
                moderator_prompt,
                required_tags=body.moderator_tags or None,
                depends_on=expert_task_ids,
            )
            mod_role_key = f"moderator_r{rn}"
            task_ids_map[mod_role_key] = moderator_task.id
            all_task_ids.append(moderator_task.id)
            prev_moderator_task_id = moderator_task.id
    
        # --- Consensus prompt ---
        consensus_key = _scratchpad_key("consensus")
        all_mod_keys_desc = "\n".join(
            f"   - Round {rn} moderator summary: key `{_scratchpad_key(f'r{rn}_moderator')}`"
            for rn in range(1, body.max_rounds + 1)
        )
    
        consensus_prompt = (
            f"You are the CONSENSUS agent in a Delphi consensus workflow.\n"
            f"\n"
            f"**Topic:** {body.topic}\n"
            f"**Rounds completed:** {body.max_rounds}\n"
            f"**Expert personas:** {', '.join(body.experts)}\n"
            f"\n"
            f"Your tasks:\n"
            f"1. Read all moderator round summaries from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   ```\n"
            f"   Keys to read:\n"
            f"{all_mod_keys_desc}\n"
            f"   Use curl to read each: "
            f"`curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
            f"\n"
            f"2. Write `consensus.md` containing:\n"
            f"   - **Topic**: {body.topic}\n"
            f"   - **Expert Perspectives**: brief summary of each persona's view\n"
            f"   - **Consensus Points**: areas of strong agreement across all experts\n"
            f"   - **Remaining Disagreements**: unresolved tensions and why\n"
            f"   - **Recommended Decision**: the most defensible position given all "
            f"expert input\n"
            f"   - **Key Trade-offs**: top 3-5 trade-offs decision-makers should know\n"
            f"   - **Caveats**: conditions under which the recommendation changes\n"
            f"\n"
            f"3. Store the consensus document in the shared scratchpad using this Python script:\n"
            f"   ```python\n"
            + _write_snippet(consensus_key, "consensus.md")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Write a balanced, well-structured consensus.md. "
            f"Represent all expert perspectives fairly."
        )
    
        consensus_task = await orchestrator.submit_task(
            consensus_prompt,
            required_tags=body.moderator_tags or None,
            depends_on=[prev_moderator_task_id] if prev_moderator_task_id else [],
            reply_to=body.reply_to,
        )
        task_ids_map["consensus"] = consensus_task.id
        all_task_ids.append(consensus_task.id)
    
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/redblue",
        summary="Submit a Red Team / Blue Team security review workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_redblue_workflow(body: RedBlueWorkflowSubmit) -> dict:
        """Submit a 3-agent sequential security review Workflow DAG.

        Pipeline (strictly sequential):

          1. **implement** (blue-team): implements *feature_description* in *language*,
             stores code in ``{scratchpad_prefix}_implementation``.
          2. **attack** (red-team): reads implementation, identifies vulnerabilities
             based on *security_focus* list (OWASP categories, severity, line refs),
             stores findings in ``{scratchpad_prefix}_vulnerabilities``.
          3. **assess** (arbiter): reads both artifacts, produces CVSS-style risk
             assessment with overall risk level (LOW/MEDIUM/HIGH/CRITICAL),
             stores report in ``{scratchpad_prefix}_risk_report``.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``redblue/<feature_description>``)
        - ``task_ids``: dict with keys ``implement``, ``attack``, ``assess``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - arXiv:2601.19138, "AgenticSCR: Autonomous Agentic Secure Code Review" (2025):
          agentic multi-iteration code review with contextual awareness.
        - "Red-Teaming LLM MAS via Communication Attacks" ACL 2025 arXiv:2502.14847.
        - OWASP Top 10 for LLMs 2025: structured security_focus maps to OWASP categories.
        - DESIGN.md §10.75 (v1.1.43)
        """

        wm = orchestrator.get_workflow_manager()
        wf_name = f"redblue/{body.feature_description}"

        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"{body.scratchpad_prefix}_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        implementation_key = _scratchpad_key("implementation")
        vulnerabilities_key = _scratchpad_key("vulnerabilities")
        risk_report_key = _scratchpad_key("risk_report")

        security_focus_str = ", ".join(body.security_focus) if body.security_focus else "general security"

        # --- Blue-team (implement) prompt ---
        implement_prompt = (
            f"You are the BLUE-TEAM agent in a Red Team / Blue Team security review workflow.\n"
            f"\n"
            f"**Feature to implement:** {body.feature_description}\n"
            f"**Language:** {body.language}\n"
            f"\n"
            f"Your task is to implement the feature described above in {body.language}.\n"
            f"Write real, functional code. The red-team will scrutinise it for vulnerabilities.\n"
            f"\n"
            f"Steps:\n"
            f"1. Implement the feature in {body.language}:\n"
            f"   - Write complete, runnable code.\n"
            f"   - Include comments explaining your design decisions.\n"
            f"   - Focus on functionality — the red-team will find security gaps.\n"
            f"2. Write your implementation to `implementation.{body.language}` in your working directory.\n"
            f"3. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat implementation.{body.language})\n"
            + _write_snippet(implementation_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be thorough. Write at least 30 lines of real code. Max 600 words total."
        )

        # --- Red-team (attack) prompt ---
        attack_prompt = (
            f"You are the RED-TEAM agent in a Red Team / Blue Team security review workflow.\n"
            f"\n"
            f"**Feature under review:** {body.feature_description}\n"
            f"**Language:** {body.language}\n"
            f"**Security focus areas:** {security_focus_str}\n"
            f"\n"
            f"Your role is to find security vulnerabilities in the blue-team implementation.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the blue-team implementation from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(implementation_key, "IMPLEMENTATION")
            + f"   echo \"Implementation: $IMPLEMENTATION\"\n"
            f"   ```\n"
            f"2. Systematically identify vulnerabilities focusing on: {security_focus_str}\n"
            f"   For each vulnerability:\n"
            f"   - OWASP category (e.g. A01:2021-Broken Access Control)\n"
            f"   - Severity: CRITICAL / HIGH / MEDIUM / LOW\n"
            f"   - Description: exact weakness and which line/function is affected\n"
            f"   - Attack vector: how an attacker would exploit it\n"
            f"3. Write your findings to `vulnerabilities.md` in this format:\n"
            f"   ```\n"
            f"   ## Vulnerability N: <title>\n"
            f"   - OWASP: <category>\n"
            f"   - Severity: <CRITICAL|HIGH|MEDIUM|LOW>\n"
            f"   - Location: <function/line>\n"
            f"   - Description: <what is wrong>\n"
            f"   - Attack vector: <how to exploit>\n"
            f"   ```\n"
            f"4. Store findings in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat vulnerabilities.md)\n"
            + _write_snippet(vulnerabilities_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Find at least 3 vulnerabilities. Be specific and technical. Max 500 words."
        )

        # --- Arbiter (assess) prompt ---
        assess_prompt = (
            f"You are the ARBITER agent in a Red Team / Blue Team security review workflow.\n"
            f"\n"
            f"**Feature under review:** {body.feature_description}\n"
            f"\n"
            f"Your task is to read the implementation and vulnerabilities, then produce\n"
            f"a comprehensive risk assessment report.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read both artifacts from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(implementation_key, "IMPLEMENTATION")
            + _read_snippet(vulnerabilities_key, "VULNERABILITIES")
            + f"   ```\n"
            f"2. Write `risk_report.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Risk Assessment: {body.feature_description}\n"
            f"   ## Implementation Summary\n"
            f"   ## Vulnerability Summary\n"
            f"   ## Risk Matrix\n"
            f"   | ID | Vulnerability | CVSS Score | Severity | Priority |\n"
            f"   |----|---------------|------------|----------|----------|\n"
            f"   ## Overall Risk Level\n"
            f"   <!-- Must be exactly one of: LOW, MEDIUM, HIGH, CRITICAL -->\n"
            f"   **Overall Risk: <LOW|MEDIUM|HIGH|CRITICAL>**\n"
            f"   ## Remediation Priorities\n"
            f"   ## Verdict\n"
            f"   ```\n"
            f"3. Store the report in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat risk_report.md)\n"
            + _write_snippet(risk_report_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be rigorous. The Overall Risk Level must be LOW, MEDIUM, HIGH, or CRITICAL."
        )

        # Submit tasks in pipeline order: implement → attack → assess
        implement_task = await orchestrator.submit_task(
            implement_prompt,
            required_tags=body.blue_tags or None,
            depends_on=[],
        )

        attack_task = await orchestrator.submit_task(
            attack_prompt,
            required_tags=body.red_tags or None,
            depends_on=[implement_task.id],
        )

        assess_task = await orchestrator.submit_task(
            assess_prompt,
            required_tags=body.arbiter_tags or None,
            depends_on=[attack_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "implement": implement_task.id,
            "attack": attack_task.id,
            "assess": assess_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/socratic",
        summary="Submit a Socratic dialogue (questioner/responder/synthesizer) workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_socratic_workflow(body: SocraticWorkflowSubmit) -> dict:
        """Submit a 3-agent Socratic dialogue Workflow DAG.
    
        Pipeline (strictly sequential):
    
          1. **questioner**: applies Maieutic method — probes assumptions,
             demands definitions, challenges logical basis.  Phase 1 uses
             adversarial questions; Phase 2 shifts to integrative questions.
             Stores Q&A log in ``{scratchpad_prefix}_dialogue``.
          2. **responder**: reads questioner output and refines, defends, or
             revises the position.  Appends answers to the dialogue log.
          3. **synthesizer**: reads the complete dialogue and extracts a
             structured ``synthesis.md`` with main arguments, agreed points,
             unresolved questions, and recommendations.  Stores result in
             ``{scratchpad_prefix}_synthesis``.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``socratic/<topic>``)
        - ``task_ids``: dict with keys ``questioner``, ``responder``,
          ``synthesizer``
        - ``scratchpad_prefix``: scratchpad namespace for this run
    
        Design references:
        - Liang et al. "SocraSynth" arXiv:2402.06634 (2024): staged
          questioner → responder → synthesizer with sycophancy suppression.
        - "KELE" arXiv:2409.05511 EMNLP 2025: two-phase questioning.
        - "CONSENSAGENT" ACL 2025: dynamic prompt refinement reduces sycophancy.
        - DESIGN.md §10.24 (v1.0.25)
        """

    
        wm = orchestrator.get_workflow_manager()
        wf_name = f"socratic/{body.topic}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"socratic_{pre_run_id[:8]}"
    
        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"
    
        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )
    
        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )
    
        dialogue_key = _scratchpad_key("dialogue")
        synthesis_key = _scratchpad_key("synthesis")
    
        # --- Questioner prompt ---
        questioner_prompt = (
            f"You are the QUESTIONER agent in a Socratic dialogue workflow.\n"
            f"\n"
            f"**Dialogue topic:** {body.topic}\n"
            f"\n"
            f"Your role is to probe this topic using the Maieutic (midwifery) method.\n"
            f"Do NOT take a position yourself — your job is to draw out and sharpen the\n"
            f"thinking of the responder through precise, incisive questions.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write 4–6 Socratic questions targeting the topic above.\n"
            f"   **Phase 1 questions (questions 1–3)** — adversarial / challenging:\n"
            f"   - 'What exactly do you mean by X?'\n"
            f"   - 'What is your evidence for that assumption?'\n"
            f"   - 'Give a concrete counter-example where that fails.'\n"
            f"   **Phase 2 questions (questions 4–6)** — integrative / constructive:\n"
            f"   - 'Under what conditions would that be correct?'\n"
            f"   - 'What would you need to be true for this to work?'\n"
            f"   - 'Where do you see the strongest case for the alternative?'\n"
            f"2. Write your questions to `questioner_output.md` in your working directory.\n"
            f"3. Store the Q&A log (your questions only for now) in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat questioner_output.md)\n"
            + _write_snippet(dialogue_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be sharp and neutral. Do not answer your own questions. Max 300 words."
        )
    
        # --- Responder prompt ---
        responder_prompt = (
            f"You are the RESPONDER agent in a Socratic dialogue workflow.\n"
            f"\n"
            f"**Dialogue topic:** {body.topic}\n"
            f"\n"
            f"Your role is to answer the Socratic questions posed by the questioner,\n"
            f"refining and defending the best position on this topic.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the questioner's questions from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(dialogue_key, "QUESTIONS")
            + f"   echo \"Questions: $QUESTIONS\"\n"
            f"   ```\n"
            f"2. Write a `responder_output.md` that:\n"
            f"   - Addresses each question in turn (quote each question before answering).\n"
            f"   - Provides concrete, specific answers with evidence or examples.\n"
            f"   - Refines or revises the position where the questioning reveals a weakness.\n"
            f"   - Acknowledges genuine uncertainty rather than bluffing.\n"
            f"3. Append your answers to the dialogue log in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   FULL_DIALOGUE=\"$QUESTIONS\n\n--- RESPONDER ANSWERS ---\n$(cat responder_output.md)\"\n"
            f"   CONTENT=$FULL_DIALOGUE\n"
            + _write_snippet(dialogue_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be honest and precise. Acknowledge where the questioning reveals genuine weaknesses."
            f" Max 400 words."
        )
    
        # --- Synthesizer prompt ---
        synthesizer_prompt = (
            f"You are the SYNTHESIZER agent in a Socratic dialogue workflow.\n"
            f"\n"
            f"**Dialogue topic:** {body.topic}\n"
            f"\n"
            f"Your task is to read the complete Socratic dialogue and extract a structured\n"
            f"conclusion that will be useful to the design team.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the full dialogue from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(dialogue_key, "DIALOGUE")
            + f"   echo \"Dialogue: $DIALOGUE\"\n"
            f"   ```\n"
            f"2. Write `synthesis.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Socratic Synthesis: {body.topic}\n"
            f"   ## Main Arguments Surfaced\n"
            f"   ## Points of Agreement\n"
            f"   ## Unresolved Questions\n"
            f"   ## Recommendations\n"
            f"   ## Key Assumptions and Preconditions\n"
            f"   ```\n"
            f"3. Store the synthesis in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat synthesis.md)\n"
            + _write_snippet(synthesis_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be precise and actionable. The synthesis should help the reader make a\n"
            f"well-informed decision about the topic. Max 400 words."
        )
    
        # Submit tasks in pipeline order
        questioner_task = await orchestrator.submit_task(
            questioner_prompt,
            required_tags=body.questioner_tags or None,
            depends_on=[],
        )
    
        responder_task = await orchestrator.submit_task(
            responder_prompt,
            required_tags=body.responder_tags or None,
            depends_on=[questioner_task.id],
        )
    
        synthesizer_task = await orchestrator.submit_task(
            synthesizer_prompt,
            required_tags=body.synthesizer_tags or None,
            depends_on=[responder_task.id],
            reply_to=body.reply_to,
        )
    
        task_ids_map = {
            "questioner": questioner_task.id,
            "responder": responder_task.id,
            "synthesizer": synthesizer_task.id,
        }
    
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/pair",
        summary="Submit a 2-agent PairCoder (Navigator + Driver) workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_pair_workflow(body: PairWorkflowSubmit) -> dict:
        """Submit a 2-agent PairCoder Workflow DAG.
    
        Pipeline (strictly sequential):
    
          1. **navigator**: analyses the task, writes a structured ``PLAN.md``
             (architecture decisions, interfaces, step-by-step guide, acceptance
             criteria).  Stores the plan in the shared scratchpad.
          2. **driver**: reads the navigator's plan from the scratchpad,
             implements the code, writes tests, runs them, and writes
             ``driver_summary.md`` with a pass/fail report.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``pair/<task[:40]>``)
        - ``task_ids``: dict with keys ``navigator``, ``driver``
        - ``scratchpad_prefix``: scratchpad namespace for this run
    
        Design references:
        - Beck & Fowler "Extreme Programming Explained" (1999).
        - FlowHunt "TDD with AI Agents" (2025): PairCoder quality gains.
        - DESIGN.md §10.27 (v1.0.27)
        """

    
        wm = orchestrator.get_workflow_manager()
        # Use first 40 chars of task as name suffix
        task_slug = body.task[:40].strip().replace("\n", " ")
        wf_name = f"pair/{task_slug}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"pair_{pre_run_id[:8]}"
    
        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"
    
        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )
    
        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )
    
        plan_key = _scratchpad_key("plan")
        result_key = _scratchpad_key("result")
    
        # --- Navigator prompt ---
        navigator_prompt = (
            f"You are the NAVIGATOR agent in a PairCoder workflow.\n"
            f"\n"
            f"**Task:** {body.task}\n"
            f"\n"
            f"Your role is to produce a thorough, structured PLAN.md that the Driver\n"
            f"agent will use to implement the task.  Do NOT write any implementation\n"
            f"code yourself — your job is to plan, not to code.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `PLAN.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Plan: <task title>\n"
            f"   ## Goal\n"
            f"   ## Architecture & Design Decisions\n"
            f"   ## Module / File Layout\n"
            f"   ## Public Interfaces\n"
            f"   ## Step-by-step Implementation Guide\n"
            f"   ## Acceptance Criteria\n"
            f"   ## Edge Cases & Gotchas\n"
            f"   ```\n"
            f"2. Store PLAN.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat PLAN.md)\n"
            + _write_snippet(plan_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be precise and concrete — the Driver must be able to implement from your\n"
            f"plan without additional context.  Max 500 words."
        )
    
        # --- Driver prompt ---
        driver_prompt = (
            f"You are the DRIVER agent in a PairCoder workflow.\n"
            f"\n"
            f"**Task:** {body.task}\n"
            f"\n"
            f"Your role is to implement the task strictly following the Navigator's\n"
            f"PLAN.md.  Write the code, tests, run the tests, and report results.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the Navigator's plan from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(plan_key, "PLAN")
            + f"   echo \"$PLAN\" > PLAN.md\n"
            f"   cat PLAN.md\n"
            f"   ```\n"
            f"2. Implement the code exactly as described in PLAN.md.\n"
            f"   - Create the implementation file(s) in your working directory.\n"
            f"   - Create test file(s) following the acceptance criteria in PLAN.md.\n"
            f"3. Run the tests and capture the result:\n"
            f"   ```bash\n"
            f"   python -m pytest test_*.py -v 2>&1 | tee test_output.txt || true\n"
            f"   ```\n"
            f"4. Write `driver_summary.md` with:\n"
            f"   ```markdown\n"
            f"   # Driver Summary\n"
            f"   ## Implementation\n"
            f"   ## Test Results\n"
            f"   ## Deviations from Plan\n"
            f"   ## Status: PASS | FAIL\n"
            f"   ```\n"
            f"5. Store the summary in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat driver_summary.md)\n"
            + _write_snippet(result_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Follow the plan faithfully.  If a step is unclear, make the simplest\n"
            f"reasonable interpretation and document it in Deviations from Plan."
        )
    
        # Submit tasks in pipeline order
        navigator_task = await orchestrator.submit_task(
            navigator_prompt,
            required_tags=body.navigator_tags or None,
            depends_on=[],
        )
    
        driver_task = await orchestrator.submit_task(
            driver_prompt,
            required_tags=body.driver_tags or None,
            depends_on=[navigator_task.id],
            reply_to=body.reply_to,
        )
    
        task_ids_map = {
            "navigator": navigator_task.id,
            "driver": driver_task.id,
        }
    
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/fulldev",
        summary="Submit a 5-agent Full Software Development Lifecycle workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_fulldev_workflow(body: FulldevWorkflowSubmit) -> dict:
        """Submit a 5-agent Full Software Development Lifecycle Workflow DAG.
    
        Pipeline (each step depends_on the previous):
    
        1. **spec-writer**: writes a precise feature specification (SPEC.md) with
           functional requirements and acceptance criteria; stores it to scratchpad.
        2. **architect**: reads the spec, writes an architecture/design document
           (DESIGN.md or ADR) with component breakdown and interface definitions;
           stores it to scratchpad.
        3. **tdd-test-writer** (RED): reads spec + design, writes failing pytest
           tests that codify acceptance criteria; stores test file path to scratchpad.
        4. **tdd-implementer** (GREEN): reads spec + test file path, writes the
           minimal implementation that makes all tests pass; stores impl path.
        5. **reviewer**: reads spec, tests, and implementation; writes a structured
           code review (REVIEW.md) categorising blocking/non-blocking issues.
    
        All artifacts are passed via the shared scratchpad (Blackboard pattern).
        Scratchpad keys use underscores (not slashes) as namespace separator.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``fulldev/<feature>``)
        - ``task_ids``: dict with keys ``spec_writer``, ``architect``,
          ``test_writer``, ``implementer``, ``reviewer`` mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``fulldev_{run_id[:8]}``)
    
        Design references:
        - MetaGPT arXiv:2308.00352 (2023/2024): PM → Architect → Engineer SOP pipeline.
        - AgentMesh arXiv:2507.19902 (2025): Planner → Coder → Debugger → Reviewer.
        - arXiv:2508.00083 "Survey on Code Generation with LLM-based Agents" (2025).
        - arXiv:2505.16339 "Rethinking Code Review Workflows" (2025).
        - DESIGN.md §10.16 (v0.42.0)
        """

    
        lang = body.language
        feature_slug = body.feature.replace(" ", "_")
    
        wm = orchestrator.get_workflow_manager()
        wf_name = f"fulldev/{body.feature}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"fulldev_{pre_run_id[:8]}"
    
        # Scratchpad keys (underscores only — no slashes)
        spec_key = f"{scratchpad_prefix}_spec"
        design_key = f"{scratchpad_prefix}_design"
        tests_key = f"{scratchpad_prefix}_tests"
        impl_key = f"{scratchpad_prefix}_impl"
        review_key = f"{scratchpad_prefix}_review"
    
        # Shared bash snippet for reading context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )
    
        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )
    
        # ---------------------------------------------------------------
        # 1. SPEC-WRITER prompt
        # ---------------------------------------------------------------
        spec_writer_prompt = (
            f"You are the SPEC-WRITER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature to specify:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task:\n"
            f"1. Write a precise, unambiguous specification for '{body.feature}'.\n"
            f"   Use this format:\n"
            f"   ```markdown\n"
            f"   # Specification: {body.feature}\n"
            f"   ## Context\n"
            f"   ## Functional Requirements\n"
            f"   1. <FR-1> ...\n"
            f"   ## Acceptance Criteria\n"
            f"   - AC-1: Given ... when ... then ...\n"
            f"   ## Out of Scope\n"
            f"   ## Glossary\n"
            f"   ```\n"
            f"2. Save the specification to `SPEC.md` in your working directory.\n"
            f"3. Store the SPEC.md contents in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat SPEC.md)\n"
            + _write_snippet(spec_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Focus on WHAT the system must do, not HOW to implement it.\n"
            f"Be precise: every requirement must be verifiable. Max 400 words."
        )
    
        # ---------------------------------------------------------------
        # 2. ARCHITECT prompt
        # ---------------------------------------------------------------
        architect_prompt = (
            f"You are the ARCHITECT agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read the feature specification from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + f"   echo \"Spec loaded: $(echo $SPEC | wc -c) chars\"\n"
            f"   ```\n"
            f"2. Design the {lang} implementation architecture:\n"
            f"   - Identify the main components/classes/modules.\n"
            f"   - Define the public API (function signatures, class interfaces).\n"
            f"   - List key design decisions (data structures, error handling strategy).\n"
            f"   - Note any constraints or non-functional requirements.\n"
            f"3. Write the design to `DESIGN.md`:\n"
            f"   ```markdown\n"
            f"   # Design: {body.feature}\n"
            f"   ## Components\n"
            f"   ## Public API\n"
            f"   ## Key Design Decisions\n"
            f"   ## Implementation Notes\n"
            f"   ```\n"
            f"4. Store DESIGN.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat DESIGN.md)\n"
            + _write_snippet(design_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Focus on the architecture, not the implementation. Max 400 words."
        )
    
        # ---------------------------------------------------------------
        # 3. TDD-TEST-WRITER prompt
        # ---------------------------------------------------------------
        test_writer_prompt = (
            f"You are the TDD-TEST-WRITER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (RED phase — write failing tests first):\n"
            f"1. Read the specification and design from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + _read_snippet(design_key, "DESIGN")
            + f"   echo \"Spec + Design loaded\"\n"
            f"   ```\n"
            f"2. Write {lang} pytest tests for '{body.feature}':\n"
            f"   - Each test must correspond to an Acceptance Criterion in the spec.\n"
            f"   - Tests must FAIL against an empty/stub implementation.\n"
            f"   - Use descriptive test names that act as a specification.\n"
            f"   - Import the module that the implementer will create.\n"
            f"3. Save tests to `test_{feature_slug}.py` in your working directory.\n"
            f"4. Verify tests fail: `python -m pytest test_{feature_slug}.py -v 2>&1 | tail -20`\n"
            f"   (Expect ImportError or AssertionError — that's correct for RED phase)\n"
            f"5. Store the ABSOLUTE PATH to the test file in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(pwd)/test_{feature_slug}.py\n"
            + _write_snippet(tests_key, "CONTENT")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Do NOT write any implementation code. Focus only on tests."
        )
    
        # ---------------------------------------------------------------
        # 4. TDD-IMPLEMENTER prompt
        # ---------------------------------------------------------------
        implementer_prompt = (
            f"You are the TDD-IMPLEMENTER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task (GREEN phase — make tests pass):\n"
            f"1. Read the specification and test file path from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + _read_snippet(tests_key, "TEST_FILE")
            + f"   echo \"Spec loaded, test file: $TEST_FILE\"\n"
            f"   ```\n"
            f"2. Read the test file: `cat $TEST_FILE`\n"
            f"3. Write the MINIMAL {lang} implementation of '{body.feature}' that makes\n"
            f"   all tests pass:\n"
            f"   - Implement only what the tests require.\n"
            f"   - Follow the design from the spec (the architect's DESIGN.md).\n"
            f"   - Write the module in the same directory as the tests\n"
            f"     (so the test imports work).\n"
            f"4. Verify: `python -m pytest $TEST_FILE -v`\n"
            f"   All tests must pass before proceeding.\n"
            f"5. Store the ABSOLUTE PATH to the implementation file in the scratchpad:\n"
            f"   ```bash\n"
            f"   # Replace 'your_impl_file.py' with the actual filename\n"
            f"   CONTENT=$(pwd)/your_impl_file.py\n"
            + _write_snippet(impl_key, "CONTENT")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Implement '{body.feature}' to make all tests pass."
        )
    
        # ---------------------------------------------------------------
        # 5. REVIEWER prompt
        # ---------------------------------------------------------------
        reviewer_prompt = (
            f"You are the REVIEWER agent in a Full Software Development Lifecycle workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read all artifacts from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + _read_snippet(tests_key, "TEST_FILE")
            + _read_snippet(impl_key, "IMPL_FILE")
            + f"   echo \"All artifacts loaded\"\n"
            f"   echo \"Test file: $TEST_FILE\"\n"
            f"   echo \"Impl file: $IMPL_FILE\"\n"
            f"   ```\n"
            f"2. Read the actual test and implementation files:\n"
            f"   `cat $TEST_FILE && cat $IMPL_FILE`\n"
            f"3. Run the tests to verify they pass:\n"
            f"   `python -m pytest $TEST_FILE -v`\n"
            f"4. Write a structured code review to `REVIEW.md`:\n"
            f"   ```markdown\n"
            f"   # Code Review: {body.feature}\n"
            f"   ## Summary\n"
            f"   ## Spec Compliance\n"
            f"   (Does the implementation satisfy all acceptance criteria?)\n"
            f"   ## Blocking Issues\n"
            f"   ## Non-Blocking Issues\n"
            f"   ## Suggestions\n"
            f"   ## Test Coverage Assessment\n"
            f"   ## Conclusion: APPROVED / APPROVED WITH CHANGES / REJECTED\n"
            f"   ```\n"
            f"5. Store your review in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat REVIEW.md)\n"
            + _write_snippet(review_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be rigorous. Do not approve code that fails tests or violates the spec."
        )
    
        # ---------------------------------------------------------------
        # Submit the 5-step DAG (linear pipeline)
        # ---------------------------------------------------------------
        sw_task = await orchestrator.submit_task(
            spec_writer_prompt,
            required_tags=body.spec_writer_tags or None,
        )
        arch_task = await orchestrator.submit_task(
            architect_prompt,
            required_tags=body.architect_tags or None,
            depends_on=[sw_task.id],
        )
        tw_task = await orchestrator.submit_task(
            test_writer_prompt,
            required_tags=body.test_writer_tags or None,
            depends_on=[arch_task.id],
        )
        impl_task = await orchestrator.submit_task(
            implementer_prompt,
            required_tags=body.implementer_tags or None,
            depends_on=[tw_task.id],
        )
        rev_task = await orchestrator.submit_task(
            reviewer_prompt,
            required_tags=body.reviewer_tags or None,
            depends_on=[impl_task.id],
            reply_to=body.reply_to,
        )
    
        task_ids_map = {
            "spec_writer": sw_task.id,
            "architect": arch_task.id,
            "test_writer": tw_task.id,
            "implementer": impl_task.id,
            "reviewer": rev_task.id,
        }
    
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/clean-arch",
        summary="Submit a 4-agent Clean Architecture pipeline workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_clean_arch_workflow(body: CleanArchWorkflowSubmit) -> dict:
        """Submit a 4-agent Clean Architecture Workflow DAG.
    
        Pipeline (each step depends_on the previous):
    
        1. **domain-designer**: defines domain Entities, Value Objects, Aggregates,
           and Domain Events without any framework dependency; writes ``DOMAIN.md``
           and stores it in the shared scratchpad.
        2. **usecase-designer**: reads the domain layer; defines Use Cases
           (Application Interactors), Input/Output DTOs, and Port interfaces (abstract
           boundaries); writes ``USECASES.md`` and stores it.
        3. **adapter-designer**: reads domain + use-cases; defines concrete Interface
           Adapters (Repository implementations, Presenters, Controllers); writes
           ``ADAPTERS.md`` and stores it.
        4. **framework-designer**: reads all previous layers; synthesises the complete
           architecture into ``ARCHITECTURE.md`` plus writes executable Python skeleton
           files demonstrating framework wiring (FastAPI/SQLite/CLI); stores the result.
    
        All artifacts are passed via the shared scratchpad (Blackboard pattern).
        Scratchpad keys use underscores (not slashes) as namespace separator.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``clean-arch/<feature>``)
        - ``task_ids``: dict with keys ``domain_designer``, ``usecase_designer``,
          ``adapter_designer``, ``framework_designer`` mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``cleanarch_{run_id[:8]}``)
    
        Design references:
        - Robert C. Martin, "Clean Architecture" (2017): Domain → Use Cases →
          Interface Adapters → Frameworks & Drivers.
        - AgentMesh arXiv:2507.19902 (2025): 4-role artifact-centric pipeline.
        - Muthu (2025-11) "The Architecture is the Prompt".
        - DESIGN.md §10.30 (v1.0.30)
        """

    
        lang = body.language
        wm = orchestrator.get_workflow_manager()
        wf_name = f"clean-arch/{body.feature}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"cleanarch_{pre_run_id[:8]}"
    
        # Scratchpad keys (underscores only — no slashes)
        domain_key = f"{scratchpad_prefix}_domain"
        usecases_key = f"{scratchpad_prefix}_usecases"
        adapters_key = f"{scratchpad_prefix}_adapters"
        arch_key = f"{scratchpad_prefix}_arch"
    
        # Shared bash snippet for reading context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )
    
        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )
    
        # ---------------------------------------------------------------
        # 1. DOMAIN-DESIGNER prompt
        # ---------------------------------------------------------------
        domain_designer_prompt = (
            f"You are the DOMAIN-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature to design:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to define the innermost layer of Clean Architecture:\n"
            f"the **Domain layer** (Entities, Value Objects, Aggregates, Domain Events).\n"
            f"This layer must have ZERO framework or infrastructure dependencies.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `DOMAIN.md` in your working directory with this structure:\n"
            f"   ```markdown\n"
            f"   # Domain Layer: {body.feature}\n"
            f"   ## Entities\n"
            f"   (Core business objects with identity — list each with fields and invariants)\n"
            f"   ## Value Objects\n"
            f"   (Immutable descriptors without identity)\n"
            f"   ## Aggregates\n"
            f"   (Consistency boundaries — specify aggregate root)\n"
            f"   ## Domain Events\n"
            f"   (State changes that domain experts care about)\n"
            f"   ## Domain Rules / Invariants\n"
            f"   (Business rules that must always hold)\n"
            f"   ## Ubiquitous Language\n"
            f"   (Key terms with precise definitions)\n"
            f"   ```\n"
            f"2. Write stub {lang} classes for each Entity and Value Object in\n"
            f"   `domain.py` (pure {lang} — no external imports except stdlib).\n"
            f"3. Store DOMAIN.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat DOMAIN.md)\n"
            + _write_snippet(domain_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Focus ONLY on the domain model. Do NOT define use cases, repositories,\n"
            f"or any infrastructure. Keep it under 400 words."
        )
    
        # ---------------------------------------------------------------
        # 2. USECASE-DESIGNER prompt
        # ---------------------------------------------------------------
        usecase_designer_prompt = (
            f"You are the USECASE-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to define the **Application / Use-Case layer** of Clean\n"
            f"Architecture: Use Cases (Interactors), Input/Output DTOs, and Port\n"
            f"interfaces (abstract boundaries the domain defines for infrastructure).\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the Domain layer from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(domain_key, "DOMAIN")
            + f"   echo \"Domain loaded: $(echo $DOMAIN | wc -c) chars\"\n"
            f"   ```\n"
            f"2. Write `USECASES.md` with this structure:\n"
            f"   ```markdown\n"
            f"   # Use-Case Layer: {body.feature}\n"
            f"   ## Use Cases (Interactors)\n"
            f"   (List each use case: name, input DTO fields, output DTO fields,\n"
            f"    steps, domain rules invoked)\n"
            f"   ## Port Interfaces\n"
            f"   (Abstract interfaces the use cases need — e.g. IRepository, INotifier)\n"
            f"   ## Input / Output DTOs\n"
            f"   (Plain data structures crossing the use-case boundary)\n"
            f"   ```\n"
            f"3. Write stub {lang} abstract classes / protocols for each Port in\n"
            f"   `ports.py` (stdlib `abc.ABC` only, no framework imports).\n"
            f"4. Store USECASES.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat USECASES.md)\n"
            + _write_snippet(usecases_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Derive use cases strictly from the Domain layer above.\n"
            f"Do NOT define adapters or framework wiring. Max 400 words."
        )
    
        # ---------------------------------------------------------------
        # 3. ADAPTER-DESIGNER prompt
        # ---------------------------------------------------------------
        adapter_designer_prompt = (
            f"You are the ADAPTER-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to define the **Interface Adapters layer** of Clean\n"
            f"Architecture: concrete implementations of Port interfaces (Repositories,\n"
            f"Presenters, Controllers / Request Handlers).\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the Domain and Use-Case layers from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(domain_key, "DOMAIN")
            + _read_snippet(usecases_key, "USECASES")
            + f"   echo \"Domain + Use-Cases loaded\"\n"
            f"   ```\n"
            f"2. Write `ADAPTERS.md` with this structure:\n"
            f"   ```markdown\n"
            f"   # Interface Adapters Layer: {body.feature}\n"
            f"   ## Repository Adapters\n"
            f"   (Concrete implementations of IRepository ports — e.g. SQLite, in-memory)\n"
            f"   ## Presenter Adapters\n"
            f"   (Transforms use-case output DTOs to view models / JSON responses)\n"
            f"   ## Controller / Request Handler Adapters\n"
            f"   (Translates HTTP/CLI input to use-case input DTOs)\n"
            f"   ## Adapter Dependency Map\n"
            f"   (Which adapter implements which port)\n"
            f"   ```\n"
            f"3. Write stub {lang} classes for each adapter in `adapters.py`.\n"
            f"   - Repositories may import sqlite3 or similar stdlib only.\n"
            f"   - Implement the Port abstract interfaces from ports.py.\n"
            f"4. Store ADAPTERS.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat ADAPTERS.md)\n"
            + _write_snippet(adapters_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Adapters implement Ports — they must NOT import domain entities directly\n"
            f"except through the Port interfaces. Max 400 words."
        )
    
        # ---------------------------------------------------------------
        # 4. FRAMEWORK-DESIGNER prompt
        # ---------------------------------------------------------------
        framework_designer_prompt = (
            f"You are the FRAMEWORK-DESIGNER agent in a Clean Architecture workflow.\n"
            f"\n"
            f"**Feature:** {body.feature}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to synthesise ALL previous layers and define the outermost\n"
            f"**Frameworks & Drivers layer** of Clean Architecture: the wiring that\n"
            f"connects adapters to use cases via dependency injection.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read all previous layers from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(domain_key, "DOMAIN")
            + _read_snippet(usecases_key, "USECASES")
            + _read_snippet(adapters_key, "ADAPTERS")
            + f"   echo \"All layers loaded\"\n"
            f"   ```\n"
            f"2. Write `ARCHITECTURE.md` synthesising the complete design:\n"
            f"   ```markdown\n"
            f"   # Clean Architecture: {body.feature}\n"
            f"   ## Layer Overview\n"
            f"   (Concentric rings: Domain → Use Cases → Interface Adapters → Frameworks)\n"
            f"   ## Dependency Rule\n"
            f"   (Verify: each layer only imports inward)\n"
            f"   ## Framework Wiring\n"
            f"   (How FastAPI / SQLite / CLI entry points connect to adapters and use cases)\n"
            f"   ## Module Structure\n"
            f"   (directory tree with file → layer mapping)\n"
            f"   ## Dependency Injection Strategy\n"
            f"   (How adapters are injected into interactors at startup)\n"
            f"   ## Key Design Decisions\n"
            f"   ```\n"
            f"3. Write `main.py` as the composition root that wires everything together:\n"
            f"   - Instantiates repository adapters\n"
            f"   - Injects them into use-case interactors\n"
            f"   - Exposes a simple CLI or HTTP entry point\n"
            f"   Keep it under 60 lines.\n"
            f"4. Store ARCHITECTURE.md in the scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat ARCHITECTURE.md)\n"
            + _write_snippet(arch_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"The composition root (main.py) is the ONLY place where all layers touch.\n"
            f"Verify the Dependency Rule: inner layers must NOT import outer layers."
        )
    
        # ---------------------------------------------------------------
        # Submit the 4-step DAG (linear pipeline)
        # ---------------------------------------------------------------
        domain_task = await orchestrator.submit_task(
            domain_designer_prompt,
            required_tags=body.domain_designer_tags or None,
        )
        usecase_task = await orchestrator.submit_task(
            usecase_designer_prompt,
            required_tags=body.usecase_designer_tags or None,
            depends_on=[domain_task.id],
        )
        adapter_task = await orchestrator.submit_task(
            adapter_designer_prompt,
            required_tags=body.adapter_designer_tags or None,
            depends_on=[usecase_task.id],
        )
        framework_task = await orchestrator.submit_task(
            framework_designer_prompt,
            required_tags=body.framework_designer_tags or None,
            depends_on=[adapter_task.id],
            reply_to=body.reply_to,
        )
    
        task_ids_map = {
            "domain_designer": domain_task.id,
            "usecase_designer": usecase_task.id,
            "adapter_designer": adapter_task.id,
            "framework_designer": framework_task.id,
        }
    
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/ddd",
        summary="Submit a DDD Bounded Context decomposition workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_ddd_workflow(body: DDDWorkflowSubmit) -> dict:
        """Submit a DDD Bounded Context Decomposition Workflow DAG.
    
        Three-phase pipeline:
    
        1. **context-mapper** (Phase 1 — sequential): performs EventStorming
           analysis on the topic; identifies Bounded Contexts and their Ubiquitous
           Language; writes ``EVENTSTORMING.md`` and ``BOUNDED_CONTEXTS.md``;
           stores context list in scratchpad under ``{prefix}_contexts``.
    
        2. **domain-expert-{context}** (Phase 2 — parallel, one per context):
           reads ``BOUNDED_CONTEXTS.md``; designs the domain model for its
           assigned context (Entities, Aggregates, Value Objects, Domain Services);
           writes ``DOMAIN_{CONTEXT}.md`` and stores in ``{prefix}_domain_{context}``.
    
        3. **integration-designer** (Phase 3 — sequential, depends on all domain
           experts): reads all domain models; produces ``CONTEXT_MAP.md`` with
           context-mapping patterns (Shared Kernel / Customer–Supplier / ACL)
           between every pair of Bounded Contexts.
    
        All artifacts are passed via the shared scratchpad (Blackboard pattern).
        Scratchpad keys use underscores (not slashes) as namespace separator.
    
        If ``contexts`` is supplied in the request body, those names are used
        directly (skipping autonomous discovery). If omitted, the context-mapper
        discovers them from the topic description.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``ddd/<topic>``)
        - ``task_ids``: dict with keys ``context_mapper``,
          ``domain_expert_{context}`` (one per context), and
          ``integration_designer``, mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``ddd_{run_id[:8]}``)
    
        Design references:
        - Evans, "Domain-Driven Design" (2003): Bounded Context + Ubiquitous Language.
        - IJCSE V12I3P102 (2025): EventStorming → agent communication protocols.
        - Russ Miles, "Domain-Driven Agent Design", 2025 (DICE framework).
        - DESIGN.md §10.31 (v1.0.31)
        """

    
        lang = body.language
        topic = body.topic
        wm = orchestrator.get_workflow_manager()
        wf_name = f"ddd/{topic}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"ddd_{pre_run_id[:8]}"
    
        # Scratchpad keys
        contexts_key = f"{scratchpad_prefix}_contexts"
        bounded_contexts_key = f"{scratchpad_prefix}_bounded_contexts"
    
        # Shared bash snippet for reading context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )
    
        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )
    
        # Determine contexts list: use provided or defer to agent discovery
        contexts: list[str] = [c.strip() for c in body.contexts if c.strip()]
        contexts_hint = (
            f"Use these Bounded Context names exactly: {', '.join(contexts)}"
            if contexts
            else "Identify 2–4 Bounded Contexts autonomously from the topic description."
        )
    
        # ---------------------------------------------------------------
        # Phase 1: CONTEXT-MAPPER prompt
        # ---------------------------------------------------------------
        context_mapper_prompt = (
            f"You are the CONTEXT-MAPPER agent in a DDD (Domain-Driven Design) workflow.\n"
            f"\n"
            f"**Topic:** {topic}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role is to perform an **EventStorming** analysis and identify the\n"
            f"**Bounded Contexts** that structure the domain.\n"
            f"\n"
            f"{contexts_hint}\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `EVENTSTORMING.md` in your working directory:\n"
            f"   ```markdown\n"
            f"   # EventStorming: {topic}\n"
            f"   ## Domain Events\n"
            f"   (Orange stickies — past-tense facts: 'OrderPlaced', 'PaymentFailed')\n"
            f"   ## Commands\n"
            f"   (Blue stickies — actions that trigger events: 'PlaceOrder', 'ProcessPayment')\n"
            f"   ## Aggregates\n"
            f"   (Yellow stickies — clusters of related events and commands)\n"
            f"   ## Bounded Contexts Identified\n"
            f"   (List each context name with a 1-sentence purpose)\n"
            f"   ```\n"
            f"2. Write `BOUNDED_CONTEXTS.md` in your working directory:\n"
            f"   ```markdown\n"
            f"   # Bounded Contexts: {topic}\n"
            f"   (For each context, include:)\n"
            f"   ## <Context Name>\n"
            f"   **Purpose:** <one sentence>\n"
            f"   **Ubiquitous Language:** <key domain terms specific to this context>\n"
            f"   **Core Aggregates:** <names>\n"
            f"   **Inbound Events:** <events this context reacts to>\n"
            f"   **Outbound Events:** <events this context produces>\n"
            f"   ```\n"
            f"3. Store BOUNDED_CONTEXTS.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat BOUNDED_CONTEXTS.md)\n"
            + _write_snippet(bounded_contexts_key)
            + f"\n"
            f"   ```\n"
            f"4. Store the comma-separated list of context names (no spaces around commas):\n"
            f"   ```bash\n"
            f"   # Example: CONTEXT_NAMES='Orders,Inventory,Shipping'\n"
            f"   CONTEXT_NAMES='<comma-separated list>'\n"
            + _write_snippet(contexts_key, "CONTEXT_NAMES")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Keep EVENTSTORMING.md and BOUNDED_CONTEXTS.md under 600 words combined.\n"
            f"Focus on strategic design — do NOT write implementation code."
        )
    
        # ---------------------------------------------------------------
        # Phase 2: DOMAIN-EXPERT prompts (one per context)
        # ---------------------------------------------------------------
        # Build per-context scratchpad keys and prompts
        def _domain_key(ctx_name: str) -> str:
            safe = ctx_name.lower().replace(" ", "_").replace("-", "_")
            return f"{scratchpad_prefix}_domain_{safe}"
    
        def _domain_expert_prompt(ctx_name: str) -> str:
            dk = _domain_key(ctx_name)
            safe = ctx_name.lower().replace(" ", "_").replace("-", "_")
            return (
                f"You are the DOMAIN-EXPERT agent for the **{ctx_name}** Bounded Context\n"
                f"in a DDD workflow.\n"
                f"\n"
                f"**Overall Topic:** {topic}\n"
                f"**Your Bounded Context:** {ctx_name}\n"
                f"**Language:** {lang}\n"
                f"\n"
                f"Steps:\n"
                f"1. Read the full Bounded Contexts overview from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(bounded_contexts_key, "BOUNDED_CONTEXTS")
                + f"   echo \"Bounded contexts loaded: $(echo $BOUNDED_CONTEXTS | wc -c) chars\"\n"
                f"   ```\n"
                f"2. Write `DOMAIN_{safe.upper()}.md` in your working directory:\n"
                f"   ```markdown\n"
                f"   # Domain Model: {ctx_name}\n"
                f"   ## Entities\n"
                f"   (Objects with identity — list each with fields and invariants)\n"
                f"   ## Value Objects\n"
                f"   (Immutable descriptors without identity)\n"
                f"   ## Aggregates\n"
                f"   (Consistency boundaries — specify aggregate root and invariants)\n"
                f"   ## Domain Services\n"
                f"   (Stateless operations that don't belong to a single aggregate)\n"
                f"   ## Domain Events\n"
                f"   (Past-tense facts this context produces)\n"
                f"   ## Ubiquitous Language\n"
                f"   (Term → definition, unique to this context)\n"
                f"   ```\n"
                f"3. Write stub {lang} classes for entities/value objects in\n"
                f"   `domain_{safe}.py` (pure {lang} — stdlib only, no frameworks).\n"
                f"4. Store the domain model in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   CONTENT=$(cat DOMAIN_{safe.upper()}.md)\n"
                + _write_snippet(dk)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Focus ONLY on the {ctx_name} context. Do NOT design other contexts.\n"
                f"Keep it under 400 words."
            )
    
        # ---------------------------------------------------------------
        # Phase 3: INTEGRATION-DESIGNER prompt
        # ---------------------------------------------------------------
        def _integration_designer_prompt(ctx_names: list[str]) -> str:
            read_all = ""
            for ctx in ctx_names:
                dk = _domain_key(ctx)
                safe_var = ctx.upper().replace(" ", "_").replace("-", "_")
                read_all += _read_snippet(dk, f"DOMAIN_{safe_var}")
            return (
                f"You are the INTEGRATION-DESIGNER agent in a DDD workflow.\n"
                f"\n"
                f"**Topic:** {topic}\n"
                f"**Language:** {lang}\n"
                f"\n"
                f"Your role is to produce a **Context Map** — the strategic diagram\n"
                f"that shows how the Bounded Contexts relate and integrate.\n"
                f"\n"
                f"Steps:\n"
                f"1. Read all domain models from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(bounded_contexts_key, "BOUNDED_CONTEXTS")
                + read_all
                + f"   echo \"All domain models loaded\"\n"
                f"   ```\n"
                f"2. Write `CONTEXT_MAP.md` in your working directory:\n"
                f"   ```markdown\n"
                f"   # Context Map: {topic}\n"
                f"   ## Summary\n"
                f"   (1–2 sentences: what the full domain does)\n"
                f"   ## Bounded Contexts\n"
                f"   (Recap: one line per context with its core responsibility)\n"
                f"   ## Context Relationships\n"
                f"   (For each pair of contexts that interact, specify the pattern:)\n"
                f"   ### <Context A> ↔ <Context B>\n"
                f"   **Pattern:** Shared Kernel | Customer–Supplier | Conformist | ACL | Published Language\n"
                f"   **Direction:** <upstream> → <downstream>\n"
                f"   **Shared Concepts:** <terms or events shared across the boundary>\n"
                f"   **Integration Mechanism:** <domain events / REST API / message queue / direct call>\n"
                f"   ## Anti-Corruption Layers\n"
                f"   (List any ACLs required and what they translate)\n"
                f"   ## Shared Kernel\n"
                f"   (List any truly shared types, if applicable; otherwise 'None')\n"
                f"   ## Key Design Decisions\n"
                f"   (Rationale for the chosen integration patterns)\n"
                f"   ```\n"
                f"3. Store CONTEXT_MAP.md in the shared scratchpad:\n"
                f"   ```bash\n"
                f"   CONTENT=$(cat CONTEXT_MAP.md)\n"
                + _write_snippet(f"{scratchpad_prefix}_context_map")
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Use standard DDD context-mapping patterns (Evans 2003, Vernon 2013).\n"
                f"Keep CONTEXT_MAP.md under 600 words."
            )
    
        # ---------------------------------------------------------------
        # Submit the DAG
        # Phase 1: context-mapper (no dependencies)
        # Phase 2: domain-expert-N (each depends on context-mapper)
        # Phase 3: integration-designer (depends on ALL domain-experts)
        # ---------------------------------------------------------------
        context_mapper_task = await orchestrator.submit_task(
            context_mapper_prompt,
            required_tags=body.context_mapper_tags or None,
        )
    
        # For provided contexts, submit phase 2 immediately (with dependency on context-mapper).
        # If contexts not provided, we still need to submit the domain-expert tasks; we use the
        # provided-contexts path. When contexts=[], we fall back to a single placeholder context
        # that instructs the agent to read the context list from scratchpad.
        effective_contexts = contexts if contexts else ["(auto-discovered)"]
    
        domain_expert_tasks = []
        task_ids_map: dict[str, str] = {"context_mapper": context_mapper_task.id}
    
        if contexts:
            # Contexts are known — submit domain-expert tasks now
            for ctx_name in contexts:
                prompt = _domain_expert_prompt(ctx_name)
                t = await orchestrator.submit_task(
                    prompt,
                    required_tags=body.domain_expert_tags or None,
                    depends_on=[context_mapper_task.id],
                )
                safe = ctx_name.lower().replace(" ", "_").replace("-", "_")
                task_ids_map[f"domain_expert_{safe}"] = t.id
                domain_expert_tasks.append(t)
        else:
            # Contexts not provided — submit a single domain-expert task that reads
            # the context list from scratchpad and designs all contexts in one pass.
            auto_prompt = (
                f"You are the DOMAIN-EXPERT agent in a DDD workflow.\n"
                f"\n"
                f"**Topic:** {topic}\n"
                f"**Language:** {lang}\n"
                f"\n"
                f"The context-mapper agent has already identified the Bounded Contexts.\n"
                f"Read the context list and design ALL domain models in a single pass.\n"
                f"\n"
                f"Steps:\n"
                f"1. Read the Bounded Contexts overview from the shared scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(bounded_contexts_key, "BOUNDED_CONTEXTS")
                + _read_snippet(contexts_key, "CONTEXT_NAMES")
                + f"   echo \"Contexts: $CONTEXT_NAMES\"\n"
                f"   ```\n"
                f"2. For EACH context in CONTEXT_NAMES, write a `DOMAIN_<CONTEXT>.md` file\n"
                f"   with Entities, Value Objects, Aggregates, Domain Services, Domain Events,\n"
                f"   and Ubiquitous Language sections.\n"
                f"3. Store each domain model in the scratchpad:\n"
                f"   ```bash\n"
                f"   for CTX in $(echo $CONTEXT_NAMES | tr ',' ' '); do\n"
                f"     SAFE=$(echo $CTX | tr '[:upper:]' '[:lower:]' | tr ' -' '_')\n"
                f"     CONTENT=$(cat DOMAIN_${{CTX}}.md 2>/dev/null || echo \"(empty)\")\n"
                + _write_snippet(f"{scratchpad_prefix}_domain_$SAFE")
                + f"\n"
                f"   done\n"
                f"   ```\n"
                f"\n"
                f"Keep each domain model under 300 words. Focus on strategic design."
            )
            t = await orchestrator.submit_task(
                auto_prompt,
                required_tags=body.domain_expert_tags or None,
                depends_on=[context_mapper_task.id],
            )
            task_ids_map["domain_expert_auto"] = t.id
            domain_expert_tasks.append(t)
    
        # Phase 3: integration-designer depends on ALL domain-expert tasks
        integration_prompt = _integration_designer_prompt(
            contexts if contexts else ["(all contexts from scratchpad)"]
        )
        integration_task = await orchestrator.submit_task(
            integration_prompt,
            required_tags=body.integration_designer_tags or None,
            depends_on=[t.id for t in domain_expert_tasks],
            reply_to=body.reply_to,
        )
        task_ids_map["integration_designer"] = integration_task.id
    
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.post(
        "/workflows/competition",
        summary="Submit a Best-of-N competitive solver workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_competition_workflow(body: CompetitionWorkflowSubmit) -> dict:
        """Submit a Best-of-N competitive solver Workflow DAG.
    
        Spawns N solver agents in **parallel** (all ``depends_on=[]``), each
        applying a different strategy to the same problem.  After all solvers
        complete, a **judge** agent reads every solver's result from the shared
        scratchpad, compares them against the scoring criterion, and writes
        ``COMPETITION_RESULT.md`` declaring the winner.
    
        Workflow topology::
    
            solver_strategy_0 ──┐
            solver_strategy_1 ──┼─→ judge
            solver_strategy_2 ──┘
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``competition/<problem[:40]>``)
        - ``task_ids``: dict with keys ``solver_{strategy}`` (one per strategy)
          and ``judge``, mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``competition_{run_id[:8]}``)
    
        Design references:
        - "Making, not Taking, the Best of N" (FusioN), arXiv:2510.00931, 2025.
        - M-A-P "Multi-Agent Parallel Test-Time Scaling", arXiv:2506.12928, 2025.
        - "When AIs Judge AIs: Agent-as-a-Judge", arXiv:2508.02994, 2025.
        - MultiAgentBench, arXiv:2503.01935, 2025.
        - DESIGN.md §10.36 (v1.1.0)
        """

    
        wm = orchestrator.get_workflow_manager()
        problem_slug = body.problem[:40].strip().replace("\n", " ")
        wf_name = f"competition/{problem_slug}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"competition_{pre_run_id[:8]}"
    
        # Shared Python snippet for reading orchestrator context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )
    
        def _write_snippet(key: str, filename: str) -> str:
            """Python3-based scratchpad write snippet (handles quotes/newlines)."""
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\"  "
            )
    
        task_ids_map: dict[str, str] = {}
        solver_task_ids: list[str] = []
    
        # Phase 1: one solver per strategy, all in parallel (depends_on=[])
        for strategy in body.strategies:
            # Sanitise strategy name for use in file/key names
            safe_strategy = strategy.strip().replace(" ", "_").replace("/", "_")
            solver_key = f"{scratchpad_prefix}_solver_{safe_strategy}"
            result_filename = f"solver_{safe_strategy}_result.md"
    
            solver_prompt = (
                f"You are a SOLVER agent competing in a Best-of-N competition.\n"
                f"\n"
                f"**Problem:**\n"
                f"{body.problem}\n"
                f"\n"
                f"**Your strategy:** {strategy}\n"
                f"**Scoring criterion:** {body.scoring_criterion}\n"
                f"\n"
                f"Your tasks:\n"
                f"1. Implement a solution to the problem using the **{strategy}** strategy.\n"
                f"   - Be concrete: write actual code or a detailed algorithmic procedure.\n"
                f"   - Optimise for the scoring criterion: {body.scoring_criterion}.\n"
                f"2. Evaluate your own solution against the scoring criterion.\n"
                f"   Produce a single numeric score (integer or float).\n"
                f"3. Write `{result_filename}` with this exact format:\n"
                f"   ```markdown\n"
                f"   # Solver Result: {strategy}\n"
                f"   ## Strategy\n"
                f"   <description of your approach>\n"
                f"   ## Solution\n"
                f"   <your full solution / code>\n"
                f"   ## Self-Evaluation\n"
                f"   <how you assessed the score>\n"
                f"   SCORE: <number>\n"
                f"   ```\n"
                f"   The `SCORE: <number>` line MUST appear at the end of the file.\n"
                f"4. Store your result in the shared scratchpad:\n"
                f"   ```python\n"
                + _write_snippet(solver_key, result_filename)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Be thorough and honest in your self-evaluation. "
                f"A higher numeric score means a better solution."
            )
    
            solver_task = await orchestrator.submit_task(
                solver_prompt,
                required_tags=body.solver_tags or None,
                depends_on=[],
            )
            role_key = f"solver_{safe_strategy}"
            task_ids_map[role_key] = solver_task.id
            solver_task_ids.append(solver_task.id)
    
        # Phase 2: judge reads all solver results and picks the winner
        solver_keys_desc = "\n".join(
            f"   - strategy '{s}': key `{scratchpad_prefix}_solver_{s.strip().replace(' ', '_').replace('/', '_')}`"
            for s in body.strategies
        )
        judge_key = f"{scratchpad_prefix}_judge"
    
        judge_prompt = (
            f"You are the JUDGE agent in a Best-of-N competitive solver workflow.\n"
            f"\n"
            f"**Problem:**\n"
            f"{body.problem}\n"
            f"\n"
            f"**Scoring criterion:** {body.scoring_criterion}\n"
            f"**Strategies competing:** {', '.join(body.strategies)}\n"
            f"\n"
            f"Your tasks:\n"
            f"1. Set up credentials:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   ```\n"
            f"2. Read ALL solver results from the shared scratchpad:\n"
            f"   Keys to read:\n"
            f"{solver_keys_desc}\n"
            f"   Use curl to read each key:\n"
            f"   `curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
            f"\n"
            f"3. For each solver result:\n"
            f"   - Extract the numeric score from the `SCORE: <number>` line.\n"
            f"   - Read the solution and self-evaluation.\n"
            f"\n"
            f"4. Write `COMPETITION_RESULT.md` with this exact format:\n"
            f"   ```markdown\n"
            f"   # Competition Result\n"
            f"   ## Problem\n"
            f"   <brief summary>\n"
            f"   ## Scoring Criterion\n"
            f"   {body.scoring_criterion}\n"
            f"   ## Scores\n"
            f"   | Strategy | Score |\n"
            f"   |----------|-------|\n"
            f"   | <strategy> | <score> |\n"
            f"   ...\n"
            f"   ## Winner\n"
            f"   WINNER: <winning_strategy_name>\n"
            f"   ## Rationale\n"
            f"   <why this strategy won>\n"
            f"   ## Runner-up\n"
            f"   <second-best strategy and its score>\n"
            f"   ```\n"
            f"   The `WINNER: <strategy>` line MUST appear in the ## Winner section.\n"
            f"\n"
            f"5. Store the competition result in the shared scratchpad:\n"
            f"   ```python\n"
            + _write_snippet(judge_key, "COMPETITION_RESULT.md")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be objective. Select the winner based solely on the numeric scores "
            f"and the scoring criterion: {body.scoring_criterion}. "
            f"If two strategies tie on score, prefer the one whose solution is "
            f"simpler and more maintainable."
        )
    
        judge_task = await orchestrator.submit_task(
            judge_prompt,
            required_tags=body.judge_tags or None,
            depends_on=solver_task_ids,
            reply_to=body.reply_to,
        )
        task_ids_map["judge"] = judge_task.id
    
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)
    
        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }
    
    @router.get(
        "/workflows",
        summary="List all workflow runs",
        dependencies=[Depends(auth)],
    )
    async def list_workflows() -> list:
        """Return a list of all submitted workflow runs and their current status.
    
        Each entry contains:
        - ``id``: workflow run UUID
        - ``name``: name given at submission
        - ``task_ids``: ordered list of global orchestrator task IDs
        - ``status``: ``"pending"`` | ``"running"`` | ``"complete"`` | ``"failed"``
        - ``created_at``: Unix timestamp of submission
        - ``completed_at``: Unix timestamp when all tasks finished, or ``null``
        - ``tasks_total``: total number of tasks in the workflow
        - ``tasks_done``: tasks that have finished (succeeded + failed)
        - ``tasks_failed``: tasks that failed
    
        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        return orchestrator.get_workflow_manager().list_all()

    # ------------------------------------------------------------------
    # GET /workflows/templates — MUST be defined BEFORE /workflows/{id}
    # to prevent FastAPI's path parameter from capturing "templates" as a
    # workflow_id value.
    # Design reference: DESIGN.md §10.103 (v1.2.28)
    # ------------------------------------------------------------------

    @router.get(
        "/workflows/templates",
        summary="List available YAML workflow templates",
        dependencies=[Depends(auth)],
    )
    async def list_workflow_templates() -> dict:
        """Return a catalogue of phase-based YAML templates available for
        ``POST /workflows/from-template``.

        Each entry includes:
        - ``template``: identifier to pass as the ``template`` field
        - ``name``: human-readable name (may contain ``{variable}`` placeholders)
        - ``description``: short description of the workflow
        - ``variables``: all declared variable names
        - ``required_variables``: variables that MUST be supplied in the request
        - ``path``: relative path within the templates directory

        Design references:
        - Argo Workflows ``WorkflowTemplate`` listing (kubectl get workflowtemplates)
        - DESIGN.md §10.103 (v1.2.28)
        """
        from tmux_orchestrator.infrastructure.workflow_loader import list_templates  # noqa: PLC0415

        try:
            templates = list_templates(_templates_dir)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to enumerate templates: {exc}",
            )
        return {"templates": templates, "templates_dir": str(_templates_dir)}

    @router.get(
        "/workflows/{workflow_id}",
        summary="Get a specific workflow run status",
        dependencies=[Depends(auth)],
    )
    async def get_workflow(workflow_id: str) -> dict:
        """Return the status and task list for *workflow_id*.
    
        Returns 404 if the workflow ID is unknown.
    
        Design reference: DESIGN.md §10.20 (v0.25.0).
        """
        result = orchestrator.get_workflow_manager().status(workflow_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {workflow_id!r} not found",
            )
        return result
    
    
    @router.get(
        "/workflows/{workflow_id}/dag",
        summary="Get workflow DAG with node and edge topology",
        dependencies=[Depends(auth)],
    )
    async def get_workflow_dag(workflow_id: str) -> dict:
        """Return the dependency graph (DAG) for *workflow_id* with per-node status.

        Each node in the graph corresponds to one orchestrator task in the
        workflow.  Each edge represents a ``depends_on`` relationship
        (``from`` must complete before ``to`` can start).

        Response format::

            {
              "workflow_id": "...",
              "name": "...",
              "status": "running",
              "nodes": [
                {
                  "task_id": "global-uuid",
                  "phase_name": "plan",
                  "status": "success",
                  "depends_on": [],
                  "dependents": ["impl-task-id"],
                  "started_at": "2026-...",
                  "finished_at": null,
                  "duration_s": null,
                  "assigned_agent": "worker-1"
                },
                ...
              ],
              "edges": [
                {"from": "plan-task-id", "to": "impl-task-id"}
              ]
            }

        ``nodes`` contains one entry per task in the workflow.
        ``edges`` duplicates the dependency topology for graph-rendering
        libraries that prefer a separate edge list (e.g. cytoscape.js, d3-dag,
        dagrejs).

        Returns 404 if the workflow ID is unknown.

        Design references:
        - AWS Glue GetDataflowGraph — separate DagNodes + DagEdges arrays
          (https://docs.aws.amazon.com/glue/latest/webapi/API_GetDataflowGraph.html)
        - ZenML DAG visualization — per-node status for progress rendering
          (https://www.zenml.io/blog/dag-visualization-vscode-extension)
        - DESIGN.md §10.90 (v1.2.14)
        """
        wm = orchestrator.get_workflow_manager()
        run = wm.get(workflow_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {workflow_id!r} not found",
            )

        # Build task_id → phase_name lookup from run.phases (if phases-mode).
        task_to_phase: dict[str, str] = {}
        for phase in run.phases:
            for tid in getattr(phase, "task_ids", []):
                task_to_phase[tid] = getattr(phase, "name", "")

        # Build nodes
        nodes: list[dict] = []
        for tid in run.task_ids:
            info = orchestrator.get_task_info(tid)
            nodes.append({
                "task_id": tid,
                "phase_name": task_to_phase.get(tid, ""),
                "status": info["status"],
                "depends_on": info["depends_on"],
                "dependents": info["dependents"],
                "started_at": info["started_at"],
                "finished_at": info["finished_at"],
                "duration_s": None,
                "assigned_agent": info["assigned_agent"],
            })

        # Build edges from stored dag_edges (topology captured at submission).
        edges = [{"from": frm, "to": to} for frm, to in run.dag_edges]

        return {
            "workflow_id": run.id,
            "name": run.name,
            "status": run.status,
            "nodes": nodes,
            "edges": edges,
        }

    @router.delete(
        "/workflows/{workflow_id}",
        summary="Cancel all tasks in a workflow",
        dependencies=[Depends(auth)],
    )
    async def delete_workflow(workflow_id: str) -> dict:
        """Cancel all tasks belonging to *workflow_id* and mark it as cancelled.
    
        Cancels each task in the workflow (queued or in-progress) and sets the
        workflow status to ``"cancelled"``.
    
        Returns:
        ``{"workflow_id": ..., "cancelled": [...task_ids...], "already_done": [...task_ids...]}``
    
        - ``cancelled``: task IDs that were successfully cancelled.
        - ``already_done``: task IDs that were not found (already completed,
          dead-lettered, or unknown).
    
        Returns 404 if *workflow_id* is unknown.
    
        Design references:
        - Apache Airflow ``dag_run.update_state("cancelled")`` — bulk cancel
        - AWS Step Functions ``StopExecution`` — cancel a running state machine
        - DESIGN.md §10.22 (v0.27.0)
        """
        result = await orchestrator.cancel_workflow(workflow_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {workflow_id!r} not found",
            )
        return result

    @router.post(
        "/workflows/spec-first",
        summary="Submit a 2-agent Spec-First (spec-writer → implementer) workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_spec_first_workflow(body: SpecFirstWorkflowSubmit) -> dict:
        """Submit a 2-agent Spec-First Workflow DAG.

        Pipeline (strictly sequential):

          1. **spec-writer**: reads the requirements, produces a formal ``SPEC.md``
             with preconditions, postconditions, invariants, type signatures, and
             acceptance criteria. Stores the spec in the shared scratchpad.
          2. **implementer**: reads ``SPEC.md`` from the scratchpad, implements the
             feature satisfying every acceptance criterion, writes tests, and runs
             them.  Stores an implementation summary in the scratchpad.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``spec-first/<topic>``)
        - ``task_ids``: dict with keys ``spec_writer``, ``implementer``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - Vasilopoulos arXiv:2602.20478 "Codified Context" (2026): formal spec docs.
        - Hou et al. "Trustworthy AI Requires Formal Methods" (2025).
        - SYSMOBENCH arXiv:2509.23130 (2025): LLM TLA+ spec generation.
        - DESIGN.md §10.44 (v1.1.8)
        """

        wm = orchestrator.get_workflow_manager()
        topic_slug = body.topic[:40].strip().replace("\n", " ")
        wf_name = f"spec-first/{topic_slug}"

        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"specfirst_{pre_run_id[:8]}"

        # Shared bash snippets for reading context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _scratchpad_key(suffix: str) -> str:
            return f"{scratchpad_prefix}_{suffix}"

        def _write_snippet(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        spec_key = _scratchpad_key("spec")
        impl_key = _scratchpad_key("impl")

        # --- Spec-writer prompt ---
        spec_writer_prompt = (
            f"You are the SPEC-WRITER agent in a Spec-First development workflow.\n"
            f"\n"
            f"**Topic:** {body.topic}\n"
            f"\n"
            f"**Requirements:**\n"
            f"{body.requirements}\n"
            f"\n"
            f"Your task is to write a formal specification document (SPEC.md) that "
            f"the implementer will use to build the feature.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `SPEC.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Specification: {body.topic}\n"
            f"   ## Context\n"
            f"   ## Scope\n"
            f"   - IN SCOPE: ...\n"
            f"   - OUT OF SCOPE: ...\n"
            f"   ## Type Signatures\n"
            f"   ## Preconditions\n"
            f"   - PRE-1: ...\n"
            f"   ## Postconditions\n"
            f"   - POST-1: ...\n"
            f"   ## Invariants\n"
            f"   - INV-1: ...\n"
            f"   ## Functional Requirements\n"
            f"   ## Acceptance Criteria\n"
            f"   - AC-1: Given ... when ... then ...\n"
            f"   ## Edge Cases\n"
            f"   - EDGE-1: ...\n"
            f"   ## Glossary\n"
            f"   ```\n"
            f"2. Store SPEC.md in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat SPEC.md)\n"
            + _write_snippet(spec_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be precise and unambiguous — the implementer must be able to build from "
            f"your spec alone without consulting you. Max 600 words."
        )

        # --- Implementer prompt ---
        implementer_prompt = (
            f"You are the IMPLEMENTER agent in a Spec-First development workflow.\n"
            f"\n"
            f"**Topic:** {body.topic}\n"
            f"\n"
            f"Your task is to implement the feature strictly following the spec-writer's "
            f"SPEC.md. Write code that satisfies every acceptance criterion.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the spec-writer's SPEC.md from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(spec_key, "SPEC")
            + f"   echo \"$SPEC\" > SPEC.md\n"
            f"   cat SPEC.md\n"
            f"   ```\n"
            f"2. Implement the feature exactly as specified in SPEC.md:\n"
            f"   - Write the implementation file(s) in your working directory.\n"
            f"   - Write tests in `test_*.py` covering every acceptance criterion.\n"
            f"3. Run the tests:\n"
            f"   ```bash\n"
            f"   python -m pytest test_*.py -v 2>&1 | tee test_output.txt || true\n"
            f"   ```\n"
            f"4. Write `impl_summary.md` with:\n"
            f"   ```markdown\n"
            f"   # Implementation Summary: {body.topic}\n"
            f"   ## Files Created\n"
            f"   ## Acceptance Criteria Status\n"
            f"   - AC-1: PASS/FAIL\n"
            f"   ## Test Results\n"
            f"   ## Notes\n"
            f"   ```\n"
            f"5. Store the summary in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat impl_summary.md)\n"
            + _write_snippet(impl_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Follow the spec faithfully. Do not add features not listed in SPEC.md. "
            f"If a requirement is ambiguous, implement the simplest interpretation and "
            f"document it in Notes."
        )

        # Submit tasks in pipeline order
        spec_writer_task = await orchestrator.submit_task(
            spec_writer_prompt,
            required_tags=body.spec_tags or None,
            depends_on=[],
        )

        implementer_task = await orchestrator.submit_task(
            implementer_prompt,
            required_tags=body.impl_tags or None,
            depends_on=[spec_writer_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "spec_writer": spec_writer_task.id,
            "implementer": implementer_task.id,
        }

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @router.post(
        "/workflows/mob-review",
        summary="Submit an N-reviewer + synthesizer Mob Code Review workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_mob_review_workflow(body: MobReviewWorkflowSubmit) -> dict:
        """Submit a Mob Code Review Workflow DAG.

        Spawns N reviewer agents in **parallel** (all ``depends_on=[]``), each
        examining the same code from a distinct quality dimension (e.g. security,
        performance, maintainability, testing).  After all reviewers complete, a
        **synthesizer** agent reads every aspect review from the shared scratchpad
        and produces a unified ``MOB_REVIEW.md`` report.

        Workflow topology::

            reviewer_security       ──┐
            reviewer_performance    ──┼─→ synthesizer
            reviewer_maintainability──┤
            reviewer_testing        ──┘

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``mob-review/<language>``)
        - ``task_ids``: dict with keys ``reviewer_{aspect}`` (one per aspect)
          and ``synthesizer``, mapping to global task IDs
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``mobreview_{run_id[:8]}``)

        Design references:
        - ChatEval (arXiv:2308.07201, ICLR 2024): unique reviewer personas are
          essential — homogeneous role prompts degrade multi-agent evaluation quality.
        - Agent-as-a-Judge (arXiv:2508.02994, 2025): aggregating independent
          judgements reduces variance akin to a voting committee.
        - Code in Harmony (OpenReview 2025): parallel multi-agent evaluation with
          orthogonal quality dimensions outperforms sequential review.
        - DESIGN.md §10.52 (v1.1.20)
        """
        wm = orchestrator.get_workflow_manager()
        lang_slug = body.language[:30].strip().replace(" ", "_")
        wf_name = f"mob-review/{lang_slug}"

        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"mobreview_{pre_run_id[:8]}"

        # Shared snippet for reading orchestrator context + API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str, filename: str) -> str:
            """Python3-based scratchpad write snippet (handles quotes/newlines)."""
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\"  "
            )

        task_ids_map: dict[str, str] = {}
        reviewer_task_ids: list[str] = []

        # ---------- Phase 1: N parallel reviewers ----------------------------
        for aspect in body.aspects:
            safe_aspect = aspect.strip().lower().replace(" ", "_").replace("/", "_")
            review_key = f"{scratchpad_prefix}_review_{safe_aspect}"
            review_filename = f"review_{safe_aspect}.md"

            # Aspect-specific guidance to ensure persona diversity (ChatEval insight)
            aspect_guidance: dict[str, str] = {
                "security": (
                    "Focus EXCLUSIVELY on security. Look for: injection vulnerabilities "
                    "(SQL/command/template), authentication/authorisation flaws, insecure "
                    "data handling, missing input validation, hard-coded credentials, "
                    "unsafe deserialization, information disclosure, and OWASP Top 10 issues."
                ),
                "performance": (
                    "Focus EXCLUSIVELY on performance. Look for: O(n²) or worse algorithms, "
                    "unnecessary database queries inside loops (N+1 problem), missing "
                    "caching opportunities, synchronous I/O in async paths, memory leaks, "
                    "large data structure copies, and unoptimised string operations."
                ),
                "maintainability": (
                    "Focus EXCLUSIVELY on maintainability. Look for: code duplication (DRY "
                    "violations), overly complex functions (high cyclomatic complexity), poor "
                    "naming, missing/inadequate documentation, tight coupling, God objects, "
                    "magic numbers, long parameter lists, and missing type annotations."
                ),
                "testing": (
                    "Focus EXCLUSIVELY on testability and test coverage. Look for: untestable "
                    "code patterns (hidden dependencies, no dependency injection), missing "
                    "edge case tests, lack of error-path coverage, flaky-prone patterns "
                    "(time-dependent, order-dependent tests), missing mocks for external "
                    "services, and inadequate assertions."
                ),
            }
            default_guidance = (
                f"Focus EXCLUSIVELY on **{aspect}** quality. Identify specific issues, "
                f"explain their impact, and suggest concrete improvements."
            )
            guidance = aspect_guidance.get(safe_aspect, default_guidance)

            reviewer_prompt = (
                f"You are a specialist CODE REVIEWER participating in a Mob Code Review.\n"
                f"Your assigned quality dimension: **{aspect.upper()}**\n"
                f"\n"
                f"{guidance}\n"
                f"\n"
                f"**Language/Framework:** {body.language}\n"
                f"\n"
                f"**Code to Review:**\n"
                f"```{body.language.lower().split()[0]}\n"
                f"{body.code}\n"
                f"```\n"
                f"\n"
                f"Your tasks:\n"
                f"1. Analyse the code from the **{aspect}** perspective ONLY.\n"
                f"   Do not comment on other dimensions — those are handled by specialist reviewers.\n"
                f"2. Write `{review_filename}` with this exact format:\n"
                f"   ```markdown\n"
                f"   # {aspect.title()} Review\n"
                f"   ## Summary\n"
                f"   <1-3 sentence overall assessment>\n"
                f"   ## Severity: [CRITICAL|HIGH|MEDIUM|LOW|NONE]\n"
                f"   ## Findings\n"
                f"   ### Finding 1: <title>\n"
                f"   - **Location:** <line numbers or function name>\n"
                f"   - **Issue:** <description>\n"
                f"   - **Impact:** <what goes wrong if not fixed>\n"
                f"   - **Fix:** <concrete recommendation>\n"
                f"   ...(repeat for each finding)\n"
                f"   ## Positive Aspects\n"
                f"   <what the code does well from the {aspect} perspective>\n"
                f"   ```\n"
                f"3. Store your review in the shared scratchpad:\n"
                f"   ```python\n"
                + _write_snippet(review_key, review_filename)
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Be precise and concrete. Use line numbers when possible. "
                f"Rate severity honestly — do not escalate minor issues."
            )

            # Auto-generate per-aspect required_tags if reviewer_tags not explicitly set.
            # Pattern: ["mob_reviewer", "{aspect}"] ensures each task is routed to the
            # agent tagged with that specific aspect (ChatEval insight: unique personas
            # per reviewer; MasRouter ACL 2025: capability-tag matching).
            # If caller explicitly passes reviewer_tags, those override the auto-generated ones.
            if body.reviewer_tags:
                aspect_required_tags: list[str] | None = list(body.reviewer_tags)
            else:
                aspect_required_tags = ["mob_reviewer", safe_aspect]

            reviewer_task = await orchestrator.submit_task(
                reviewer_prompt,
                required_tags=aspect_required_tags,
                depends_on=[],
            )
            task_ids_map[f"reviewer_{safe_aspect}"] = reviewer_task.id
            reviewer_task_ids.append(reviewer_task.id)

        # ---------- Phase 2: synthesizer -------------------------------------
        review_keys_list = [
            f"{scratchpad_prefix}_review_{a.strip().lower().replace(' ', '_').replace('/', '_')}"
            for a in body.aspects
        ]
        review_keys_desc = "\n".join(f"   - `{k}`" for k in review_keys_list)
        synthesis_key = f"{scratchpad_prefix}_synthesis"

        synthesizer_prompt = (
            f"You are the SYNTHESIZER agent in a Mob Code Review.\n"
            f"\n"
            f"Specialist reviewers have independently examined the following code:\n"
            f"```{body.language.lower().split()[0]}\n"
            f"{body.code}\n"
            f"```\n"
            f"\n"
            f"Your task:\n"
            f"1. Read your orchestrator context and API credentials:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   ```\n"
            f"2. Read ALL specialist reviews from the shared scratchpad:\n"
            f"   Keys to read:\n"
            f"{review_keys_desc}\n"
            f"   Use curl for each: "
            f"`curl -s -H \"X-API-Key: $API_KEY\" \"$WEB_BASE_URL/scratchpad/<key>\"`\n"
            f"\n"
            f"3. Synthesize ALL reviews into `MOB_REVIEW.md` with this exact format:\n"
            f"   ```markdown\n"
            f"   # Mob Code Review — {body.language}\n"
            f"   ## Executive Summary\n"
            f"   <2-4 sentence overall assessment integrating all dimensions>\n"
            f"   ## Overall Severity: [CRITICAL|HIGH|MEDIUM|LOW]\n"
            f"   (take the maximum severity across all aspect reviews)\n"
            f"   ## Critical Findings\n"
            f"   <list findings rated CRITICAL or HIGH from any reviewer, with aspect label>\n"
            f"   ## Medium/Low Findings\n"
            f"   <list findings rated MEDIUM or LOW, with aspect label>\n"
            f"   ## Dimension Summary\n"
            f"   | Dimension | Severity | Key Finding |\n"
            f"   |-----------|----------|-------------|\n"
            f"   | <aspect> | <severity> | <one-line summary> |\n"
            f"   ...\n"
            f"   ## Recommended Actions\n"
            f"   1. <highest-priority fix>\n"
            f"   2. <second-priority fix>\n"
            f"   ...\n"
            f"   ## Positive Aspects\n"
            f"   <what the code does well overall>\n"
            f"   ```\n"
            f"4. Store `MOB_REVIEW.md` in the shared scratchpad:\n"
            f"   ```python\n"
            + _write_snippet(synthesis_key, "MOB_REVIEW.md")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be balanced. Acknowledge strengths as well as weaknesses. "
            f"Prioritise findings by severity and actionability. "
            f"The MOB_REVIEW.md should be a self-contained document that a developer "
            f"can act on without needing to read the individual aspect reviews."
        )

        synthesizer_task = await orchestrator.submit_task(
            synthesizer_prompt,
            required_tags=body.synthesizer_tags or None,
            depends_on=reviewer_task_ids,
            reply_to=body.reply_to,
        )
        task_ids_map["synthesizer"] = synthesizer_task.id

        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    # -----------------------------------------------------------------------
    # POST /workflows/iterative-review
    # -----------------------------------------------------------------------

    @router.post(
        "/workflows/iterative-review",
        summary="Submit an implementer→reviewer→revisor Iterative Review workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_iterative_review_workflow(
        body: IterativeReviewWorkflowSubmit,
    ) -> dict:
        """Submit an Iterative Review Workflow DAG.

        Spawns 3 sequential agents: an **implementer** writes the initial code,
        a **reviewer** critiques it (Self-Refine FEEDBACK step), and a **revisor**
        produces an improved version (Self-Refine REFINE step).

        Workflow topology (sequential via depends_on)::

            implementer → reviewer → revisor

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``iterative-review/<task_slug>``)
        - ``task_ids``: dict with keys ``implementer``, ``reviewer``, ``revisor``
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``iterrev_{run_id[:8]}``)

        Design references:
        - Self-Refine (arXiv:2303.17651, NeurIPS 2023): FEEDBACK→REFINE iterative
          loop improves code quality ~20% on average. reviewer=FEEDBACK, revisor=REFINE.
        - MAR: Multi-Agent Reflexion (arXiv:2512.20845, 2025): cross-agent feedback
          outperforms single-agent self-feedback.
        - RevAgent (arXiv:2511.00517, 2025): multi-stage code review pipeline.
        - DESIGN.md §10.53 (v1.1.21)
        """
        wm = orchestrator.get_workflow_manager()
        task_slug = body.task[:40].strip().replace(" ", "_").replace("/", "_")
        wf_name = f"iterative-review/{task_slug}"

        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"iterrev_{pre_run_id[:8]}"

        impl_key = f"{scratchpad_prefix}_implementation"
        review_key = f"{scratchpad_prefix}_review"
        revised_key = f"{scratchpad_prefix}_revised"

        # Shared read snippet (reads value from scratchpad key)
        def _read_snippet(key: str) -> str:
            return (
                f"python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"resp = urllib.request.urlopen(req, timeout=15)\n"
                f"data = json.loads(resp.read())\n"
                f"print(data.get('value', data))\n"
                f"\""
            )

        def _write_snippet_ir(key: str, filename: str) -> str:
            return (
                f"python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\""
            )

        lang = body.language
        lang_ext = lang.lower().split()[0]
        impl_file = f"implementation.{lang_ext}"
        review_file = "review.md"
        revised_file = f"revised.{lang_ext}"

        # ---------- Phase 1: implementer ------------------------------------
        implementer_prompt = (
            f"You are the IMPLEMENTER in an Iterative Review pipeline.\n"
            f"\n"
            f"**Task:** {body.task}\n"
            f"**Language/Framework:** {lang}\n"
            f"\n"
            f"Your job:\n"
            f"1. Implement the task above in {lang}. Write clean, correct, idiomatic code.\n"
            f"   Save your implementation to `{impl_file}`.\n"
            f"2. Store your implementation in the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_write_snippet_ir(impl_key, impl_file)}\n"
            f"   ```\n"
            f"\n"
            f"Write complete, working code. Include docstrings and type hints where appropriate.\n"
            f"A reviewer will critique your implementation, and a revisor will improve it.\n"
        )

        # ---------- Phase 2: reviewer (Self-Refine FEEDBACK step) -----------
        reviewer_prompt = (
            f"You are the REVIEWER in an Iterative Review pipeline (Self-Refine FEEDBACK step).\n"
            f"\n"
            f"**Task context:** {body.task}\n"
            f"**Language/Framework:** {lang}\n"
            f"\n"
            f"Your job:\n"
            f"1. Read the implementer's code from the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_read_snippet(impl_key)}\n"
            f"   ```\n"
            f"2. Write a structured review to `{review_file}` with this exact format:\n"
            f"   ```markdown\n"
            f"   # Code Review — FEEDBACK\n"
            f"   ## Overall Assessment\n"
            f"   <2-3 sentence summary of implementation quality>\n"
            f"   ## Issues (ordered by severity)\n"
            f"   ### Issue 1: <title>\n"
            f"   - **Severity:** [CRITICAL|HIGH|MEDIUM|LOW]\n"
            f"   - **Location:** <line/function name>\n"
            f"   - **Problem:** <what is wrong>\n"
            f"   - **Fix:** <concrete improvement instruction>\n"
            f"   ...\n"
            f"   ## Positive Aspects\n"
            f"   <what the implementation does well>\n"
            f"   ## Summary of Required Changes\n"
            f"   <numbered list of must-fix items for the revisor>\n"
            f"   ```\n"
            f"3. Store your review in the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_write_snippet_ir(review_key, review_file)}\n"
            f"   ```\n"
            f"\n"
            f"Be precise, actionable, and constructive. "
            f"Focus on correctness, clarity, and {lang} best practices.\n"
        )

        # ---------- Phase 3: revisor (Self-Refine REFINE step) --------------
        revisor_prompt = (
            f"You are the REVISOR in an Iterative Review pipeline (Self-Refine REFINE step).\n"
            f"\n"
            f"**Task context:** {body.task}\n"
            f"**Language/Framework:** {lang}\n"
            f"\n"
            f"Your job:\n"
            f"1. Read the original implementation from the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_read_snippet(impl_key)}\n"
            f"   ```\n"
            f"2. Read the reviewer's feedback from the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_read_snippet(review_key)}\n"
            f"   ```\n"
            f"3. Produce an improved version that addresses ALL issues raised by the reviewer.\n"
            f"   Save the revised code to `{revised_file}`.\n"
            f"4. Store your revised implementation in the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_write_snippet_ir(revised_key, revised_file)}\n"
            f"   ```\n"
            f"\n"
            f"Apply EVERY fix listed in the reviewer's 'Summary of Required Changes'.\n"
            f"The revised code should be strictly better than the original on all dimensions.\n"
        )

        # Auto-generate required_tags per role unless explicitly overridden
        impl_req_tags: list[str] | None = (
            list(body.implementer_tags) if body.implementer_tags else ["iterative_implementer"]
        )
        rev_req_tags: list[str] | None = (
            list(body.reviewer_tags) if body.reviewer_tags else ["iterative_reviewer"]
        )
        revisor_req_tags: list[str] | None = (
            list(body.revisor_tags) if body.revisor_tags else ["iterative_revisor"]
        )

        implementer_task = await orchestrator.submit_task(
            implementer_prompt,
            required_tags=impl_req_tags,
            depends_on=[],
        )
        reviewer_task = await orchestrator.submit_task(
            reviewer_prompt,
            required_tags=rev_req_tags,
            depends_on=[implementer_task.id],
        )
        revisor_task = await orchestrator.submit_task(
            revisor_prompt,
            required_tags=revisor_req_tags,
            depends_on=[reviewer_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "implementer": implementer_task.id,
            "reviewer": reviewer_task.id,
            "revisor": revisor_task.id,
        }
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    # POST /workflows/spec-first-tdd
    # -----------------------------------------------------------------------

    @router.post(
        "/workflows/spec-first-tdd",
        summary="Submit a spec-writer→implementer→tester Spec-First TDD workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_spec_first_tdd_workflow(
        body: SpecFirstTddWorkflowSubmit,
    ) -> dict:
        """Submit a Spec-First TDD Workflow DAG.

        Spawns 3 sequential agents: a **spec-writer** produces a formal SPEC.md,
        an **implementer** implements the feature satisfying the spec, and a
        **tester** writes and runs a pytest test suite validating every acceptance
        criterion.

        Workflow topology (sequential via depends_on)::

            spec-writer → implementer → tester

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``spec-first-tdd/<topic>``)
        - ``task_ids``: dict with keys ``spec_writer``, ``implementer``, ``tester``
        - ``scratchpad_prefix``: scratchpad namespace for this run
          (e.g. ``sftdd_{run_id[:8]}``)

        Design references:
        - Vasilopoulos arXiv:2602.20478 "Codified Context" (2026): formal specs.
        - Beck "Test-Driven Development by Example" (2003): TDD cycle.
        - AgentCoder: programmer → test_designer → test_executor pipeline.
        - DESIGN.md §10.54 (v1.1.22)
        """
        wm = orchestrator.get_workflow_manager()
        topic_slug = body.topic[:40].strip().replace(" ", "_").replace("/", "_")
        wf_name = f"spec-first-tdd/{topic_slug}"

        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"sftdd_{pre_run_id[:8]}"

        spec_key = f"{scratchpad_prefix}_spec"
        impl_key = f"{scratchpad_prefix}_impl"
        test_result_key = f"{scratchpad_prefix}_test_result"

        lang = body.language
        lang_ext = lang.lower().split()[0]
        impl_file = f"implementation.{lang_ext}"
        test_file = f"test_implementation.{lang_ext}"

        def _read_snippet(key: str) -> str:
            return (
                f"python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"resp = urllib.request.urlopen(req, timeout=15)\n"
                f"data = json.loads(resp.read())\n"
                f"print(data.get('value', data))\n"
                f"\""
            )

        def _write_snippet(key: str, filename: str) -> str:
            return (
                f"python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\""
            )

        # --- Phase 1: spec-writer -------------------------------------------
        spec_writer_prompt = (
            f"You are the SPEC-WRITER agent in a Spec-First TDD workflow.\n"
            f"\n"
            f"**Topic:** {body.topic}\n"
            f"**Language/Framework:** {lang}\n"
            f"\n"
            f"**Requirements:**\n"
            f"{body.requirements}\n"
            f"\n"
            f"Your task is to write a formal specification document SPEC.md.\n"
            f"\n"
            f"Steps:\n"
            f"1. Write `SPEC.md` with these sections:\n"
            f"   ```markdown\n"
            f"   # Specification: {body.topic}\n"
            f"   ## Context\n"
            f"   <brief context>\n"
            f"   ## Scope\n"
            f"   <what is in/out of scope>\n"
            f"   ## Type Signatures\n"
            f"   <function/class signatures with types>\n"
            f"   ## Preconditions\n"
            f"   <what must be true before each operation>\n"
            f"   ## Postconditions\n"
            f"   <what must be true after each operation>\n"
            f"   ## Acceptance Criteria\n"
            f"   - AC-1: <testable criterion>\n"
            f"   - AC-2: <testable criterion>\n"
            f"   ...\n"
            f"   ## Edge Cases\n"
            f"   <error conditions, boundary values>\n"
            f"   ```\n"
            f"2. Store SPEC.md in the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_write_snippet(spec_key, 'SPEC.md')}\n"
            f"   ```\n"
            f"\n"
            f"Write a complete, precise specification. The implementer and tester "
            f"will use it as their sole source of truth.\n"
        )

        # --- Phase 2: implementer -------------------------------------------
        implementer_prompt = (
            f"You are the IMPLEMENTER agent in a Spec-First TDD workflow.\n"
            f"\n"
            f"**Topic:** {body.topic}\n"
            f"**Language/Framework:** {lang}\n"
            f"\n"
            f"Your task is to implement the feature as specified in SPEC.md.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the formal specification from the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_read_snippet(spec_key)}\n"
            f"   ```\n"
            f"2. Implement the feature satisfying EVERY acceptance criterion in SPEC.md.\n"
            f"   Save your implementation to `{impl_file}`.\n"
            f"   Include docstrings, type hints, and follow {lang} best practices.\n"
            f"3. Store your implementation in the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_write_snippet(impl_key, impl_file)}\n"
            f"   ```\n"
            f"\n"
            f"Write complete, correct code. The tester will independently verify "
            f"every acceptance criterion against your implementation.\n"
        )

        # --- Phase 3: tester ------------------------------------------------
        tester_prompt = (
            f"You are the TESTER agent in a Spec-First TDD workflow.\n"
            f"\n"
            f"**Topic:** {body.topic}\n"
            f"**Language/Framework:** {lang}\n"
            f"\n"
            f"Your task is to write and run a test suite that validates every "
            f"acceptance criterion in SPEC.md.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the formal specification:\n"
            f"   ```python\n"
            f"   {_read_snippet(spec_key)}\n"
            f"   ```\n"
            f"2. Read the implementation:\n"
            f"   ```python\n"
            f"   {_read_snippet(impl_key)}\n"
            f"   ```\n"
            f"3. Save the implementation to `{impl_file}` so you can import it.\n"
            f"4. Write a pytest test file `{test_file}` with one test function per\n"
            f"   acceptance criterion (AC-N). Also test edge cases from SPEC.md.\n"
            f"5. Run the tests: `python -m pytest {test_file} -v 2>&1 | tee test_output.txt`\n"
            f"6. Write a summary to `test_results.md`:\n"
            f"   ```markdown\n"
            f"   # Test Results — {body.topic}\n"
            f"   ## Verdict: PASS / FAIL\n"
            f"   ## Tests Run: N passed, M failed\n"
            f"   ## AC Coverage\n"
            f"   - AC-1: PASS/FAIL\n"
            f"   ...\n"
            f"   ## Notes\n"
            f"   ```\n"
            f"7. Store the test results in the shared scratchpad:\n"
            f"   ```python\n"
            f"   {_write_snippet(test_result_key, 'test_results.md')}\n"
            f"   ```\n"
            f"\n"
            f"Test every acceptance criterion. If tests fail, note which AC failed "
            f"and why. Do NOT modify the implementation — only test it.\n"
        )

        # Auto-generate required_tags per role unless explicitly overridden
        spec_req_tags = list(body.spec_tags) if body.spec_tags else ["sftdd_spec"]
        impl_req_tags = list(body.impl_tags) if body.impl_tags else ["sftdd_impl"]
        tester_req_tags = list(body.tester_tags) if body.tester_tags else ["sftdd_tester"]

        spec_writer_task = await orchestrator.submit_task(
            spec_writer_prompt,
            required_tags=spec_req_tags,
            depends_on=[],
        )
        implementer_task = await orchestrator.submit_task(
            implementer_prompt,
            required_tags=impl_req_tags,
            depends_on=[spec_writer_task.id],
        )
        tester_task = await orchestrator.submit_task(
            tester_prompt,
            required_tags=tester_req_tags,
            depends_on=[implementer_task.id],
            reply_to=body.reply_to,
        )

        task_ids_map = {
            "spec_writer": spec_writer_task.id,
            "implementer": implementer_task.id,
            "tester": tester_task.id,
        }
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @router.post(
        "/workflows/agentmesh",
        summary="Submit an AgentMesh 4-role development pipeline (Planner→Coder→Debugger→Reviewer)",
        dependencies=[Depends(auth)],
    )
    async def submit_agentmesh_workflow(body: AgentmeshWorkflowSubmit) -> dict:
        """Submit a 4-agent sequential AgentMesh development pipeline Workflow DAG.

        Pipeline (strictly sequential):

          1. **planner**: reads the feature request, produces a detailed implementation
             plan (data structures, algorithm, test cases, edge cases), and stores it
             in ``{scratchpad_prefix}_plan``.
          2. **coder**: reads the plan from the scratchpad, writes a complete
             implementation in ``language``, and stores it in ``{scratchpad_prefix}_code``.
          3. **debugger**: reads the implementation, identifies bugs and edge-case
             failures, writes a corrected/improved version, and stores it in
             ``{scratchpad_prefix}_debugged``.
          4. **reviewer**: reads all previous scratchpad outputs (plan, code, debugged),
             writes a structured code review with a star rating (1–5) and actionable
             recommendations to ``{scratchpad_prefix}_review``.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``agentmesh/<feature_request[:40]>``)
        - ``task_ids``: dict with keys ``planner``, ``coder``, ``debugger``, ``reviewer``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - Elias, "AgentMesh: A Cooperative Multi-Agent Generative AI Framework
          for Software Development Automation", arXiv:2507.19902 (2025):
          Planner/Coder/Debugger/Reviewer 4-role pipeline.
        - ACM TOSEM, "LLM-Based Multi-Agent Systems for Software Engineering" (2025),
          https://dl.acm.org/doi/10.1145/3712003: Pipeline pattern with deterministic
          handoff between specialised roles.
        - DESIGN.md §10.73 (v1.1.41)
        """

        wm = orchestrator.get_workflow_manager()
        feature_slug = body.feature_request[:40].replace(" ", "_").replace("/", "_")
        wf_name = f"agentmesh/{feature_slug}"

        pre_run_id = str(uuid.uuid4())
        # If caller supplied a custom prefix other than the default, use it as-is;
        # otherwise auto-generate a collision-safe prefix.
        if body.scratchpad_prefix and body.scratchpad_prefix != "agentmesh":
            scratchpad_prefix = body.scratchpad_prefix
        else:
            scratchpad_prefix = f"agentmesh_{pre_run_id[:8]}"

        lang = body.language

        # Scratchpad key helpers (no slashes — REST route uses plain {key})
        plan_key = f"{scratchpad_prefix}_plan"
        code_key = f"{scratchpad_prefix}_code"
        debugged_key = f"{scratchpad_prefix}_debugged"
        review_key = f"{scratchpad_prefix}_review"

        # Shared bash snippet for reading orchestrator context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str, filename: str) -> str:
            """Python3-based snippet to safely write a file's content to scratchpad."""
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\"  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            """Bash snippet that reads scratchpad key into $varname."""
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        # ----------------------------------------------------------------
        # Phase 1: PLANNER
        # ----------------------------------------------------------------
        planner_prompt = (
            f"You are the PLANNER agent in an AgentMesh 4-role software development pipeline.\n"
            f"\n"
            f"**Feature Request:** {body.feature_request}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role (PLAN phase):\n"
            f"Produce a detailed, structured implementation plan that the Coder agent can\n"
            f"follow without ambiguity.\n"
            f"\n"
            f"Steps:\n"
            f"1. Analyse the feature request carefully.\n"
            f"2. Write a file `plan.md` in your working directory containing:\n"
            f"   - **Overview**: 2-3 sentence summary of the approach\n"
            f"   - **Data Structures**: what types/classes/data structures are needed\n"
            f"   - **Algorithm**: step-by-step description of the algorithm\n"
            f"   - **Function Signatures**: exact function/method signatures with types\n"
            f"   - **Test Cases**: at least 5 concrete test cases (input → expected output)\n"
            f"   - **Edge Cases**: empty input, boundary values, error conditions\n"
            f"   - **Implementation Notes**: any gotchas or important details\n"
            f"3. Store the plan in the shared scratchpad:\n"
            f"   ```python\n"
            + _write_snippet(plan_key, "plan.md")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be specific and complete. The Coder agent will implement EXACTLY what you specify.\n"
            f"Do NOT write any {lang} code — only the plan."
        )

        # ----------------------------------------------------------------
        # Phase 2: CODER
        # ----------------------------------------------------------------
        coder_prompt = (
            f"You are the CODER agent in an AgentMesh 4-role software development pipeline.\n"
            f"\n"
            f"**Feature Request:** {body.feature_request}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role (CODE phase):\n"
            f"Read the planner's implementation plan from the scratchpad and write the\n"
            f"complete implementation.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the implementation plan from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(plan_key, "PLAN")
            + f"   echo \"Plan retrieved.\"\n"
            f"   ```\n"
            f"2. Write the complete {lang} implementation to `implementation.py` in your\n"
            f"   working directory. Follow the plan EXACTLY:\n"
            f"   - Implement every function/class specified in the plan\n"
            f"   - Handle all edge cases from the plan\n"
            f"   - Add docstrings explaining what each function does\n"
            f"   - Include type annotations\n"
            f"3. Verify the implementation compiles/parses: `python3 -m py_compile implementation.py`\n"
            f"4. Store the implementation in the shared scratchpad:\n"
            f"   ```python\n"
            + _write_snippet(code_key, "implementation.py")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Write clean, working {lang} code. The Debugger will test and fix it next."
        )

        # ----------------------------------------------------------------
        # Phase 3: DEBUGGER
        # ----------------------------------------------------------------
        debugger_prompt = (
            f"You are the DEBUGGER agent in an AgentMesh 4-role software development pipeline.\n"
            f"\n"
            f"**Feature Request:** {body.feature_request}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role (DEBUG phase):\n"
            f"Read the coder's implementation, identify bugs and issues, and produce a\n"
            f"corrected version.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the implementation from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(code_key, "CODE")
            + f"   echo \"$CODE\" > implementation.py\n"
            f"   ```\n"
            f"2. Also read the original plan for reference:\n"
            f"   ```bash\n"
            + _read_snippet(plan_key, "PLAN")
            + f"   ```\n"
            f"3. Analyse the implementation carefully:\n"
            f"   - Test against the test cases from the plan (write a test script\n"
            f"     `test_debug.py` and run it with `python3 test_debug.py`)\n"
            f"   - Check for: off-by-one errors, missing edge cases, type errors,\n"
            f"     incorrect logic, missing imports\n"
            f"4. Write the corrected implementation to `implementation_fixed.py`.\n"
            f"   If no bugs are found, copy `implementation.py` unchanged but add a\n"
            f"   comment at the top: `# Debugger review: no bugs found`.\n"
            f"5. Store the fixed implementation in the shared scratchpad:\n"
            f"   ```python\n"
            + _write_snippet(debugged_key, "implementation_fixed.py")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be thorough. Run actual tests. The Reviewer will read your fixed version."
        )

        # ----------------------------------------------------------------
        # Phase 4: REVIEWER
        # ----------------------------------------------------------------
        reviewer_prompt = (
            f"You are the REVIEWER agent in an AgentMesh 4-role software development pipeline.\n"
            f"\n"
            f"**Feature Request:** {body.feature_request}\n"
            f"**Language:** {lang}\n"
            f"\n"
            f"Your role (REVIEW phase):\n"
            f"Read all previous pipeline outputs and write a comprehensive code review.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read all pipeline artifacts from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(plan_key, "PLAN")
            + _read_snippet(code_key, "CODE")
            + _read_snippet(debugged_key, "DEBUGGED")
            + f"   ```\n"
            f"2. Write `review.md` in your working directory containing:\n"
            f"   - **Star Rating**: ★ (1-5 stars) with brief justification\n"
            f"   - **Summary**: 2-3 sentence overall assessment\n"
            f"   - **Correctness**: does the implementation match the plan? any logic errors?\n"
            f"   - **Code Quality**: readability, naming, docstrings, type annotations\n"
            f"   - **Edge Case Handling**: are all edge cases from the plan handled?\n"
            f"   - **Performance**: any obvious inefficiencies?\n"
            f"   - **Security**: any obvious security issues (e.g., injection, overflow)?\n"
            f"   - **Recommendations**: top 3-5 actionable improvements\n"
            f"   - **Final Verdict**: APPROVED / APPROVED_WITH_COMMENTS / NEEDS_REVISION\n"
            f"3. Store the review in the shared scratchpad:\n"
            f"   ```python\n"
            + _write_snippet(review_key, "review.md")
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be thorough and constructive. Your review is the final quality gate."
        )

        # ----------------------------------------------------------------
        # Submit the 4-step sequential DAG
        # ----------------------------------------------------------------
        planner_task = await orchestrator.submit_task(
            planner_prompt,
            required_tags=["agentmesh_planner"],
            depends_on=[],
            timeout=body.agent_timeout,
        )

        coder_task = await orchestrator.submit_task(
            coder_prompt,
            required_tags=["agentmesh_coder"],
            depends_on=[planner_task.id],
            timeout=body.agent_timeout,
        )

        debugger_task = await orchestrator.submit_task(
            debugger_prompt,
            required_tags=["agentmesh_debugger"],
            depends_on=[coder_task.id],
            timeout=body.agent_timeout,
        )

        reviewer_task = await orchestrator.submit_task(
            reviewer_prompt,
            required_tags=["agentmesh_reviewer"],
            depends_on=[debugger_task.id],
            reply_to=body.reply_to,
            timeout=body.agent_timeout,
        )

        task_ids_map = {
            "planner": planner_task.id,
            "coder": coder_task.id,
            "debugger": debugger_task.id,
            "reviewer": reviewer_task.id,
        }
        all_task_ids = list(task_ids_map.values())
        run = wm.submit(name=wf_name, task_ids=all_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids_map,
            "scratchpad_prefix": scratchpad_prefix,
        }

    @router.post(
        "/workflows/pdca",
        summary="Submit a PDCA (Plan-Do-Check-Act) iterative improvement workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_pdca_workflow(body: PdcaWorkflowSubmit) -> dict:
        """Submit a PDCA iterative improvement workflow.

        Builds and submits a loop-based workflow that executes Plan → Do → Check
        → Act phases iteratively until a quality condition is met or the maximum
        number of cycles is reached.

        Each cycle:
        1. **plan** (``required_tags: planner_tags``): Produces a plan for this
           iteration.  Writes plan to scratchpad key
           ``{prefix}_plan_iter{N}``.
        2. **do** (``required_tags: doer_tags``): Implements the plan.  Reads
           ``{prefix}_plan_iter{N}``, writes output to ``{prefix}_do_iter{N}``.
        3. **check** (``required_tags: checker_tags``): Evaluates the result.
           Reads ``{prefix}_do_iter{N}``.  Writes quality assessment to
           ``{prefix}_check_iter{N}``.  When quality is acceptable, writes
           ``quality_approved=yes`` to the scratchpad to trigger early
           termination.
        4. **act** (``required_tags: actor_tags``): Acts on the findings.
           Reads ``{prefix}_check_iter{N}``, writes action summary to
           ``{prefix}_act_iter{N}``.

        Loop termination:
        - Terminates early when ``scratchpad['quality_approved'] == 'yes'``
          (or the custom ``success_condition``).
        - Always terminates after ``max_cycles`` iterations.

        Returns:
        - ``workflow_id``: workflow run UUID for status polling.
        - ``name``: ``pdca/{objective_slug}``.
        - ``task_ids``: ``local_id → global_task_id`` mapping.
        - ``scratchpad_prefix``: scratchpad key namespace.
        - ``loop_block_name``: name of the loop block (``"pdca_cycle"``).

        Design references:
        - Deming, "Out of the Crisis" (1986) — PDCA cycle
        - Moxo, "Continuous improvement with the PDSA & PDCA cycle" (2025)
        - AiiDA ``while_()`` convergence loop for iterative scientific workflows
        - DESIGN.md §10.76 (v1.1.44)
        """
        from tmux_orchestrator.domain.phase_strategy import (  # noqa: PLC0415
            LoopBlock,
            LoopSpec,
            SkipCondition,
        )
        from tmux_orchestrator.phase_executor import (  # noqa: PLC0415
            AgentSelector,
            PhaseSpec,
            expand_phase_items_with_status,
        )
        from tmux_orchestrator.workflow_manager import validate_dag  # noqa: PLC0415

        wm = orchestrator.get_workflow_manager()
        objective_slug = body.objective[:40].replace(" ", "_").replace("/", "_")
        wf_name = f"pdca/{objective_slug}"
        pre_run_id = uuid.uuid4().hex[:8]
        sp = f"{body.scratchpad_prefix}_{pre_run_id}"  # unique prefix for this run

        # Resolve success condition (default: quality_approved == yes)
        if body.success_condition is not None:
            until = SkipCondition(
                key=body.success_condition.key,
                value=body.success_condition.value,
                negate=body.success_condition.negate,
            )
        else:
            until = SkipCondition(key="quality_approved", value="yes")

        # Scratchpad helper snippet
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str) -> str:
            return (
                f"   VALUE=$(cat your_output_file_or_echo_result_here)\n"
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'$VALUE'\"}}'"
            )

        def _read_snippet(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        def _approve_snippet() -> str:
            """Snippet for checker to write quality_approved=yes."""
            return (
                f"   # Write quality approval signal when quality is acceptable:\n"
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/quality_approved\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"yes\"}}'"
            )

        plan_key = f"{sp}_plan_iter{{iter}}"
        do_key = f"{sp}_do_iter{{iter}}"
        check_key = f"{sp}_check_iter{{iter}}"
        act_key = f"{sp}_act_iter{{iter}}"

        plan_prompt = (
            f"You are the PLAN agent in a PDCA (Plan-Do-Check-Act) iterative improvement workflow.\n"
            f"\n"
            f"**Objective:** {body.objective}\n"
            f"**Iteration:** {{iter}} of {body.max_cycles}\n"
            f"\n"
            f"Your task:\n"
            f"1. Analyse the current state (read previous iteration outputs from the scratchpad if available).\n"
            f"2. Produce a concrete, actionable plan to advance the objective.\n"
            f"3. Write your plan to the shared scratchpad.\n"
            f"\n"
            f"To read previous iteration outputs and write your plan:\n"
            f"```bash\n"
            f"{_ctx_snippet}\n"
            f"# Write your plan:\n"
            f"PLAN='<your detailed plan here>'\n"
            f"curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"  \"$WEB_BASE_URL/scratchpad/{plan_key}\" \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"value\": \"'$PLAN'\"}}'\n"
            f"```\n"
        )

        do_prompt = (
            f"You are the DO agent in a PDCA iterative improvement workflow.\n"
            f"\n"
            f"**Objective:** {body.objective}\n"
            f"**Iteration:** {{iter}} of {body.max_cycles}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read the plan for this iteration from the scratchpad.\n"
            f"2. Execute the plan — implement the improvements described.\n"
            f"3. Write the result/output to the shared scratchpad.\n"
            f"\n"
            f"To read the plan and write your output:\n"
            f"```bash\n"
            f"{_ctx_snippet}\n"
            f"# Read this iteration's plan:\n"
            f"{_read_snippet(plan_key, 'PLAN')}"
            f"echo \"Plan: $PLAN\"\n"
            f"# Implement the plan, then write your output:\n"
            f"OUTPUT='<your implementation result here>'\n"
            f"curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"  \"$WEB_BASE_URL/scratchpad/{do_key}\" \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"value\": \"'$OUTPUT'\"}}'\n"
            f"```\n"
        )

        check_prompt = (
            f"You are the CHECK agent in a PDCA iterative improvement workflow.\n"
            f"\n"
            f"**Objective:** {body.objective}\n"
            f"**Iteration:** {{iter}} of {body.max_cycles}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read the DO agent's output from the scratchpad.\n"
            f"2. Evaluate the quality of the output against the objective.\n"
            f"3. Write your assessment to the scratchpad.\n"
            f"4. If quality is acceptable, write quality_approved=yes to signal loop termination.\n"
            f"\n"
            f"To read the output, write your assessment, and optionally approve:\n"
            f"```bash\n"
            f"{_ctx_snippet}\n"
            f"# Read the DO agent's output:\n"
            f"{_read_snippet(do_key, 'OUTPUT')}"
            f"echo \"Output: $OUTPUT\"\n"
            f"# Write your quality assessment:\n"
            f"ASSESSMENT='<your quality assessment here>'\n"
            f"curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"  \"$WEB_BASE_URL/scratchpad/{check_key}\" \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"value\": \"'$ASSESSMENT'\"}}'\n"
            f"# If quality is acceptable, signal loop termination:\n"
            f"{_approve_snippet()}\n"
            f"```\n"
            f"\n"
            f"Only write quality_approved=yes when the objective is fully met.\n"
        )

        act_prompt = (
            f"You are the ACT agent in a PDCA iterative improvement workflow.\n"
            f"\n"
            f"**Objective:** {body.objective}\n"
            f"**Iteration:** {{iter}} of {body.max_cycles}\n"
            f"\n"
            f"Your task:\n"
            f"1. Read the CHECK agent's assessment from the scratchpad.\n"
            f"2. Standardise improvements or prepare adjustments for the next cycle.\n"
            f"3. Write your action summary to the scratchpad.\n"
            f"\n"
            f"To read the assessment and write your action summary:\n"
            f"```bash\n"
            f"{_ctx_snippet}\n"
            f"# Read the CHECK agent's assessment:\n"
            f"{_read_snippet(check_key, 'ASSESSMENT')}"
            f"echo \"Assessment: $ASSESSMENT\"\n"
            f"# Write your action summary:\n"
            f"ACTION='<your action summary and lessons learned>'\n"
            f"curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"  \"$WEB_BASE_URL/scratchpad/{act_key}\" \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"value\": \"'$ACTION'\"}}'\n"
            f"```\n"
        )

        plan_phase = PhaseSpec(
            name="plan",
            pattern="single",
            agents=AgentSelector(tags=list(body.planner_tags)),
            required_tags=list(body.planner_tags),
            timeout=body.agent_timeout,
            context=plan_prompt,
        )
        do_phase = PhaseSpec(
            name="do",
            pattern="single",
            agents=AgentSelector(tags=list(body.doer_tags)),
            required_tags=list(body.doer_tags),
            timeout=body.agent_timeout,
            context=do_prompt,
        )
        check_phase = PhaseSpec(
            name="check",
            pattern="single",
            agents=AgentSelector(tags=list(body.checker_tags)),
            required_tags=list(body.checker_tags),
            timeout=body.agent_timeout,
            context=check_prompt,
        )
        act_phase = PhaseSpec(
            name="act",
            pattern="single",
            agents=AgentSelector(tags=list(body.actor_tags)),
            required_tags=list(body.actor_tags),
            timeout=body.agent_timeout,
            context=act_prompt,
        )

        loop_block = LoopBlock(
            name="pdca_cycle",
            loop=LoopSpec(max=body.max_cycles, until=until),
            phases=[plan_phase, do_phase, check_phase, act_phase],
        )

        task_specs, phase_statuses, _loop_terminals = expand_phase_items_with_status(
            [loop_block],
            context=body.objective,
            scratchpad_prefix=sp,
        )

        try:
            ordered = validate_dag(task_specs, local_id_key="local_id", deps_key="depends_on")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        local_to_global: dict[str, str] = {}
        global_task_ids: list[str] = []

        for spec in ordered:
            global_deps = [local_to_global[lid] for lid in spec.get("depends_on", [])]
            task = await orchestrator.submit_task(
                spec["prompt"],
                priority=0,
                depends_on=global_deps or None,
                required_tags=spec.get("required_tags") or None,
                timeout=spec.get("timeout"),
            )
            local_to_global[spec["local_id"]] = task.id
            global_task_ids.append(task.id)

        run = wm.submit(name=wf_name, task_ids=global_task_ids)

        if phase_statuses:
            for ps in phase_statuses:
                ps.task_ids = [local_to_global[lid] for lid in ps.task_ids if lid in local_to_global]
            run.phases = phase_statuses
            wm.register_phases(run.id)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": local_to_global,
            "scratchpad_prefix": sp,
            "loop_block_name": "pdca_cycle",
            "max_cycles": body.max_cycles,
        }

    # -------------------------------------------------------------------------
    # POST /workflows/paircoder
    # PairCoder writer→reviewer loop (Codified Context hot-memory pattern)
    # -------------------------------------------------------------------------

    @router.post(
        "/workflows/paircoder",
        summary="Submit a PairCoder writer→reviewer loop workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_paircoder_workflow(body: PairCoderWorkflowSubmit) -> dict:
        """Submit a PairCoder writer→reviewer loop Workflow DAG.

        The PairCoder pattern implements the Generator-and-Critic (Ralph Loop)
        methodology in which two agents iterate: a **writer** implements the task
        and a **reviewer** checks the output against codified conventions, providing
        structured PASS/FAIL feedback.  The writer revises until the reviewer is
        satisfied or ``max_rounds`` is reached.

        Workflow topology (max_rounds=N, strictly sequential)::

            write_r1 → review_r1 → write_r2 → review_r2 → … → review_rN

        Scratchpad keys (Blackboard pattern):

        - ``{prefix}_impl_r{n}``     : path to writer's implementation file for round N
        - ``{prefix}_feedback_r{n}`` : reviewer's PASS/FAIL feedback for round N
        - ``{prefix}_verdict``       : final PASS or FAIL verdict (written by last reviewer)

        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``paircoder/<task[:40]>``)
        - ``task_ids``: ordered dict ``{write_r1, review_r1, write_r2, review_r2, …}``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - Geoffrey Huntley "The Ralph Loop" — external review loop for coding agents (2025)
          https://dev.to/yw1975/after-2-years-of-ai-assisted-coding-i-automated-the-one-thing
        - Google ADK "Generator-and-Critic pattern" (developers.googleblog.com, 2025)
          https://developers.googleblog.com/developers-guide-to-multi-agent-patterns-in-adk/
        - Vasilopoulos et al. arXiv:2602.20478 "Codified Context" §3 (2026)
        - DESIGN.md §10.86 (v1.2.10)
        """

        wm = orchestrator.get_workflow_manager()
        task_slug = body.task[:40].replace(" ", "_").replace("/", "_")
        wf_name = f"paircoder/{task_slug}"

        pre_run_id = str(uuid.uuid4())
        if body.scratchpad_prefix:
            scratchpad_prefix = body.scratchpad_prefix
        else:
            scratchpad_prefix = f"paircoder_{pre_run_id[:8]}"

        lang = body.language
        # File extension for implementation output
        _ext_map = {
            "python": "py",
            "javascript": "js",
            "typescript": "ts",
            "rust": "rs",
            "go": "go",
            "java": "java",
            "c": "c",
            "cpp": "cpp",
        }
        ext = _ext_map.get(lang.lower(), "py")

        # Shared bash snippet for reading orchestrator context and API key
        _ctx_snippet = (
            "WEB_BASE_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(cat __orchestrator_api_key__ 2>/dev/null || "
            "echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_snippet(key: str, filename: str) -> str:
            """Python3-based snippet to safely write a file's content to scratchpad."""
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"content = open('{filename}', 'r', errors='replace').read()\n"
                f"payload = json.dumps({{'value': content}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored to scratchpad: {key}')\n"
                f"\"  "
            )

        def _write_value_snippet(key: str, value_expr: str) -> str:
            """Python3-based snippet to write a string value to scratchpad."""
            return (
                f"   python3 -c \"\n"
                f"import json, urllib.request, os\n"
                f"payload = json.dumps({{'value': {value_expr}}}).encode()\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"if not api_key:\n"
                f"    try: api_key = open('__orchestrator_api_key__').read().strip()\n"
                f"    except: pass\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"base_url = ctx['web_base_url']\n"
                f"req = urllib.request.Request(base_url + '/scratchpad/{key}', data=payload, method='PUT')\n"
                f"req.add_header('Content-Type', 'application/json')\n"
                f"req.add_header('X-API-Key', api_key)\n"
                f"urllib.request.urlopen(req, timeout=15)\n"
                f"print('Stored verdict to scratchpad: {key}')\n"
                f"\"  "
            )

        def _read_snippet(key: str, varname: str) -> str:
            """Bash snippet that reads scratchpad key into $varname."""
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_BASE_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        # Spec keys section for reviewer: read each spec_key from scratchpad
        spec_keys_section = ""
        if body.spec_keys:
            spec_keys_section = (
                f"\n4. Read the following convention/spec keys from scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
            )
            for sk in body.spec_keys:
                spec_keys_section += _read_snippet(sk, f"SPEC_{sk.upper().replace('-', '_')}")
            spec_keys_section += "   ```\n   Check the implementation against these specifications.\n"

        # Build writer and reviewer tasks for each round
        task_specs: list[dict] = []

        for rnd in range(1, body.max_rounds + 1):
            impl_key = f"{scratchpad_prefix}_impl_r{rnd}"
            feedback_key = f"{scratchpad_prefix}_feedback_r{rnd}"
            verdict_key = f"{scratchpad_prefix}_verdict"
            is_first_round = rnd == 1
            is_last_round = rnd == body.max_rounds

            write_local_id = f"write_r{rnd}"
            review_local_id = f"review_r{rnd}"

            # Dependency: write_r1 depends on nothing; subsequent writes depend on review_r{n-1}
            write_deps = [] if is_first_round else [f"review_r{rnd - 1}"]
            # Review always depends on the same-round write
            review_deps = [write_local_id]

            # ----------------------------------------------------------------
            # Writer prompt
            # ----------------------------------------------------------------
            feedback_preamble = ""
            if not is_first_round:
                prev_feedback_key = f"{scratchpad_prefix}_feedback_r{rnd - 1}"
                feedback_preamble = (
                    f"\nThis is ROUND {rnd} of {body.max_rounds}. Read the reviewer's feedback "
                    f"from the previous round and address ALL points.\n\n"
                    f"Steps to read previous feedback:\n"
                    f"```bash\n"
                    f"{_ctx_snippet}\n"
                    + _read_snippet(prev_feedback_key, "PREV_FEEDBACK")
                    + f'echo "Previous feedback:"\necho "$PREV_FEEDBACK"\n'
                    f"```\n"
                    f"Address every issue raised before writing your revised implementation.\n"
                )
            else:
                feedback_preamble = f"\nThis is ROUND {rnd} of {body.max_rounds} (initial implementation).\n"

            writer_prompt = (
                f"You are the WRITER agent in a PairCoder writer→reviewer loop.\n"
                f"\n"
                f"**Task:** {body.task}\n"
                f"**Language:** {lang}\n"
                f"{feedback_preamble}\n"
                f"Steps:\n"
                f"1. Write the complete {lang} implementation to `impl.{ext}` in your working directory.\n"
                f"   - Add type hints to ALL function parameters and return values\n"
                f"   - Add docstrings to ALL public functions\n"
                f"   - Handle edge cases (empty input, boundary values, error conditions)\n"
                f"   - Follow the conventions in your CLAUDE.md (## Codified Specs section if present)\n"
                f"2. Verify your implementation parses/compiles:\n"
                f"   ```bash\n"
                f"   python3 -m py_compile impl.{ext} && echo 'Syntax OK'\n"
                f"   ```\n"
                f"3. Commit your implementation:\n"
                f"   ```bash\n"
                f"   git add impl.{ext}\n"
                f"   git commit -m 'impl: round {rnd}'\n"
                f"   ```\n"
                f"4. Store the file path in the shared scratchpad:\n"
                f"   ```python\n"
                + _write_value_snippet(impl_key, f"'impl.{ext}'")
                + f"\n"
                f"   ```\n"
                f"\n"
                f"Be thorough. The reviewer will check your implementation against coding conventions.\n"
                f"Call `/task-complete` when done."
            )

            # ----------------------------------------------------------------
            # Reviewer prompt
            # ----------------------------------------------------------------
            verdict_instruction = ""
            if is_last_round:
                verdict_instruction = (
                    f"\n5. Write the final verdict (PASS or FAIL) to scratchpad key `{verdict_key}`:\n"
                    f"   ```python\n"
                    + _write_value_snippet(verdict_key, "verdict")
                    + f"\n"
                    f"   ```\n"
                    f"   where `verdict` is the Python string 'PASS' or 'FAIL'.\n"
                )

            reviewer_prompt = (
                f"You are the REVIEWER agent in a PairCoder writer→reviewer loop.\n"
                f"\n"
                f"**Task being reviewed:** {body.task}\n"
                f"**Language:** {lang}\n"
                f"**Round:** {rnd} of {body.max_rounds}\n"
                f"\n"
                f"Steps:\n"
                f"1. Read the implementation file path from the scratchpad:\n"
                f"   ```bash\n"
                f"   {_ctx_snippet}\n"
                + _read_snippet(impl_key, "IMPL_PATH")
                + f"   echo \"Implementation path: $IMPL_PATH\"\n"
                f"   ```\n"
                f"2. Read the implementation file:\n"
                f"   ```bash\n"
                f"   cat impl.{ext}\n"
                f"   ```\n"
                f"   (If the path in $IMPL_PATH is not `impl.{ext}`, read from $IMPL_PATH)\n"
                f"3. Review the implementation carefully against these criteria:\n"
                f"   - Type hints present on ALL function parameters and return values\n"
                f"   - Docstrings on ALL public functions\n"
                f"   - No bare `except` clauses — specific exception types required\n"
                f"   - f-strings used for string formatting (not .format() or %)\n"
                f"   - Edge cases handled (empty input, None values, boundary conditions)\n"
                f"   - Logic is correct and implements the task as described\n"
                + spec_keys_section
                + f"4. Write your feedback to scratchpad key `{feedback_key}`:\n"
                f"   Start with **PASS** or **FAIL**, then list specific issues.\n"
                f"   Format:\n"
                f"   ```\n"
                f"   PASS  (or FAIL)\n"
                f"   \n"
                f"   Issues found:\n"
                f"   - [issue 1]\n"
                f"   - [issue 2]\n"
                f"   \n"
                f"   Recommendations:\n"
                f"   - [recommendation]\n"
                f"   ```\n"
                f"   Store feedback:\n"
                f"   ```python\n"
                + _write_snippet(feedback_key, f"feedback.txt")
                + f"\n"
                f"   ```\n"
                f"   (Write the feedback text to `feedback.txt` first, then store it)\n"
                + verdict_instruction
                + f"\n"
                f"Be thorough but fair. Your feedback drives the next revision round.\n"
                f"Call `/task-complete` when done."
            )

            task_specs.append({
                "local_id": write_local_id,
                "prompt": writer_prompt,
                "depends_on": write_deps,
                "required_tags": list(body.writer_tags),
                "timeout": body.agent_timeout,
            })
            task_specs.append({
                "local_id": review_local_id,
                "prompt": reviewer_prompt,
                "depends_on": review_deps,
                "required_tags": list(body.reviewer_tags),
                "timeout": body.agent_timeout,
            })

        # Validate DAG (no cycles, no unknown local_ids)
        try:
            from tmux_orchestrator.workflow_manager import validate_dag  # noqa: PLC0415
            ordered = validate_dag(task_specs, local_id_key="local_id", deps_key="depends_on")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        local_to_global: dict[str, str] = {}
        global_task_ids: list[str] = []

        for spec in ordered:
            global_deps = [local_to_global[lid] for lid in spec.get("depends_on", [])]
            task = await orchestrator.submit_task(
                spec["prompt"],
                priority=0,
                depends_on=global_deps or None,
                required_tags=spec.get("required_tags") or None,
                timeout=spec.get("timeout"),
            )
            local_to_global[spec["local_id"]] = task.id
            global_task_ids.append(task.id)

        run = wm.submit(name=wf_name, task_ids=global_task_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": local_to_global,
            "scratchpad_prefix": scratchpad_prefix,
            "max_rounds": body.max_rounds,
        }

    # -----------------------------------------------------------------------
    # POST /workflows/peer-review  (v1.2.24)
    # -----------------------------------------------------------------------

    @router.post(
        "/workflows/peer-review",
        summary="Submit a 3-agent Peer Review workflow (parallel impl-a + impl-b → reviewer)",
        dependencies=[Depends(auth)],
    )
    async def submit_peer_review_workflow(body: PeerReviewWorkflowSubmit) -> dict:
        """Submit a 3-agent Peer Review Workflow DAG.

        Two implementer agents work **in parallel** on the same feature, then a
        single reviewer agent compares both implementations and declares a winner.

        DAG topology::

            impl-a ──┐
                     ├──▶ reviewer
            impl-b ──┘

        Scratchpad keys:
        - ``{prefix}_impl_a`` — implementation A (code text)
        - ``{prefix}_impl_b`` — implementation B (code text)
        - ``{prefix}_review``  — reviewer's structured REVIEW.md
        - ``{prefix}_winner``  — ``"A"`` or ``"B"`` (winner declaration)

        Returns:
        - ``workflow_id``: workflow run UUID
        - ``name``: ``peer-review/<feature>``
        - ``task_ids``: ``{"impl_a": ..., "impl_b": ..., "reviewer": ...}``
        - ``scratchpad_prefix``: scratchpad namespace for this run

        Design references:
        - AgentReview: EMNLP 2024 arXiv:2406.12708
        - arXiv:2505.16339 "Rethinking Code Review Workflows" (2025)
        - DESIGN.md §10.99 (v1.2.24)
        """
        lang = body.language
        feature = body.feature

        wm = orchestrator.get_workflow_manager()
        wf_name = f"peer-review/{feature}"

        pre_run_id = str(uuid.uuid4())
        prefix = body.scratchpad_prefix or f"peerreview_{pre_run_id[:8]}"

        impl_a_key = f"{prefix}_impl_a"
        impl_b_key = f"{prefix}_impl_b"
        review_key = f"{prefix}_review"
        winner_key = f"{prefix}_winner"

        # ------------------------------------------------------------------
        # Common bash snippet: read context + API key
        # ------------------------------------------------------------------
        _ctx = (
            "WEB_URL=$(python3 -c \""
            "import json; d=json.load(open('__orchestrator_context__.json')); "
            "print(d['web_base_url'])\")\n"
            "   API_KEY=$(echo \"$TMUX_ORCHESTRATOR_API_KEY\")"
        )

        def _write_sp(key: str, var: str = "CONTENT") -> str:
            return (
                f"   curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_URL/scratchpad/{key}\" \\\n"
                f"     -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"value\": \"'${var}'\"}}'  "
            )

        def _read_sp(key: str, varname: str) -> str:
            return (
                f"   {varname}=$(curl -s -H \"X-API-Key: $API_KEY\" \\\n"
                f"     \"$WEB_URL/scratchpad/{key}\" \\\n"
                f"     | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"value\"])')\n"
            )

        # ------------------------------------------------------------------
        # Prompt: implementer A (approach: idiomatic / clear)
        # ------------------------------------------------------------------
        impl_a_prompt = (
            f"You are IMPLEMENTER-A in a Peer Review workflow.\n\n"
            f"**Feature:** {feature}\n"
            f"**Language:** {lang}\n\n"
            f"Your approach: **idiomatic and readable** — prioritise clarity, "
            f"meaningful names, and adherence to {lang} idioms.\n\n"
            f"Tasks:\n"
            f"1. Implement '{feature}' in {lang}.\n"
            f"   - Write clean, well-commented code.\n"
            f"   - Include docstrings for all public functions/methods.\n"
            f"   - Handle edge cases explicitly.\n"
            f"2. Save your implementation to `impl_a.{lang[:2]}`.\n"
            f"3. Write a brief self-assessment (≤100 words) at the end as a comment.\n"
            f"4. Store your implementation in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx}\n"
            f"   CONTENT=$(cat impl_a.{lang[:2]})\n"
            + _write_sp(impl_a_key)
            + f"\n"
            f"   ```\n"
            f"5. Call `/task-complete \"impl_a.{lang[:2]} written and stored\"`"
        )

        # ------------------------------------------------------------------
        # Prompt: implementer B (approach: performance / concise)
        # ------------------------------------------------------------------
        impl_b_prompt = (
            f"You are IMPLEMENTER-B in a Peer Review workflow.\n\n"
            f"**Feature:** {feature}\n"
            f"**Language:** {lang}\n\n"
            f"Your approach: **performance-oriented and concise** — minimise "
            f"runtime complexity, reduce boilerplate, use built-in capabilities.\n\n"
            f"Tasks:\n"
            f"1. Implement '{feature}' in {lang}.\n"
            f"   - Optimise for efficiency and minimal code length.\n"
            f"   - Use language built-ins and standard library where possible.\n"
            f"   - Handle edge cases correctly (do not sacrifice correctness).\n"
            f"2. Save your implementation to `impl_b.{lang[:2]}`.\n"
            f"3. Write a brief self-assessment (≤100 words) at the end as a comment.\n"
            f"4. Store your implementation in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx}\n"
            f"   CONTENT=$(cat impl_b.{lang[:2]})\n"
            + _write_sp(impl_b_key)
            + f"\n"
            f"   ```\n"
            f"5. Call `/task-complete \"impl_b.{lang[:2]} written and stored\"`"
        )

        # ------------------------------------------------------------------
        # Prompt: reviewer (compare both, select winner)
        # ------------------------------------------------------------------
        reviewer_prompt = (
            f"You are the REVIEWER in a Peer Review workflow.\n\n"
            f"**Feature:** {feature}\n"
            f"**Language:** {lang}\n\n"
            f"Two implementers have independently implemented '{feature}'.\n"
            f"Your task: compare both implementations and declare a winner.\n\n"
            f"Step 1 — Load both implementations:\n"
            f"```bash\n"
            f"{_ctx}\n"
            + _read_sp(impl_a_key, "IMPL_A")
            + _read_sp(impl_b_key, "IMPL_B")
            + f"```\n\n"
            f"Step 2 — Write `REVIEW.md` with this structure:\n"
            f"```markdown\n"
            f"# Peer Review: {feature}\n\n"
            f"## Implementation A\n"
            f"**Strengths:** ...\n"
            f"**Weaknesses:** ...\n"
            f"**Score (1-10):** N\n\n"
            f"## Implementation B\n"
            f"**Strengths:** ...\n"
            f"**Weaknesses:** ...\n"
            f"**Score (1-10):** N\n\n"
            f"## Comparative Analysis\n"
            f"Evaluation axes:\n"
            f"- Correctness (handles all edge cases?)\n"
            f"- Readability (clear names, comments, structure?)\n"
            f"- Efficiency (time/space complexity?)\n"
            f"- Idiomaticity (idiomatic {lang}?)\n\n"
            f"## Winner\n"
            f"**WINNER: [A|B]**\n"
            f"Reason: <one sentence>\n"
            f"```\n\n"
            f"Step 3 — Store review and winner:\n"
            f"```bash\n"
            f"CONTENT=$(cat REVIEW.md)\n"
            + _write_sp(review_key)
            + f"\n"
            f"curl -s -X PUT -H \"X-API-Key: $API_KEY\" \\\n"
            f"  \"$WEB_URL/scratchpad/{winner_key}\" \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"value\": \"A\"}}' # Replace A with actual winner\n"
            f"```\n\n"
            f"4. Call `/task-complete \"REVIEW.md written, winner declared\"`"
        )

        # ------------------------------------------------------------------
        # Submit tasks
        # ------------------------------------------------------------------
        task_a = await orchestrator.submit_task(
            prompt=impl_a_prompt,
            required_tags=body.impl_a_tags or None,
            timeout=body.agent_timeout,
        )
        task_b = await orchestrator.submit_task(
            prompt=impl_b_prompt,
            required_tags=body.impl_b_tags or None,
            timeout=body.agent_timeout,
        )
        task_reviewer = await orchestrator.submit_task(
            prompt=reviewer_prompt,
            required_tags=body.reviewer_tags or None,
            depends_on=[task_a.id, task_b.id],
            timeout=body.agent_timeout,
            reply_to=body.reply_to,
        )

        task_ids = {
            "impl_a": task_a.id,
            "impl_b": task_b.id,
            "reviewer": task_reviewer.id,
        }

        all_ids = list(task_ids.values())
        run = wm.submit(name=wf_name, task_ids=all_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids,
            "scratchpad_prefix": prefix,
        }

    # -----------------------------------------------------------------------
    # POST /workflows/mutation-test  (v1.2.25)
    # -----------------------------------------------------------------------

    @router.post(
        "/workflows/mutation-test",
        summary="Submit a 3-agent Mutation Testing workflow (implementer → mutant-introducer → test-improver)",
        dependencies=[Depends(auth)],
    )
    async def submit_mutation_test_workflow(body: MutationTestWorkflowSubmit) -> dict:
        """Submit a 3-agent Mutation Testing Workflow DAG.

        Pipeline:
        1. **implementer**: writes the feature + initial test suite.
        2. **mutant-introducer**: introduces N intentional bugs that the
           initial tests miss; outputs mutated code + mutation descriptions.
        3. **test-improver**: adds tests that kill every mutant; verifies
           tests pass on the original code.

        Scratchpad keys:
        - ``{prefix}_impl``           : implementation + initial tests
        - ``{prefix}_mutants``        : mutated code + descriptions
        - ``{prefix}_improved_tests`` : final test suite killing all mutants

        Returns:
        - ``workflow_id``: workflow run UUID
        - ``name``: ``mutation-test/<feature>``
        - ``task_ids``: ``{"implementer": ..., "mutant_introducer": ..., "test_improver": ...}``
        - ``scratchpad_prefix``: scratchpad namespace

        Design references:
        - AdverTest arXiv:2602.08146
        - Meta ACH arXiv:2501.12862 (FSE 2025)
        - DESIGN.md §10.100 (v1.2.25)
        """
        lang = body.language
        feature = body.feature
        n = body.num_mutations

        wm = orchestrator.get_workflow_manager()
        wf_name = f"mutation-test/{feature}"

        pre_run_id = str(uuid.uuid4())
        prefix = body.scratchpad_prefix or f"muttest_{pre_run_id[:8]}"

        impl_key = f"{prefix}_impl"
        mutants_key = f"{prefix}_mutants"
        improved_tests_key = f"{prefix}_improved_tests"

        # ------------------------------------------------------------------
        # Simple Python-based scratchpad write snippet (avoids bash quoting)
        # ------------------------------------------------------------------
        def _sp_write_py(key: str, var_expr: str) -> str:
            """Return a Python-based scratchpad write snippet."""
            return (
                f"python3 - <<'PYEOF'\n"
                f"import json, urllib.request, os\n"
                f"url = json.load(open('__orchestrator_context__.json'))['web_base_url']\n"
                f"key = '{key}'\n"
                f"val = {var_expr}\n"
                f"req = urllib.request.Request(\n"
                f"    f'{{url}}/scratchpad/{{key}}',\n"
                f"    data=json.dumps({{'value': val}}).encode(),\n"
                f"    headers={{'Content-Type': 'application/json', "
                f"'X-API-Key': os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')}},\n"
                f"    method='PUT',\n"
                f")\n"
                f"urllib.request.urlopen(req, timeout=30)\n"
                f"print(f'Written {{len(val)}} chars to {{key}}')\n"
                f"PYEOF"
            )

        def _sp_read_py(key: str, var_name: str) -> str:
            """Return a Python-based scratchpad read snippet."""
            return (
                f"{var_name}=$(python3 - <<'PYEOF'\n"
                f"import json, urllib.request, os\n"
                f"url = json.load(open('__orchestrator_context__.json'))['web_base_url']\n"
                f"req = urllib.request.Request(\n"
                f"    f'{{url}}/scratchpad/{key}',\n"
                f"    headers={{'X-API-Key': os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')}},\n"
                f")\n"
                f"print(json.loads(urllib.request.urlopen(req, timeout=30).read())['value'])\n"
                f"PYEOF\n"
                f")"
            )

        # ------------------------------------------------------------------
        # 1. IMPLEMENTER prompt
        # ------------------------------------------------------------------
        implementer_prompt = (
            f"You are the IMPLEMENTER agent in a Mutation Testing workflow.\n\n"
            f"**Feature:** {feature}\n"
            f"**Language:** {lang}\n\n"
            f"Tasks:\n"
            f"1. Implement '{feature}' in {lang}. Write clean, correct code.\n"
            f"2. Write an initial test suite that verifies the basic behaviour.\n"
            f"   Tests should cover: happy path, edge cases (empty, null, boundary).\n"
            f"   Aim for at least 5 tests.\n"
            f"3. Save implementation to `impl.{lang[:2]}` and tests to `tests_initial.{lang[:2]}`.\n"
            f"4. Store both files in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_sp_write_py(impl_key, 'open(\"impl.\" + \"{lang[:2]}\").read()')}\n"
            f"   ```\n"
            f"5. Call `/task-complete \"Implementation + initial tests written to scratchpad\"`"
        )

        # ------------------------------------------------------------------
        # 2. MUTANT-INTRODUCER prompt
        # ------------------------------------------------------------------
        mutant_introducer_prompt = (
            f"You are the MUTANT-INTRODUCER agent in a Mutation Testing workflow.\n\n"
            f"**Feature:** {feature}\n"
            f"**Language:** {lang}\n"
            f"**Number of mutations to introduce:** {n}\n\n"
            f"Tasks:\n"
            f"1. Load the original implementation from scratchpad:\n"
            f"   ```bash\n"
            f"   {_sp_read_py(impl_key, 'IMPL')}\n"
            f"   echo \"$IMPL\" > impl_original.{lang[:2]}\n"
            f"   ```\n"
            f"2. Create {n} mutated versions by introducing subtle bugs.\n"
            f"   Each mutation should:\n"
            f"   - Be a minimal, plausible change (off-by-one, wrong operator, wrong comparison)\n"
            f"   - NOT be caught by the initial basic tests\n"
            f"   - Be a real fault (not just formatting)\n"
            f"3. Write `mutants.md` documenting each mutation:\n"
            f"   ```markdown\n"
            f"   # Mutations for: {feature}\n\n"
            f"   ## Mutation 1: <name>\n"
            f"   **Type:** <off-by-one|wrong-operator|missing-check|...>\n"
            f"   **Location:** line N — original: `<code>` → mutated: `<code>`\n"
            f"   **Expected failure:** test that would catch this: `test_<name>`\n\n"
            f"   ## Mutation 2: ...\n"
            f"   ```\n"
            f"4. Write the mutated code to `impl_mutated.{lang[:2]}`.\n"
            f"5. Store mutation report + mutated code in scratchpad:\n"
            f"   ```bash\n"
            f"   {_sp_write_py(mutants_key, 'open(\"mutants.md\").read()')}\n"
            f"   ```\n"
            f"6. Call `/task-complete \"{n} mutations introduced and documented\"`"
        )

        # ------------------------------------------------------------------
        # 3. TEST-IMPROVER prompt
        # ------------------------------------------------------------------
        test_improver_prompt = (
            f"You are the TEST-IMPROVER agent in a Mutation Testing workflow.\n\n"
            f"**Feature:** {feature}\n"
            f"**Language:** {lang}\n\n"
            f"Tasks:\n"
            f"1. Load the implementation and mutation report from scratchpad:\n"
            f"   ```bash\n"
            f"   {_sp_read_py(impl_key, 'IMPL')}\n"
            f"   echo \"$IMPL\" > impl.{lang[:2]}\n"
            f"   {_sp_read_py(mutants_key, 'MUTANTS')}\n"
            f"   echo \"$MUTANTS\" > mutants.md\n"
            f"   ```\n"
            f"2. Read `mutants.md` to understand each mutation.\n"
            f"3. Write improved tests in `tests_improved.{lang[:2]}` that:\n"
            f"   - Include ALL original tests (do not remove any)\n"
            f"   - Add at least one new test per mutation that KILLS the mutant\n"
            f"   - Pass on the ORIGINAL implementation\n"
            f"   - Each new test has a docstring explaining which mutation it kills\n"
            f"4. Run tests against the original implementation to verify they pass:\n"
            f"   ```bash\n"
            f"   {lang[:2] if lang == 'python' else lang} -m pytest tests_improved.{lang[:2]} -v 2>&1 | tail -20\n"
            f"   ```\n"
            f"5. Store improved tests in scratchpad:\n"
            f"   ```bash\n"
            f"   {_sp_write_py(improved_tests_key, 'open(\"tests_improved.\" + \"{lang[:2]}\").read()')}\n"
            f"   ```\n"
            f"6. Call `/task-complete \"Improved tests written — all mutations killed\"`"
        )

        # ------------------------------------------------------------------
        # Submit tasks (linear sequential chain)
        # ------------------------------------------------------------------
        task_impl = await orchestrator.submit_task(
            prompt=implementer_prompt,
            required_tags=body.implementer_tags or None,
            timeout=body.agent_timeout,
        )
        task_mutant = await orchestrator.submit_task(
            prompt=mutant_introducer_prompt,
            required_tags=body.mutant_introducer_tags or None,
            depends_on=[task_impl.id],
            timeout=body.agent_timeout,
        )
        task_improver = await orchestrator.submit_task(
            prompt=test_improver_prompt,
            required_tags=body.test_improver_tags or None,
            depends_on=[task_mutant.id],
            timeout=body.agent_timeout,
            reply_to=body.reply_to,
        )

        task_ids = {
            "implementer": task_impl.id,
            "mutant_introducer": task_mutant.id,
            "test_improver": task_improver.id,
        }

        all_ids = list(task_ids.values())
        run = wm.submit(name=wf_name, task_ids=all_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids,
            "scratchpad_prefix": prefix,
            "num_mutations": n,
        }

    # -----------------------------------------------------------------------
    # POST /workflows/code-audit  (v1.2.26)
    # -----------------------------------------------------------------------

    @router.post(
        "/workflows/code-audit",
        summary="Submit a 4-agent Code Audit workflow (implementer → security-auditor ∥ performance-auditor → synthesizer)",
        dependencies=[Depends(auth)],
    )
    async def submit_code_audit_workflow(body: CodeAuditWorkflowSubmit) -> dict:
        """Submit a 4-agent Code Audit Workflow DAG.

        Pipeline:

          1. **implementer**: writes the feature implementation in the requested
             language and stores it in ``{prefix}_impl`` (scratchpad).
          2. **security-auditor** (parallel): reads ``{prefix}_impl`` and audits
             for security vulnerabilities using OWASP Top 10 / CWE classifications.
             Stores findings in ``{prefix}_security_audit``.
          3. **performance-auditor** (parallel): reads ``{prefix}_impl`` and audits
             for performance issues (algorithmic complexity, caching, I/O patterns).
             Stores findings in ``{prefix}_performance_audit``.
          4. **synthesizer**: reads both audit reports and produces a prioritised
             ``AUDIT_REPORT.md`` stored in ``{prefix}_audit_report``.

        Returns:
        - ``workflow_id``: workflow run UUID
        - ``name``: ``code-audit/<feature>``
        - ``task_ids``: dict with ``implementer``, ``security_auditor``,
          ``performance_auditor``, ``synthesizer``
        - ``scratchpad_prefix``: scratchpad namespace for this run
        - ``audit_focus``: list of audit domains included

        Design references:
        - RepoAudit arXiv:2501.18160 ICML 2025: specialist auditor agents in parallel
        - iAudit ICSE 2025: multi-agent conversational architecture for code auditing
        - Automating Security Audit Using LLMs arXiv:2505.10732 (2025)
        - DESIGN.md §10.101 (v1.2.26)
        """
        feature = body.feature.strip()
        lang = body.language

        wm = orchestrator.get_workflow_manager()
        wf_name = f"code-audit/{feature}"

        pre_run_id = str(uuid.uuid4())
        prefix = body.scratchpad_prefix or f"audit_{pre_run_id[:8]}"

        impl_key = f"{prefix}_impl"
        security_key = f"{prefix}_security_audit"
        perf_key = f"{prefix}_performance_audit"
        report_key = f"{prefix}_audit_report"

        def _py_write(key: str, varname: str) -> str:
            """Return a Python snippet that writes varname to the scratchpad key."""
            return (
                f"python3 - <<'PYEOF'\n"
                f"import json, urllib.request, os\n"
                f"content = open('{varname}').read()\n"
                f"url = open('__orchestrator_context__.json') if False else None\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"req = urllib.request.Request(\n"
                f"    ctx['web_base_url'] + '/scratchpad/{key}',\n"
                f"    data=json.dumps({{'value': content}}).encode(),\n"
                f"    headers={{'X-API-Key': api_key, 'Content-Type': 'application/json'}},\n"
                f"    method='PUT',\n"
                f")\n"
                f"urllib.request.urlopen(req)\n"
                f"PYEOF"
            )

        def _py_read(key: str, varname: str) -> str:
            """Return a Python snippet that reads key from scratchpad into varname file."""
            return (
                f"python3 - <<'PYEOF'\n"
                f"import json, urllib.request, os\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"req = urllib.request.Request(\n"
                f"    ctx['web_base_url'] + '/scratchpad/{key}',\n"
                f"    headers={{'X-API-Key': api_key}},\n"
                f"    method='GET',\n"
                f")\n"
                f"resp = json.loads(urllib.request.urlopen(req).read())\n"
                f"open('{varname}', 'w').write(resp['value'])\n"
                f"PYEOF"
            )

        # --- Implementer prompt ---
        implementer_prompt = (
            f"You are the IMPLEMENTER agent in a code audit workflow.\n"
            f"\n"
            f"**Feature to implement**: {feature}\n"
            f"**Language**: {lang}\n"
            f"\n"
            f"Steps:\n"
            f"1. Write a clean, working implementation of the feature above in {lang}.\n"
            f"   Include docstrings/comments explaining your design choices.\n"
            f"   Focus on correctness first — security and performance auditors will review later.\n"
            f"2. Write your implementation to `implementation.{lang[:2]}` in your working directory.\n"
            f"3. Store the implementation in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_write(impl_key, f'implementation.{lang[:2]}')}\n"
            f"   ```\n"
            f"4. Call /task-complete with a one-line summary.\n"
        )

        # --- Security auditor prompt ---
        security_auditor_prompt = (
            f"You are the SECURITY AUDITOR agent in a code audit workflow.\n"
            f"\n"
            f"**Feature being audited**: {feature} (language: {lang})\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the implementation from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_read(impl_key, 'implementation_to_audit.txt')}\n"
            f"   ```\n"
            f"2. Audit the code for security vulnerabilities using OWASP Top 10 and CWE classifications.\n"
            f"   For each finding, provide:\n"
            f"   - **Severity**: CRITICAL / HIGH / MEDIUM / LOW\n"
            f"   - **CWE ID** (if applicable)\n"
            f"   - **Description**: what the vulnerability is and where it appears\n"
            f"   - **Recommendation**: how to fix it\n"
            f"3. Write your audit to `security_audit.md` in your working directory.\n"
            f"4. Store the security audit in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_write(security_key, 'security_audit.md')}\n"
            f"   ```\n"
            f"5. Call /task-complete with a one-line summary of your findings.\n"
        )

        # --- Performance auditor prompt ---
        performance_auditor_prompt = (
            f"You are the PERFORMANCE AUDITOR agent in a code audit workflow.\n"
            f"\n"
            f"**Feature being audited**: {feature} (language: {lang})\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the implementation from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_read(impl_key, 'implementation_to_audit.txt')}\n"
            f"   ```\n"
            f"2. Audit the code for performance issues. Evaluate:\n"
            f"   - **Algorithmic complexity**: time and space complexity, unnecessary loops\n"
            f"   - **I/O patterns**: redundant reads/writes, missing buffering\n"
            f"   - **Caching opportunities**: repeated computations that could be memoised\n"
            f"   - **Resource management**: connection pooling, memory allocation patterns\n"
            f"   For each finding, provide Severity (HIGH/MEDIUM/LOW), Description, and Recommendation.\n"
            f"3. Write your audit to `performance_audit.md` in your working directory.\n"
            f"4. Store the performance audit in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_write(perf_key, 'performance_audit.md')}\n"
            f"   ```\n"
            f"5. Call /task-complete with a one-line summary of your findings.\n"
        )

        # --- Synthesizer prompt ---
        synthesizer_prompt = (
            f"You are the SYNTHESIZER agent in a code audit workflow.\n"
            f"\n"
            f"**Feature audited**: {feature} (language: {lang})\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the security audit:\n"
            f"   ```bash\n"
            f"   {_py_read(security_key, 'security_audit.md')}\n"
            f"   ```\n"
            f"2. Read the performance audit:\n"
            f"   ```bash\n"
            f"   {_py_read(perf_key, 'performance_audit.md')}\n"
            f"   ```\n"
            f"3. Read the original implementation:\n"
            f"   ```bash\n"
            f"   {_py_read(impl_key, 'implementation.txt')}\n"
            f"   ```\n"
            f"4. Write a consolidated `AUDIT_REPORT.md` with:\n"
            f"   - **Executive Summary**: 2–3 sentence overall assessment\n"
            f"   - **Critical Findings** (must fix before deployment)\n"
            f"   - **High Priority Findings** (fix in next sprint)\n"
            f"   - **Medium / Low Priority Findings** (track as tech debt)\n"
            f"   - **Positive Observations**: what the code does well\n"
            f"   - **Recommended Next Steps**: ordered action items\n"
            f"5. Store the audit report in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_write(report_key, 'AUDIT_REPORT.md')}\n"
            f"   ```\n"
            f"6. Call /task-complete with: 'Audit complete — N findings (C critical, H high, M medium, L low)'\n"
        )

        # Submit tasks — implementer first (no deps), auditors fan-out, synthesizer fan-in
        task_impl = await orchestrator.submit_task(
            prompt=implementer_prompt,
            required_tags=body.implementer_tags or None,
            timeout=body.agent_timeout,
        )
        task_security = await orchestrator.submit_task(
            prompt=security_auditor_prompt,
            required_tags=body.security_auditor_tags or None,
            depends_on=[task_impl.id],
            timeout=body.agent_timeout,
        )
        task_perf = await orchestrator.submit_task(
            prompt=performance_auditor_prompt,
            required_tags=body.performance_auditor_tags or None,
            depends_on=[task_impl.id],
            timeout=body.agent_timeout,
        )
        task_synth = await orchestrator.submit_task(
            prompt=synthesizer_prompt,
            required_tags=body.synthesizer_tags or None,
            depends_on=[task_security.id, task_perf.id],
            timeout=body.agent_timeout,
            reply_to=body.reply_to,
        )

        task_ids = {
            "implementer": task_impl.id,
            "security_auditor": task_security.id,
            "performance_auditor": task_perf.id,
            "synthesizer": task_synth.id,
        }

        all_ids = list(task_ids.values())
        run = wm.submit(name=wf_name, task_ids=all_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids,
            "scratchpad_prefix": prefix,
            "audit_focus": body.audit_focus,
        }

    @router.post(
        "/workflows/refactor",
        summary="Submit a 3-agent Refactoring workflow (analyzer → refactorer → verifier)",
        dependencies=[Depends(auth)],
    )
    async def submit_refactor_workflow(body: RefactorWorkflowSubmit) -> dict:
        """Submit a 3-agent Code Refactoring Workflow DAG.

        Pipeline:

          1. **analyzer**: examines the provided code for quality issues
             (complexity, duplication, naming, design patterns) and produces a
             structured analysis report stored in ``{prefix}_analysis``.
          2. **refactorer**: reads the analysis report and the original code,
             applies the recommended refactorings, and stores the improved code
             in ``{prefix}_refactored``.
          3. **verifier**: compares the original code against the refactored
             version, confirms behavior preservation, and reports quality metric
             improvements in ``{prefix}_verification``.

        Returns:
        - ``workflow_id``: workflow run UUID
        - ``name``: ``refactor/<code_snippet>``
        - ``task_ids``: dict with ``analyzer``, ``refactorer``, ``verifier``
        - ``scratchpad_prefix``: scratchpad namespace for this run
        - ``refactor_goals``: goals used to drive the analysis

        Design references:
        - RefAgent arXiv:2511.03153 (November 2025): multi-agent refactoring,
          90% unit-test pass rate, 52.5% code-smell reduction
        - RefactorGPT PeerJ cs-3257 (October 2025): Analyzer→Refactor→Fixer pattern
        - MUARF ICSE 2025 SRC: 3-role baseline for automated refactoring
        - LLM-Driven Code Refactoring (IDE @ ICSE 2025): behavior preservation
        - DESIGN.md §10.102 (v1.2.27)
        """
        code = body.code.strip()
        lang = body.language
        goals = body.refactor_goals

        wm = orchestrator.get_workflow_manager()
        code_snippet = code[:40].replace("\n", " ")
        wf_name = f"refactor/{code_snippet}"

        pre_run_id = str(uuid.uuid4())
        prefix = body.scratchpad_prefix or f"refactor_{pre_run_id[:8]}"

        analysis_key = f"{prefix}_analysis"
        refactored_key = f"{prefix}_refactored"
        verification_key = f"{prefix}_verification"

        def _py_write(key: str, varname: str) -> str:
            """Return a Python snippet that writes varname content to the scratchpad key."""
            return (
                f"python3 - <<'PYEOF'\n"
                f"import json, urllib.request, os\n"
                f"content = open('{varname}').read()\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"req = urllib.request.Request(\n"
                f"    ctx['web_base_url'] + '/scratchpad/{key}',\n"
                f"    data=json.dumps({{'value': content}}).encode(),\n"
                f"    headers={{'X-API-Key': api_key, 'Content-Type': 'application/json'}},\n"
                f"    method='PUT',\n"
                f")\n"
                f"urllib.request.urlopen(req)\n"
                f"PYEOF"
            )

        def _py_read(key: str, varname: str) -> str:
            """Return a Python snippet that reads key from scratchpad into varname file."""
            return (
                f"python3 - <<'PYEOF'\n"
                f"import json, urllib.request, os\n"
                f"ctx = json.load(open('__orchestrator_context__.json'))\n"
                f"api_key = os.environ.get('TMUX_ORCHESTRATOR_API_KEY', '')\n"
                f"req = urllib.request.Request(\n"
                f"    ctx['web_base_url'] + '/scratchpad/{key}',\n"
                f"    headers={{'X-API-Key': api_key}},\n"
                f"    method='GET',\n"
                f")\n"
                f"resp = json.loads(urllib.request.urlopen(req).read())\n"
                f"open('{varname}', 'w').write(resp['value'])\n"
                f"PYEOF"
            )

        goals_str = ", ".join(goals)

        # --- Analyzer prompt ---
        analyzer_prompt = (
            f"You are the ANALYZER agent in a code refactoring workflow.\n"
            f"\n"
            f"**Language**: {lang}\n"
            f"**Refactoring goals**: {goals_str}\n"
            f"\n"
            f"**Original code to analyze**:\n"
            f"```{lang}\n"
            f"{code}\n"
            f"```\n"
            f"\n"
            f"Steps:\n"
            f"1. Analyze the code above for quality issues, focusing on: {goals_str}.\n"
            f"   For each issue found, document:\n"
            f"   - **Issue type**: (complexity / duplication / naming / design / readability)\n"
            f"   - **Location**: function/class/line description\n"
            f"   - **Description**: what the problem is\n"
            f"   - **Recommended refactoring**: specific transformation to apply\n"
            f"   - **Priority**: HIGH / MEDIUM / LOW\n"
            f"2. Include a **Summary** section at the top: total issues found, overall quality score (1-10), top 3 recommendations.\n"
            f"3. Write your analysis to `analysis.md` in your working directory.\n"
            f"4. Also write the original code to `original_code.{lang[:2]}` for the refactorer's reference.\n"
            f"5. Store the analysis in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_write(analysis_key, 'analysis.md')}\n"
            f"   ```\n"
            f"6. Call /task-complete with: 'Analysis complete — N issues found (H high, M medium, L low)'\n"
        )

        # --- Refactorer prompt ---
        refactorer_prompt = (
            f"You are the REFACTORER agent in a code refactoring workflow.\n"
            f"\n"
            f"**Language**: {lang}\n"
            f"**Refactoring goals**: {goals_str}\n"
            f"\n"
            f"**Original code** (also available via scratchpad):\n"
            f"```{lang}\n"
            f"{code}\n"
            f"```\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the analysis report from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_read(analysis_key, 'analysis.md')}\n"
            f"   ```\n"
            f"2. Apply ALL HIGH-priority and as many MEDIUM-priority refactorings as possible.\n"
            f"   Preserve the original behavior exactly — refactoring must not change semantics.\n"
            f"   Follow these principles:\n"
            f"   - Extract long functions into smaller, well-named ones\n"
            f"   - Remove code duplication (DRY principle)\n"
            f"   - Use descriptive names for variables, functions, and classes\n"
            f"   - Apply appropriate design patterns where beneficial\n"
            f"   - Add/improve docstrings and comments\n"
            f"3. Write the refactored code to `refactored_code.{lang[:2]}`.\n"
            f"4. Write a brief `CHANGES.md` documenting each transformation applied:\n"
            f"   - What was changed\n"
            f"   - Why (which issue from the analysis)\n"
            f"   - How behavior is preserved\n"
            f"5. Combine refactored code and changes into a single document `refactored.md` with\n"
            f"   a code block and the changes list.\n"
            f"6. Store in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_write(refactored_key, 'refactored.md')}\n"
            f"   ```\n"
            f"7. Call /task-complete with: 'Refactoring complete — N transformations applied'\n"
        )

        # --- Verifier prompt ---
        verifier_prompt = (
            f"You are the VERIFIER agent in a code refactoring workflow.\n"
            f"\n"
            f"**Language**: {lang}\n"
            f"**Refactoring goals**: {goals_str}\n"
            f"\n"
            f"**Original code** (for reference):\n"
            f"```{lang}\n"
            f"{code}\n"
            f"```\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the refactored code from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_read(refactored_key, 'refactored.md')}\n"
            f"   ```\n"
            f"2. Read the original analysis:\n"
            f"   ```bash\n"
            f"   {_py_read(analysis_key, 'analysis.md')}\n"
            f"   ```\n"
            f"3. Verify the refactoring by checking:\n"
            f"   **Behavior Preservation**:\n"
            f"   - Are all public functions/classes/methods still present with compatible signatures?\n"
            f"   - Are edge cases (empty input, None, errors) still handled?\n"
            f"   - Are all return types and side effects preserved?\n"
            f"   **Quality Improvement**:\n"
            f"   - Which original issues (from analysis.md) were addressed?\n"
            f"   - Estimate quality score improvement (original vs refactored, 1-10)\n"
            f"   - Are there any remaining issues or new issues introduced?\n"
            f"   **Coverage**:\n"
            f"   - What percentage of HIGH-priority issues were resolved?\n"
            f"   - What percentage of MEDIUM-priority issues were resolved?\n"
            f"4. Write `VERIFICATION_REPORT.md` with:\n"
            f"   - **Verdict**: PASS / FAIL / PARTIAL\n"
            f"   - **Behavior Preservation**: confirmed / issues found\n"
            f"   - **Quality Score**: original X/10 → refactored Y/10\n"
            f"   - **Issues Resolved**: N of M high-priority, N of M medium-priority\n"
            f"   - **Remaining Issues**: any unaddressed items\n"
            f"   - **Recommendations**: next steps if any\n"
            f"5. Store in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_py_write(verification_key, 'VERIFICATION_REPORT.md')}\n"
            f"   ```\n"
            f"6. Call /task-complete with: 'Verification complete — Verdict: PASS/FAIL/PARTIAL, quality X/10 → Y/10'\n"
        )

        # Submit tasks: analyzer → refactorer → verifier (sequential pipeline)
        task_analyzer = await orchestrator.submit_task(
            prompt=analyzer_prompt,
            required_tags=body.analyzer_tags or None,
            timeout=body.agent_timeout,
        )
        task_refactorer = await orchestrator.submit_task(
            prompt=refactorer_prompt,
            required_tags=body.refactorer_tags or None,
            depends_on=[task_analyzer.id],
            timeout=body.agent_timeout,
        )
        task_verifier = await orchestrator.submit_task(
            prompt=verifier_prompt,
            required_tags=body.verifier_tags or None,
            depends_on=[task_refactorer.id],
            timeout=body.agent_timeout,
            reply_to=body.reply_to,
        )

        task_ids = {
            "analyzer": task_analyzer.id,
            "refactorer": task_refactorer.id,
            "verifier": task_verifier.id,
        }

        all_ids = list(task_ids.values())
        run = wm.submit(name=wf_name, task_ids=all_ids)

        return {
            "workflow_id": run.id,
            "name": run.name,
            "task_ids": task_ids,
            "scratchpad_prefix": prefix,
            "refactor_goals": body.refactor_goals,
        }

    # ------------------------------------------------------------------
    # POST /workflows/from-template — YAML-driven workflow execution
    # (GET /workflows/templates is registered above, before /workflows/{id})
    # Design reference: DESIGN.md §10.103 (v1.2.28)
    # ------------------------------------------------------------------

    @router.post(
        "/workflows/from-template",
        summary="Submit a workflow from a YAML template",
        dependencies=[Depends(auth)],
    )
    async def submit_workflow_from_template(
        body: WorkflowFromTemplateSubmit,
    ) -> dict:
        """Load a phase-based YAML template, substitute variables, and submit.

        This endpoint lets operators add new multi-agent workflows by writing
        a YAML file — no Python code changes are required.

        **Template resolution order:**
        1. ``{templates_dir}/{template}.yaml``
        2. ``{templates_dir}/generic/{template}.yaml``

        **Variable substitution:**
        All ``{variable}`` placeholders in phase ``name``, ``context``, and
        string list elements are replaced with the values from ``variables``.
        Required variables (declared in the template's ``variables:`` section)
        must all be provided; omitting one raises HTTP 422.

        **Rendering pipeline:**
        Template → variable substitution → ``WorkflowSubmit`` (phases mode) →
        phase expansion → DAG validation → task submission.

        Returns the same response structure as ``POST /workflows``:
        - ``workflow_id``: UUID for status polling
        - ``name``: rendered workflow name
        - ``task_ids``: ``local_id → global_task_id`` mapping
        - ``template``: the template identifier that was used

        Design references:
        - Argo Workflows parameters and WorkflowTemplates
          https://argo-workflows.readthedocs.io/en/latest/workflow-templates/
        - Azure Pipelines template parameters
          https://learn.microsoft.com/en-us/azure/devops/pipelines/process/templates
        - Python ``str.format_map()`` — lightweight stdlib substitution
        - GitHub Actions YAML anchors and workflow templates (changelog 2025-09-18)
        - DESIGN.md §10.103 (v1.2.28)
        """
        from tmux_orchestrator.infrastructure.workflow_loader import (  # noqa: PLC0415
            load_workflow_template,
            render_template,
        )

        # --- Load template ---
        try:
            tmpl = load_workflow_template(body.template, _templates_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except (ValueError, ImportError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        # --- Render template with variable substitution ---
        try:
            workflow_dict = render_template(
                tmpl,
                body.variables,
                agent_timeout=body.agent_timeout,
                priority=body.priority,
                reply_to=body.reply_to,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        # --- Parse into WorkflowSubmit and delegate to the core handler ---
        try:
            workflow_submit = WorkflowSubmit.model_validate(workflow_dict)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Rendered template failed schema validation: {exc}",
            )

        # Delegate to the core submit_workflow handler.
        # We call it directly rather than re-posting to /workflows so that
        # we stay in-process and share the same scratchpad / orchestrator state.
        # submit_workflow is an inner async function captured by this closure.
        result: dict = await submit_workflow(workflow_submit)  # type: ignore[arg-type]
        result["template"] = body.template
        return result

    return router
