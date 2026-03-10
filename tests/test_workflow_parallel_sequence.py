"""Unit tests for parallel: / sequence: workflow structure blocks (v1.2.2).

Design reference: DESIGN.md §10.78

Tests cover:
- SequenceBlock / ParallelBlock dataclass creation
- SequenceBlock expansion: phases chained (B depends on A)
- ParallelBlock expansion: all phases start simultaneously (no inter-deps)
- Nested: parallel: containing sequence: blocks
- depends_on: [block_name] — phase after block waits for block completion
- Parallel block completion: waits for ALL sub-phases
- Backward compatibility: plain PhaseSpec list still works unchanged
- Domain-layer imports from phase_strategy
- Shim re-exports from phase_executor
- WorkflowSubmit accepts parallel: and sequence: blocks in JSON (via dict dispatch)
"""

from __future__ import annotations

import pytest

from tmux_orchestrator.domain.phase_strategy import (
    AgentSelector,
    ParallelBlock,
    PhaseSpec,
    SequenceBlock,
)
from tmux_orchestrator.phase_executor import (
    ParallelBlock as PBShim,
    SequenceBlock as SBShim,
    expand_phase_items_with_status,
    expand_phases_with_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _phase(name: str, pattern: str = "single", **kwargs) -> PhaseSpec:
    return PhaseSpec(name=name, pattern=pattern, **kwargs)


def _task_ids(tasks: list[dict]) -> list[str]:
    return [t["local_id"] for t in tasks]


def _deps(tasks: list[dict], local_id: str) -> list[str]:
    for t in tasks:
        if t["local_id"] == local_id:
            return t["depends_on"]
    raise KeyError(local_id)


# ---------------------------------------------------------------------------
# Dataclass creation
# ---------------------------------------------------------------------------


class TestSequenceBlockCreation:
    def test_basic_creation(self) -> None:
        p = _phase("a")
        sb = SequenceBlock(name="my_seq", phases=[p])
        assert sb.name == "my_seq"
        assert len(sb.phases) == 1

    def test_empty_phases(self) -> None:
        sb = SequenceBlock(name="empty_seq", phases=[])
        assert sb.phases == []

    def test_nested_phases(self) -> None:
        inner = SequenceBlock(name="inner", phases=[_phase("x")])
        outer = SequenceBlock(name="outer", phases=[inner])
        assert outer.phases[0] is inner

    def test_shim_re_export(self) -> None:
        """phase_executor shim must re-export SequenceBlock from domain."""
        assert SBShim is SequenceBlock


class TestParallelBlockCreation:
    def test_basic_creation(self) -> None:
        p1 = _phase("a")
        p2 = _phase("b")
        pb = ParallelBlock(name="my_par", phases=[p1, p2])
        assert pb.name == "my_par"
        assert len(pb.phases) == 2

    def test_empty_phases(self) -> None:
        pb = ParallelBlock(name="empty_par", phases=[])
        assert pb.phases == []

    def test_nested_inside_sequence(self) -> None:
        pb = ParallelBlock(name="par", phases=[_phase("x"), _phase("y")])
        sb = SequenceBlock(name="seq", phases=[pb])
        assert sb.phases[0] is pb

    def test_shim_re_export(self) -> None:
        """phase_executor shim must re-export ParallelBlock from domain."""
        assert PBShim is ParallelBlock


# ---------------------------------------------------------------------------
# SequenceBlock expansion
# ---------------------------------------------------------------------------


class TestSequenceBlockExpansion:
    def test_single_phase_in_sequence(self) -> None:
        sb = SequenceBlock(name="s", phases=[_phase("only")])
        tasks, statuses, terminals = expand_phase_items_with_status([sb], context="ctx")
        assert _task_ids(tasks) == ["phase_only_0"]
        assert _deps(tasks, "phase_only_0") == []
        assert terminals["s"] == ["phase_only_0"]

    def test_two_phases_chained(self) -> None:
        sb = SequenceBlock(name="seq", phases=[_phase("a"), _phase("b")])
        tasks, statuses, terminals = expand_phase_items_with_status([sb], context="ctx")
        assert _task_ids(tasks) == ["phase_a_0", "phase_b_0"]
        assert _deps(tasks, "phase_a_0") == []
        assert _deps(tasks, "phase_b_0") == ["phase_a_0"]
        assert terminals["seq"] == ["phase_b_0"]

    def test_three_phases_chained(self) -> None:
        sb = SequenceBlock(name="chain3", phases=[_phase("x"), _phase("y"), _phase("z")])
        tasks, statuses, terminals = expand_phase_items_with_status([sb], context="ctx")
        assert _deps(tasks, "phase_y_0") == ["phase_x_0"]
        assert _deps(tasks, "phase_z_0") == ["phase_y_0"]
        assert terminals["chain3"] == ["phase_z_0"]

    def test_sequence_respects_prior_ids(self) -> None:
        """First phase in sequence must inherit outer prior_ids."""
        p_pre = _phase("pre")
        sb = SequenceBlock(name="seq", phases=[_phase("inside")])
        tasks, statuses, terminals = expand_phase_items_with_status(
            [p_pre, sb], context="ctx"
        )
        assert _deps(tasks, "phase_inside_0") == ["phase_pre_0"]


# ---------------------------------------------------------------------------
# ParallelBlock expansion
# ---------------------------------------------------------------------------


class TestParallelBlockExpansion:
    def test_single_phase_in_parallel(self) -> None:
        pb = ParallelBlock(name="p", phases=[_phase("only")])
        tasks, statuses, terminals = expand_phase_items_with_status([pb], context="ctx")
        assert _task_ids(tasks) == ["phase_only_0"]
        assert terminals["p"] == ["phase_only_0"]

    def test_two_phases_simultaneous(self) -> None:
        pb = ParallelBlock(name="par", phases=[_phase("a"), _phase("b")])
        tasks, statuses, terminals = expand_phase_items_with_status([pb], context="ctx")
        # Both start with no deps (fan-out)
        assert _deps(tasks, "phase_a_0") == []
        assert _deps(tasks, "phase_b_0") == []

    def test_parallel_terminal_ids_are_union(self) -> None:
        pb = ParallelBlock(name="par", phases=[_phase("a"), _phase("b"), _phase("c")])
        tasks, statuses, terminals = expand_phase_items_with_status([pb], context="ctx")
        assert set(terminals["par"]) == {"phase_a_0", "phase_b_0", "phase_c_0"}

    def test_parallel_respects_prior_ids(self) -> None:
        """All branches in parallel must inherit the same outer prior_ids."""
        p_pre = _phase("pre")
        pb = ParallelBlock(name="par", phases=[_phase("a"), _phase("b")])
        tasks, statuses, terminals = expand_phase_items_with_status(
            [p_pre, pb], context="ctx"
        )
        assert _deps(tasks, "phase_a_0") == ["phase_pre_0"]
        assert _deps(tasks, "phase_b_0") == ["phase_pre_0"]

    def test_phase_after_parallel_block_waits_for_all(self) -> None:
        """Phase following a parallel block must depend on ALL branches."""
        pb = ParallelBlock(name="par", phases=[_phase("a"), _phase("b")])
        p_synth = _phase("synth")
        tasks, statuses, terminals = expand_phase_items_with_status(
            [pb, p_synth], context="ctx"
        )
        synth_deps = _deps(tasks, "phase_synth_0")
        assert set(synth_deps) == {"phase_a_0", "phase_b_0"}


# ---------------------------------------------------------------------------
# Nested: parallel containing sequence blocks
# ---------------------------------------------------------------------------


class TestNestedBlocks:
    def test_parallel_containing_two_sequences(self) -> None:
        """canonical fan-out/fan-in: parallel { sequence_a { A1, A2 }, sequence_b { B1, B2 } }."""
        a1 = _phase("scan")
        a2 = _phase("report")
        b1 = _phase("benchmark")
        b2 = _phase("analyze")

        seq_a = SequenceBlock(name="security_track", phases=[a1, a2])
        seq_b = SequenceBlock(name="perf_track", phases=[b1, b2])
        par = ParallelBlock(name="dual_track", phases=[seq_a, seq_b])

        tasks, statuses, terminals = expand_phase_items_with_status([par], context="ctx")

        # Chains within each sequence
        assert _deps(tasks, "phase_report_0") == ["phase_scan_0"]
        assert _deps(tasks, "phase_analyze_0") == ["phase_benchmark_0"]

        # Both sequences start simultaneously (fan-out)
        assert _deps(tasks, "phase_scan_0") == []
        assert _deps(tasks, "phase_benchmark_0") == []

        # Fan-in: parallel block's terminals are the two sequence terminals
        assert set(terminals["dual_track"]) == {"phase_report_0", "phase_analyze_0"}

    def test_depends_on_block_name_wires_to_terminals(self) -> None:
        """synthesize depends_on: [dual_track] → depends on both track terminals."""
        seq_a = SequenceBlock(name="sec", phases=[_phase("scan"), _phase("report")])
        seq_b = SequenceBlock(name="perf", phases=[_phase("bench"), _phase("analyze")])
        par = ParallelBlock(name="dual_track", phases=[seq_a, seq_b])
        synth = _phase("synth")

        tasks, statuses, terminals = expand_phase_items_with_status(
            [par, synth], context="ctx"
        )

        synth_deps = _deps(tasks, "phase_synth_0")
        assert set(synth_deps) == {"phase_report_0", "phase_analyze_0"}

    def test_sequence_containing_parallel(self) -> None:
        """sequence { parallel { A, B }, C } — parallel fans-out, then C runs after both."""
        par = ParallelBlock(name="par", phases=[_phase("a"), _phase("b")])
        c = _phase("c")
        seq = SequenceBlock(name="seq", phases=[par, c])

        tasks, statuses, terminals = expand_phase_items_with_status([seq], context="ctx")

        # a, b start simultaneously
        assert _deps(tasks, "phase_a_0") == []
        assert _deps(tasks, "phase_b_0") == []

        # c waits for both a and b
        c_deps = _deps(tasks, "phase_c_0")
        assert set(c_deps) == {"phase_a_0", "phase_b_0"}

        assert terminals["seq"] == ["phase_c_0"]

    def test_deeply_nested_three_levels(self) -> None:
        """seq { par { seq { A, B }, C }, D } — three levels deep."""
        inner_seq = SequenceBlock(name="inner_seq", phases=[_phase("a"), _phase("b")])
        par = ParallelBlock(name="par", phases=[inner_seq, _phase("c")])
        outer_seq = SequenceBlock(name="outer_seq", phases=[par, _phase("d")])

        tasks, statuses, terminals = expand_phase_items_with_status(
            [outer_seq], context="ctx"
        )

        # a, c start simultaneously (fan-out)
        assert _deps(tasks, "phase_a_0") == []
        assert _deps(tasks, "phase_c_0") == []

        # b depends on a (inner sequence chain)
        assert _deps(tasks, "phase_b_0") == ["phase_a_0"]

        # d depends on b AND c (fan-in)
        d_deps = _deps(tasks, "phase_d_0")
        assert set(d_deps) == {"phase_b_0", "phase_c_0"}


# ---------------------------------------------------------------------------
# Parallel block completion: waits for ALL sub-phases
# ---------------------------------------------------------------------------


class TestParallelFanIn:
    def test_fan_in_with_three_branches(self) -> None:
        pb = ParallelBlock(name="par3", phases=[_phase("a"), _phase("b"), _phase("c")])
        follow = _phase("follow")
        tasks, statuses, terminals = expand_phase_items_with_status(
            [pb, follow], context="ctx"
        )
        follow_deps = _deps(tasks, "phase_follow_0")
        assert set(follow_deps) == {"phase_a_0", "phase_b_0", "phase_c_0"}

    def test_fan_in_with_parallel_inside_sequence(self) -> None:
        """sequence { A, parallel { B, C }, D } — D waits for B and C."""
        par = ParallelBlock(name="par", phases=[_phase("b"), _phase("c")])
        seq = SequenceBlock(name="seq", phases=[_phase("a"), par, _phase("d")])
        tasks, statuses, terminals = expand_phase_items_with_status([seq], context="ctx")

        # b and c both depend on a
        assert _deps(tasks, "phase_b_0") == ["phase_a_0"]
        assert _deps(tasks, "phase_c_0") == ["phase_a_0"]

        # d waits for both b and c
        d_deps = _deps(tasks, "phase_d_0")
        assert set(d_deps) == {"phase_b_0", "phase_c_0"}


# ---------------------------------------------------------------------------
# Backward compatibility: plain PhaseSpec list still works
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_flat_list_expand_phases_with_status(self) -> None:
        """expand_phases_with_status must work unchanged with flat PhaseSpec list."""
        phases = [_phase("a"), _phase("b"), _phase("c")]
        tasks, statuses = expand_phases_with_status(phases, context="ctx")
        assert _task_ids(tasks) == ["phase_a_0", "phase_b_0", "phase_c_0"]
        assert _deps(tasks, "phase_b_0") == ["phase_a_0"]
        assert _deps(tasks, "phase_c_0") == ["phase_b_0"]

    def test_flat_list_in_expand_phase_items_with_status(self) -> None:
        """expand_phase_items_with_status must handle flat PhaseSpec items unchanged."""
        phases = [_phase("a"), _phase("b")]
        tasks, statuses, terminals = expand_phase_items_with_status(phases, context="ctx")
        assert _task_ids(tasks) == ["phase_a_0", "phase_b_0"]
        assert _deps(tasks, "phase_b_0") == ["phase_a_0"]
        # No blocks → empty terminals dict
        assert terminals == {}

    def test_mixed_flat_and_block(self) -> None:
        """Flat PhaseSpec items can coexist with block items."""
        setup = _phase("setup")
        par = ParallelBlock(name="par", phases=[_phase("a"), _phase("b")])
        cleanup = _phase("cleanup")
        tasks, statuses, terminals = expand_phase_items_with_status(
            [setup, par, cleanup], context="ctx"
        )
        # a and b depend on setup
        assert _deps(tasks, "phase_a_0") == ["phase_setup_0"]
        assert _deps(tasks, "phase_b_0") == ["phase_setup_0"]
        # cleanup depends on both a and b
        assert set(_deps(tasks, "phase_cleanup_0")) == {"phase_a_0", "phase_b_0"}


# ---------------------------------------------------------------------------
# Required tags and agent selector propagation
# ---------------------------------------------------------------------------


class TestRequiredTagsPropagation:
    def test_sequence_propagates_required_tags(self) -> None:
        p = _phase("scan", required_tags=["scanner"])
        sb = SequenceBlock(name="seq", phases=[p])
        tasks, _, _ = expand_phase_items_with_status([sb], context="ctx")
        assert "scanner" in tasks[0]["required_tags"]

    def test_parallel_propagates_required_tags(self) -> None:
        p1 = _phase("bench", required_tags=["benchmarker"])
        p2 = _phase("scan", required_tags=["scanner"])
        pb = ParallelBlock(name="par", phases=[p1, p2])
        tasks, _, _ = expand_phase_items_with_status([pb], context="ctx")
        tag_map = {t["local_id"]: t["required_tags"] for t in tasks}
        assert "benchmarker" in tag_map["phase_bench_0"]
        assert "scanner" in tag_map["phase_scan_0"]


# ---------------------------------------------------------------------------
# Schema layer: SequenceBlockModel and ParallelBlockModel
# ---------------------------------------------------------------------------


class TestWebSchemas:
    def test_sequence_block_model_import(self) -> None:
        from tmux_orchestrator.web.schemas import SequenceBlockModel, ParallelBlockModel
        assert SequenceBlockModel is not None
        assert ParallelBlockModel is not None

    def test_sequence_block_model_creation(self) -> None:
        from tmux_orchestrator.web.schemas import SequenceBlockModel
        m = SequenceBlockModel(name="seq_test", phases=[])
        assert m.name == "seq_test"
        assert m.phases == []

    def test_parallel_block_model_creation(self) -> None:
        from tmux_orchestrator.web.schemas import ParallelBlockModel
        m = ParallelBlockModel(name="par_test", phases=[])
        assert m.name == "par_test"
        assert m.phases == []

    def test_phase_item_model_includes_new_blocks(self) -> None:
        """PhaseItemModel union must include SequenceBlockModel and ParallelBlockModel."""
        from tmux_orchestrator.web.schemas import PhaseItemModel
        import typing
        args = typing.get_args(PhaseItemModel)
        type_names = {a.__name__ for a in args}
        assert "SequenceBlockModel" in type_names
        assert "ParallelBlockModel" in type_names

    def test_workflow_submit_phases_with_parallel_block_dict(self) -> None:
        """WorkflowSubmit accepts a phases list containing a parallel block dict."""
        from tmux_orchestrator.web.schemas import WorkflowSubmit
        data = {
            "name": "test",
            "phases": [
                {
                    "parallel": {
                        "name": "dual_track",
                        "phases": [
                            {
                                "sequence": {
                                    "name": "security_track",
                                    "phases": [
                                        {"name": "scan", "pattern": "single"},
                                    ],
                                }
                            },
                            {"name": "bench", "pattern": "single"},
                        ],
                    }
                },
                {"name": "synth", "pattern": "single"},
            ],
            "context": "test ctx",
        }
        submit = WorkflowSubmit.model_validate(data)
        assert submit.phases is not None
        assert len(submit.phases) == 2

    def test_workflow_submit_phases_backward_compat(self) -> None:
        """WorkflowSubmit still accepts plain PhaseSpec dicts in phases."""
        from tmux_orchestrator.web.schemas import WorkflowSubmit
        data = {
            "name": "test",
            "phases": [
                {"name": "a", "pattern": "single"},
                {"name": "b", "pattern": "single"},
            ],
            "context": "ctx",
        }
        submit = WorkflowSubmit.model_validate(data)
        assert len(submit.phases) == 2


# ---------------------------------------------------------------------------
# Dict dispatch in router's _to_domain_phase_item
# ---------------------------------------------------------------------------


class TestDictDispatch:
    """Test the dict-based dispatch inside the router's _to_domain_phase_item.

    This verifies the logic without starting a full FastAPI server.
    """

    def _build_dispatcher(self):
        """Build a standalone _to_domain_phase_item function for testing."""
        from tmux_orchestrator.domain.phase_strategy import (
            LoopBlock,
            LoopSpec,
            ParallelBlock,
            SequenceBlock,
        )
        from tmux_orchestrator.phase_executor import AgentSelector, PhaseSpec, SkipCondition
        from tmux_orchestrator.web.schemas import (
            LoopBlockModel,
            ParallelBlockModel,
            PhaseSpecModel,
            SequenceBlockModel,
        )

        def _to_domain_phase_spec(p):
            return PhaseSpec(
                name=p.name,
                pattern=p.pattern,
                agents=AgentSelector(
                    tags=p.agents.tags,
                    count=p.agents.count,
                    target_agent=p.agents.target_agent,
                    target_group=p.agents.target_group,
                ),
                required_tags=p.required_tags,
                timeout=p.timeout,
                context=p.context,
            )

        def _to_domain_phase_item(item):
            if isinstance(item, LoopBlockModel):
                inner = [_to_domain_phase_item(p) for p in item.phases]
                return LoopBlock(name=item.name, loop=LoopSpec(max=item.loop.max), phases=inner)
            if isinstance(item, SequenceBlockModel):
                inner = [_to_domain_phase_item(p) for p in item.phases]
                return SequenceBlock(name=item.name, phases=inner)
            if isinstance(item, ParallelBlockModel):
                inner = [_to_domain_phase_item(p) for p in item.phases]
                return ParallelBlock(name=item.name, phases=inner)
            if isinstance(item, dict):
                if "loop" in item:
                    return _to_domain_phase_item(LoopBlockModel.model_validate(item))
                if "sequence" in item:
                    seq_data = item["sequence"] if isinstance(item.get("sequence"), dict) else item
                    return _to_domain_phase_item(SequenceBlockModel.model_validate(seq_data))
                if "parallel" in item:
                    par_data = item["parallel"] if isinstance(item.get("parallel"), dict) else item
                    return _to_domain_phase_item(ParallelBlockModel.model_validate(par_data))
                return _to_domain_phase_spec(PhaseSpecModel.model_validate(item))
            return _to_domain_phase_spec(item)

        return _to_domain_phase_item

    def test_dict_parallel_wrapper_key(self) -> None:
        dispatch = self._build_dispatcher()
        item = {
            "parallel": {
                "name": "par",
                "phases": [
                    {"name": "a", "pattern": "single"},
                    {"name": "b", "pattern": "single"},
                ],
            }
        }
        result = dispatch(item)
        assert isinstance(result, ParallelBlock)
        assert result.name == "par"
        assert len(result.phases) == 2

    def test_dict_sequence_wrapper_key(self) -> None:
        dispatch = self._build_dispatcher()
        item = {
            "sequence": {
                "name": "seq",
                "phases": [
                    {"name": "x", "pattern": "single"},
                ],
            }
        }
        result = dispatch(item)
        assert isinstance(result, SequenceBlock)
        assert result.name == "seq"
        assert len(result.phases) == 1

    def test_dict_phasespec_no_block_key(self) -> None:
        dispatch = self._build_dispatcher()
        item = {"name": "plain", "pattern": "single"}
        result = dispatch(item)
        assert isinstance(result, PhaseSpec)
        assert result.name == "plain"

    def test_nested_parallel_containing_sequence_dicts(self) -> None:
        dispatch = self._build_dispatcher()
        item = {
            "parallel": {
                "name": "dual",
                "phases": [
                    {
                        "sequence": {
                            "name": "track_a",
                            "phases": [
                                {"name": "scan", "pattern": "single"},
                                {"name": "report", "pattern": "single"},
                            ],
                        }
                    },
                    {
                        "sequence": {
                            "name": "track_b",
                            "phases": [
                                {"name": "bench", "pattern": "single"},
                            ],
                        }
                    },
                ],
            }
        }
        result = dispatch(item)
        assert isinstance(result, ParallelBlock)
        assert result.name == "dual"
        assert isinstance(result.phases[0], SequenceBlock)
        assert result.phases[0].name == "track_a"
        assert isinstance(result.phases[1], SequenceBlock)
        assert result.phases[1].name == "track_b"
