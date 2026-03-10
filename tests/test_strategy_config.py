"""Tests for StrategyConfig value objects and PhaseSpec.timeout.

v1.1.31 — StrategyConfig typed parameters + per-phase timeout.

Design references:
- DESIGN.md §10.63 (v1.1.31)
- Pydantic discriminated unions: https://docs.pydantic.dev/latest/concepts/unions/
- Temporal per-activity timeout: https://temporal.io/blog/orchestrating-ambient-agents-with-temporal
"""

from __future__ import annotations

import pytest

from tmux_orchestrator.domain.phase_strategy import (
    AgentSelector,
    CompetitiveConfig,
    DebateConfig,
    ParallelConfig,
    PhaseSpec,
    SingleConfig,
    StrategyConfig,
    expand_phases_from_specs,
)
from tmux_orchestrator.phase_executor import (
    expand_phases,
    expand_phases_with_status,
)


# ---------------------------------------------------------------------------
# SingleConfig
# ---------------------------------------------------------------------------


def test_single_config_type_literal():
    cfg = SingleConfig()
    assert cfg.type == "single"


def test_single_config_is_strategy_config():
    """SingleConfig satisfies StrategyConfig type."""
    cfg: StrategyConfig = SingleConfig()
    assert cfg.type == "single"


# ---------------------------------------------------------------------------
# ParallelConfig
# ---------------------------------------------------------------------------


def test_parallel_config_defaults():
    cfg = ParallelConfig()
    assert cfg.type == "parallel"
    assert cfg.merge_strategy == "collect"


def test_parallel_config_custom():
    cfg = ParallelConfig(merge_strategy="first_wins")
    assert cfg.merge_strategy == "first_wins"


def test_parallel_config_invalid_merge():
    with pytest.raises(ValueError):
        ParallelConfig(merge_strategy="invalid")


# ---------------------------------------------------------------------------
# CompetitiveConfig
# ---------------------------------------------------------------------------


def test_competitive_config_defaults():
    cfg = CompetitiveConfig()
    assert cfg.type == "competitive"
    assert cfg.scorer == "llm_judge"
    assert cfg.top_k == 1
    assert cfg.timeout_per_agent is None


def test_competitive_config_custom():
    cfg = CompetitiveConfig(scorer="test_score", top_k=3, timeout_per_agent=300)
    assert cfg.scorer == "test_score"
    assert cfg.top_k == 3
    assert cfg.timeout_per_agent == 300


def test_competitive_config_top_k_positive():
    with pytest.raises(ValueError):
        CompetitiveConfig(top_k=0)


# ---------------------------------------------------------------------------
# DebateConfig
# ---------------------------------------------------------------------------


def test_debate_config_defaults():
    cfg = DebateConfig()
    assert cfg.type == "debate"
    assert cfg.rounds == 1
    assert cfg.require_consensus is False
    assert cfg.judge_criteria == ""


def test_debate_config_custom():
    cfg = DebateConfig(rounds=3, require_consensus=True, judge_criteria="correctness, brevity")
    assert cfg.rounds == 3
    assert cfg.require_consensus is True
    assert cfg.judge_criteria == "correctness, brevity"


def test_debate_config_rounds_positive():
    with pytest.raises(ValueError):
        DebateConfig(rounds=0)


# ---------------------------------------------------------------------------
# StrategyConfig — Union of all configs
# ---------------------------------------------------------------------------


def test_strategy_config_single_instance():
    cfg: StrategyConfig = SingleConfig()
    assert cfg.type == "single"


def test_strategy_config_parallel_instance():
    cfg: StrategyConfig = ParallelConfig(merge_strategy="first_wins")
    assert cfg.type == "parallel"
    assert cfg.merge_strategy == "first_wins"


def test_strategy_config_competitive_instance():
    cfg: StrategyConfig = CompetitiveConfig(top_k=2)
    assert cfg.type == "competitive"
    assert cfg.top_k == 2


def test_strategy_config_debate_instance():
    cfg: StrategyConfig = DebateConfig(rounds=2)
    assert cfg.type == "debate"
    assert cfg.rounds == 2


# ---------------------------------------------------------------------------
# PhaseSpec.strategy_config field
# ---------------------------------------------------------------------------


def test_phase_spec_strategy_config_none_by_default():
    spec = PhaseSpec(name="impl", pattern="single")
    assert spec.strategy_config is None


def test_phase_spec_accepts_competitive_config():
    cfg = CompetitiveConfig(top_k=2, timeout_per_agent=120)
    spec = PhaseSpec(name="solve", pattern="competitive", strategy_config=cfg)
    assert spec.strategy_config is not None
    assert isinstance(spec.strategy_config, CompetitiveConfig)
    assert spec.strategy_config.top_k == 2


def test_phase_spec_accepts_debate_config():
    cfg = DebateConfig(rounds=2, judge_criteria="accuracy")
    spec = PhaseSpec(name="debate", pattern="debate", strategy_config=cfg)
    assert isinstance(spec.strategy_config, DebateConfig)
    assert spec.strategy_config.rounds == 2


# ---------------------------------------------------------------------------
# PhaseSpec.timeout field
# ---------------------------------------------------------------------------


def test_phase_spec_timeout_default_none():
    spec = PhaseSpec(name="impl", pattern="single")
    assert spec.timeout is None


def test_phase_spec_timeout_set():
    spec = PhaseSpec(name="impl", pattern="single", timeout=600)
    assert spec.timeout == 600


# ---------------------------------------------------------------------------
# expand_phases propagates timeout to task specs
# ---------------------------------------------------------------------------


def test_expand_phases_single_with_timeout():
    spec = PhaseSpec(name="impl", pattern="single", timeout=600)
    tasks = expand_phases([spec], context="do something")
    assert len(tasks) == 1
    assert tasks[0].get("timeout") == 600


def test_expand_phases_single_no_timeout():
    spec = PhaseSpec(name="impl", pattern="single")
    tasks = expand_phases([spec], context="do something")
    assert len(tasks) == 1
    # When timeout is None, task spec should not contain the key OR it should be None
    assert tasks[0].get("timeout") is None


def test_expand_phases_parallel_with_timeout():
    spec = PhaseSpec(name="impl", pattern="parallel", agents=AgentSelector(count=3), timeout=300)
    tasks = expand_phases([spec], context="do work")
    assert len(tasks) == 3
    for t in tasks:
        assert t.get("timeout") == 300


def test_expand_phases_competitive_with_timeout():
    spec = PhaseSpec(name="solve", pattern="competitive", agents=AgentSelector(count=2), timeout=900)
    tasks = expand_phases([spec], context="solve problem")
    assert len(tasks) == 2
    for t in tasks:
        assert t.get("timeout") == 900


def test_expand_phases_debate_with_timeout():
    spec = PhaseSpec(name="argue", pattern="debate", debate_rounds=1, timeout=450)
    tasks = expand_phases([spec], context="debate topic")
    # debate: advocate_r1 + critic_r1 + judge = 3
    assert len(tasks) == 3
    for t in tasks:
        assert t.get("timeout") == 450


def test_expand_phases_different_timeouts_per_phase():
    phases = [
        PhaseSpec(name="research", pattern="single", timeout=1200),
        PhaseSpec(name="implement", pattern="single", timeout=600),
    ]
    tasks = expand_phases(phases, context="build feature")
    assert len(tasks) == 2
    assert tasks[0].get("timeout") == 1200
    assert tasks[1].get("timeout") == 600


def test_expand_phases_with_status_timeout_propagated():
    spec = PhaseSpec(name="impl", pattern="parallel", agents=AgentSelector(count=2), timeout=300)
    tasks, statuses = expand_phases_with_status([spec], context="work")
    assert len(tasks) == 2
    for t in tasks:
        assert t.get("timeout") == 300
    assert len(statuses) == 1


# ---------------------------------------------------------------------------
# expand_phases_from_specs (new canonical function in domain)
# ---------------------------------------------------------------------------


def test_expand_phases_from_specs_basic():
    phases = [PhaseSpec(name="a", pattern="single")]
    tasks = expand_phases_from_specs(phases, context="ctx")
    assert len(tasks) == 1


def test_expand_phases_from_specs_timeout():
    phases = [PhaseSpec(name="slow", pattern="single", timeout=999)]
    tasks = expand_phases_from_specs(phases, context="ctx")
    assert tasks[0].get("timeout") == 999
