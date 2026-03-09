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
    CleanArchWorkflowSubmit,
    CompetitionWorkflowSubmit,
    DDDWorkflowSubmit,
    DebateWorkflowSubmit,
    DelphiWorkflowSubmit,
    FulldevWorkflowSubmit,
    MobReviewWorkflowSubmit,
    PairWorkflowSubmit,
    RedBlueWorkflowSubmit,
    SpecFirstWorkflowSubmit,
    SocraticWorkflowSubmit,
    TddWorkflowSubmit,
    WorkflowSubmit,
)


def build_workflows_router(
    orchestrator: Any,
    auth: Callable,
) -> APIRouter:
    """Build and return the workflows APIRouter.

    Parameters
    ----------
    orchestrator:
        The :class:`~tmux_orchestrator.orchestrator.Orchestrator` instance.
    auth:
        Authentication dependency callable (combined session + API key).
    """
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
            from tmux_orchestrator.phase_executor import (  # noqa: PLC0415
                AgentSelector,
                PhaseSpec,
                expand_phases_with_status,
            )
    
            run_id_prefix = uuid.uuid4().hex[:8]
            phase_specs: list[PhaseSpec] = []
            for p in body.phases:
                phase_specs.append(
                    PhaseSpec(
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
                    )
                )
    
            task_specs, phase_statuses = expand_phases_with_status(
                phase_specs,
                context=body.context,
                scratchpad_prefix=f"wf/{run_id_prefix}",
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
    
        for spec in ordered:
            global_deps = [local_to_global[lid] for lid in spec.get("depends_on", [])]
            task = await orchestrator.submit_task(
                spec["prompt"],
                priority=spec.get("priority", 0),
                depends_on=global_deps or None,
                target_agent=spec.get("target_agent"),
                required_tags=spec.get("required_tags") or None,
                target_group=spec.get("target_group"),
                max_retries=spec.get("max_retries", 0),
                inherit_priority=spec.get("inherit_priority", True),
                ttl=spec.get("ttl"),
            )
            local_to_global[spec["local_id"]] = task.id
            global_task_ids.append(task.id)
    
        # Register with WorkflowManager for status tracking
        wm = orchestrator.get_workflow_manager()
        run = wm.submit(name=body.name, task_ids=global_task_ids)
    
        # Attach phase status trackers to the workflow run (if phases were used)
        if phase_statuses is not None:
            # Remap local_id → global_task_id for each phase's task_ids
            for ps in phase_statuses:
                ps.task_ids = [local_to_global[lid] for lid in ps.task_ids]
            run.phases = phase_statuses
    
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
    
        proposal_key = _scratchpad_key("proposal")
        review_key = _scratchpad_key("review")
        decision_key = _scratchpad_key("decision")
    
        # --- Proposer prompt ---
        proposer_prompt = (
            f"You are the PROPOSER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            f"\n"
            f"Your task is to analyse the architectural decision and identify candidate options.\n"
            f"\n"
            f"Steps:\n"
            f"1. Identify 2-3 concrete options for addressing '{body.topic}'.\n"
            f"2. For each option, document:\n"
            f"   - Brief description\n"
            f"   - Pros (technical advantages, performance, maintainability, cost)\n"
            f"   - Cons (drawbacks, risks, operational complexity)\n"
            f"   - When this option is most appropriate\n"
            f"3. Write your analysis to `proposal.md` in your working directory.\n"
            f"4. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat proposal.md)\n"
            + _write_snippet(proposal_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be technical, specific, and objective. Do not recommend a winner yet — "
            f"that is the synthesizer's job. Max 500 words."
        )
    
        # --- Reviewer prompt ---
        reviewer_prompt = (
            f"You are the REVIEWER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            f"\n"
            f"Your task is to critically review the proposer's analysis.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the proposer's analysis from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(proposal_key, "PROPOSAL")
            + f"   echo \"Proposal: $PROPOSAL\"\n"
            f"   ```\n"
            f"2. Critically evaluate the proposal:\n"
            f"   - Are any options missing or underrepresented?\n"
            f"   - Are the pros/cons accurate and complete?\n"
            f"   - Are there hidden risks or biases in the analysis?\n"
            f"   - What additional decision drivers should be considered?\n"
            f"     (e.g. team expertise, operational burden, vendor lock-in, scalability)\n"
            f"3. Write your critique to `review.md` in your working directory.\n"
            f"4. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat review.md)\n"
            + _write_snippet(review_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be rigorous and independent. Do not simply agree with the proposer — "
            f"your role is to stress-test the analysis. Max 400 words."
        )
    
        # --- Synthesizer prompt ---
        synthesizer_prompt = (
            f"You are the SYNTHESIZER agent in an Architecture Decision Record (ADR) workflow.\n"
            f"\n"
            f"**ADR Topic:** {body.topic}\n"
            f"\n"
            f"Your task is to read the proposal and review, then produce a final "
            f"MADR-format DECISION.md.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read both artifacts from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(proposal_key, "PROPOSAL")
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
            + _write_snippet(decision_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Synthesize both the proposal and the reviewer's critique. "
            f"Choose the best option with clear rationale. "
            f"Acknowledge the reviewer's concerns in the Consequences section."
        )
    
        # Submit tasks in pipeline order
        proposer_task = await orchestrator.submit_task(
            proposer_prompt,
            required_tags=body.proposer_tags or None,
            depends_on=[],
        )
    
        reviewer_task = await orchestrator.submit_task(
            reviewer_prompt,
            required_tags=body.reviewer_tags or None,
            depends_on=[proposer_task.id],
        )
    
        synthesizer_task = await orchestrator.submit_task(
            synthesizer_prompt,
            required_tags=body.synthesizer_tags or None,
            depends_on=[reviewer_task.id],
            reply_to=body.reply_to,
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
        summary="Submit a Red Team / Blue Team adversarial evaluation workflow",
        dependencies=[Depends(auth)],
    )
    async def submit_redblue_workflow(body: RedBlueWorkflowSubmit) -> dict:
        """Submit a 3-agent adversarial evaluation Workflow DAG.
    
        Pipeline (strictly sequential):
    
          1. **blue_team**: designs or implements a solution for *topic*, stores
             result in ``{scratchpad_prefix}_blue_design``.
          2. **red_team**: reads blue_team output and attacks it — lists
             vulnerabilities, flaws, and risks; stores result in
             ``{scratchpad_prefix}_red_findings``.
          3. **arbiter**: reads both artifacts, produces a balanced risk
             assessment with prioritised recommendations; stores result in
             ``{scratchpad_prefix}_risk_report``.
    
        Returns:
        - ``workflow_id``: workflow run UUID for status polling
        - ``name``: human-readable name (``redblue/<topic>``)
        - ``task_ids``: dict with keys ``blue_team``, ``red_team``, ``arbiter``
        - ``scratchpad_prefix``: scratchpad namespace for this run
    
        Design references:
        - Harrasse et al. "D3" arXiv:2410.04663 (2026): adversarial multi-agent
          evaluation reduces bias and improves agreement with human judgments.
        - "Red-Teaming LLM MAS via Communication Attacks" ACL 2025 arXiv:2502.14847.
        - Farzulla "Autonomous Red Team and Blue Team AI" DAI-2513 (2025).
        - DESIGN.md §10.23 (v1.0.24)
        """

    
        wm = orchestrator.get_workflow_manager()
        wf_name = f"redblue/{body.topic}"
    
        pre_run_id = str(uuid.uuid4())
        scratchpad_prefix = f"redblue_{pre_run_id[:8]}"
    
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
    
        blue_design_key = _scratchpad_key("blue_design")
        red_findings_key = _scratchpad_key("red_findings")
        risk_report_key = _scratchpad_key("risk_report")
    
        # --- Blue-team prompt ---
        blue_prompt = (
            f"You are the BLUE-TEAM agent in a Red/Blue adversarial evaluation workflow.\n"
            f"\n"
            f"**Evaluation topic:** {body.topic}\n"
            f"\n"
            f"Your task is to produce a concrete design or implementation plan for the topic above.\n"
            f"\n"
            f"Steps:\n"
            f"1. Analyse the topic and design a concrete solution:\n"
            f"   - Describe the approach, architecture, or implementation plan.\n"
            f"   - Include key technical decisions with rationale.\n"
            f"   - Be specific: name technologies, patterns, or APIs you would use.\n"
            f"   - Address security, performance, and maintainability.\n"
            f"2. Write your design to `blue_design.md` in your working directory.\n"
            f"3. Store it in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            f"   CONTENT=$(cat blue_design.md)\n"
            + _write_snippet(blue_design_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be thorough and honest — the red-team will scrutinise your design. Max 500 words."
        )
    
        # --- Red-team prompt ---
        red_prompt = (
            f"You are the RED-TEAM agent in a Red/Blue adversarial evaluation workflow.\n"
            f"\n"
            f"**Evaluation topic:** {body.topic}\n"
            f"\n"
            f"Your role is to act as an adversary and find weaknesses in the blue-team design.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read the blue-team design from the shared scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(blue_design_key, "BLUE_DESIGN")
            + f"   echo \"Blue design: $BLUE_DESIGN\"\n"
            f"   ```\n"
            f"2. Attack the design rigorously from an adversarial perspective:\n"
            f"   - Identify security vulnerabilities (authentication, authorisation, injection, etc.).\n"
            f"   - Find scalability / reliability risks.\n"
            f"   - Spot missing error handling or edge cases.\n"
            f"   - Highlight hidden assumptions that may not hold in production.\n"
            f"   - Be specific: name exact weaknesses, not vague concerns.\n"
            f"   - Do NOT be constructive — your job is to list problems, not solutions.\n"
            f"3. Write your findings to `red_findings.md` in your working directory.\n"
            f"4. Store findings in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat red_findings.md)\n"
            + _write_snippet(red_findings_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be adversarial and thorough. Max 400 words."
        )
    
        # --- Arbiter prompt ---
        arbiter_prompt = (
            f"You are the ARBITER agent in a Red/Blue adversarial evaluation workflow.\n"
            f"\n"
            f"**Evaluation topic:** {body.topic}\n"
            f"\n"
            f"Your task is to read both the blue-team design and red-team findings,\n"
            f"then produce a balanced risk assessment report.\n"
            f"\n"
            f"Steps:\n"
            f"1. Read both artifacts from the scratchpad:\n"
            f"   ```bash\n"
            f"   {_ctx_snippet}\n"
            + _read_snippet(blue_design_key, "BLUE_DESIGN")
            + _read_snippet(red_findings_key, "RED_FINDINGS")
            + f"   ```\n"
            f"2. Write `risk_report.md` in your working directory with these sections:\n"
            f"   ```markdown\n"
            f"   # Risk Assessment: {body.topic}\n"
            f"   ## Blue-Team Design Summary\n"
            f"   ## Red-Team Findings Summary\n"
            f"   ## Risk Matrix\n"
            f"   | Risk | Severity (High/Med/Low) | Likelihood | Mitigation |\n"
            f"   |------|------------------------|------------|------------|\n"
            f"   ## Overall Risk Level\n"
            f"   ## Prioritised Recommendations\n"
            f"   ## Verdict\n"
            f"   ```\n"
            f"3. Store the report in the shared scratchpad:\n"
            f"   ```bash\n"
            f"   CONTENT=$(cat risk_report.md)\n"
            + _write_snippet(risk_report_key)
            + f"\n"
            f"   ```\n"
            f"\n"
            f"Be balanced and objective. Acknowledge both the blue-team's strengths and "
            f"the red-team's valid concerns. Prioritise recommendations by severity."
        )
    
        # Submit tasks in pipeline order
        blue_task = await orchestrator.submit_task(
            blue_prompt,
            required_tags=body.blue_tags or None,
            depends_on=[],
        )
    
        red_task = await orchestrator.submit_task(
            red_prompt,
            required_tags=body.red_tags or None,
            depends_on=[blue_task.id],
        )
    
        arbiter_task = await orchestrator.submit_task(
            arbiter_prompt,
            required_tags=body.arbiter_tags or None,
            depends_on=[red_task.id],
            reply_to=body.reply_to,
        )
    
        task_ids_map = {
            "blue_team": blue_task.id,
            "red_team": red_task.id,
            "arbiter": arbiter_task.id,
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

            reviewer_task = await orchestrator.submit_task(
                reviewer_prompt,
                required_tags=body.reviewer_tags or None,
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

    return router
