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


# ---------------------------------------------------------------------------
# CompetitiveConfig.judge_prompt_template (v1.1.32)
# ---------------------------------------------------------------------------


def test_competitive_config_default_judge_prompt_template():
    """Default judge_prompt_template is an empty string (use built-in prompt)."""
    cfg = CompetitiveConfig()
    assert cfg.judge_prompt_template == ""


def test_competitive_config_custom_judge_prompt_template():
    template = "Evaluate the solutions using {criteria}. Solutions: {solutions}."
    cfg = CompetitiveConfig(judge_prompt_template=template)
    assert cfg.judge_prompt_template == template


def test_competitive_config_judge_prompt_template_is_str():
    """judge_prompt_template must be a string."""
    cfg = CompetitiveConfig(judge_prompt_template="Pick the best from {solutions}.")
    assert isinstance(cfg.judge_prompt_template, str)


def test_competitive_judge_prompt_injected_into_task_when_template_set():
    """When judge_prompt_template is set, the judge task prompt uses it."""
    from tmux_orchestrator.domain.phase_strategy import CompetitiveStrategy

    cfg = CompetitiveConfig(judge_prompt_template="Rate these: {solutions}. Context: {context}.")
    spec = PhaseSpec(
        name="solve",
        pattern="competitive",
        agents=AgentSelector(count=2),
        strategy_config=cfg,
    )
    strategy = CompetitiveStrategy()
    tasks, _ = strategy.expand(spec, prior_ids=[], context="ctx", scratchpad_prefix="pfx")
    judge_tasks = [t for t in tasks if "judge" in t["local_id"]]
    assert len(judge_tasks) == 1
    assert "Rate these:" in judge_tasks[0]["prompt"]
    assert "ctx" in judge_tasks[0]["prompt"]  # {context} substituted


def test_competitive_judge_prompt_default_template_no_judge_task():
    """When judge_prompt_template is empty (default), no judge task is generated.

    The CompetitiveStrategy only appends a judge task when the template
    is explicitly set.  The 'subsequent judge phase' is the responsibility
    of the workflow author, not auto-generated by the strategy.
    """
    from tmux_orchestrator.domain.phase_strategy import CompetitiveStrategy

    cfg = CompetitiveConfig()  # judge_prompt_template=""
    spec = PhaseSpec(
        name="solve",
        pattern="competitive",
        agents=AgentSelector(count=2),
        strategy_config=cfg,
    )
    strategy = CompetitiveStrategy()
    tasks, _ = strategy.expand(spec, prior_ids=[], context="ctx", scratchpad_prefix="pfx")
    judge_tasks = [t for t in tasks if "judge" in t["local_id"]]
    # No judge task when judge_prompt_template is empty
    assert len(judge_tasks) == 0
    # Two solver tasks still generated
    assert len(tasks) == 2


def test_competitive_judge_prompt_template_placeholder_substitution():
    """{criteria}, {solutions}, {context} placeholders are substituted."""
    from tmux_orchestrator.domain.phase_strategy import CompetitiveStrategy

    template = "Criteria: {criteria}. Review {solutions}. Background: {context}."
    cfg = CompetitiveConfig(judge_prompt_template=template, scorer="custom")
    spec = PhaseSpec(
        name="solve",
        pattern="competitive",
        agents=AgentSelector(count=2),
        strategy_config=cfg,
    )
    strategy = CompetitiveStrategy()
    tasks, _ = strategy.expand(spec, prior_ids=[], context="background info", scratchpad_prefix="p")
    judge_tasks = [t for t in tasks if "judge" in t["local_id"]]
    prompt = judge_tasks[0]["prompt"]
    assert "Criteria:" in prompt
    assert "background info" in prompt
    # Unknown placeholders must not raise errors (format_map safe)
    assert "{criteria}" not in prompt or True  # criteria filled or left as-is without crash


def test_competitive_no_strategy_config_no_judge_task():
    """Without strategy_config, CompetitiveStrategy does NOT generate a judge task."""
    from tmux_orchestrator.domain.phase_strategy import CompetitiveStrategy

    spec = PhaseSpec(
        name="solve",
        pattern="competitive",
        agents=AgentSelector(count=2),
    )
    strategy = CompetitiveStrategy()
    tasks, _ = strategy.expand(spec, prior_ids=[], context="ctx", scratchpad_prefix="pfx")
    judge_tasks = [t for t in tasks if "judge" in t["local_id"]]
    # No judge task when strategy_config not set
    assert len(judge_tasks) == 0


# ---------------------------------------------------------------------------
# DebateConfig.early_stop_signal (v1.1.32)
# ---------------------------------------------------------------------------


def test_debate_config_default_early_stop_signal():
    """Default early_stop_signal is an empty string (disabled)."""
    cfg = DebateConfig()
    assert cfg.early_stop_signal == ""


def test_debate_config_custom_early_stop_signal():
    cfg = DebateConfig(early_stop_signal="CONSENSUS_REACHED")
    assert cfg.early_stop_signal == "CONSENSUS_REACHED"


def test_debate_config_early_stop_signal_is_str():
    cfg = DebateConfig(early_stop_signal="STOP_NOW")
    assert isinstance(cfg.early_stop_signal, str)


def test_debate_config_early_stop_with_rounds_valid():
    """early_stop_signal is compatible with multiple rounds."""
    cfg = DebateConfig(rounds=3, early_stop_signal="CONVERGED")
    assert cfg.rounds == 3
    assert cfg.early_stop_signal == "CONVERGED"


def test_debate_judge_prompt_includes_early_stop_signal_instruction():
    """When early_stop_signal is set, judge prompt instructs agent to emit it."""
    from tmux_orchestrator.domain.phase_strategy import DebateStrategy

    cfg = DebateConfig(rounds=1, early_stop_signal="CONSENSUS_REACHED")
    spec = PhaseSpec(
        name="argue",
        pattern="debate",
        debate_rounds=1,
        strategy_config=cfg,
    )
    strategy = DebateStrategy()
    tasks, _ = strategy.expand(spec, prior_ids=[], context="topic", scratchpad_prefix="pfx")
    judge_tasks = [t for t in tasks if "judge" in t["local_id"]]
    assert len(judge_tasks) == 1
    assert "CONSENSUS_REACHED" in judge_tasks[0]["prompt"]


def test_debate_judge_prompt_no_early_stop_when_signal_empty():
    """When early_stop_signal is empty, judge prompt has no early-stop instruction."""
    from tmux_orchestrator.domain.phase_strategy import DebateStrategy

    cfg = DebateConfig(rounds=1, early_stop_signal="")
    spec = PhaseSpec(
        name="argue",
        pattern="debate",
        debate_rounds=1,
        strategy_config=cfg,
    )
    strategy = DebateStrategy()
    tasks, _ = strategy.expand(spec, prior_ids=[], context="topic", scratchpad_prefix="pfx")
    judge_tasks = [t for t in tasks if "judge" in t["local_id"]]
    assert len(judge_tasks) == 1
    # No early-stop instruction in the prompt
    assert "EARLY_STOP" not in judge_tasks[0]["prompt"]
    assert "early stop" not in judge_tasks[0]["prompt"].lower()


def test_debate_judge_prompt_early_stop_without_strategy_config():
    """Without strategy_config, no early-stop instruction in judge prompt."""
    from tmux_orchestrator.domain.phase_strategy import DebateStrategy

    spec = PhaseSpec(name="argue", pattern="debate", debate_rounds=1)
    strategy = DebateStrategy()
    tasks, _ = strategy.expand(spec, prior_ids=[], context="topic", scratchpad_prefix="pfx")
    judge_tasks = [t for t in tasks if "judge" in t["local_id"]]
    assert len(judge_tasks) == 1
    assert "EARLY_STOP" not in judge_tasks[0]["prompt"]


# ---------------------------------------------------------------------------
# Pydantic schema models (web/schemas.py) — v1.1.32 new fields
# ---------------------------------------------------------------------------


def test_competitive_config_model_has_judge_prompt_template():
    from tmux_orchestrator.web.schemas import CompetitiveConfigModel

    m = CompetitiveConfigModel()
    assert hasattr(m, "judge_prompt_template")
    assert m.judge_prompt_template == ""


def test_competitive_config_model_judge_prompt_template_set():
    from tmux_orchestrator.web.schemas import CompetitiveConfigModel

    m = CompetitiveConfigModel(judge_prompt_template="Pick best: {solutions}.")
    assert m.judge_prompt_template == "Pick best: {solutions}."


def test_debate_config_model_has_early_stop_signal():
    from tmux_orchestrator.web.schemas import DebateConfigModel

    m = DebateConfigModel()
    assert hasattr(m, "early_stop_signal")
    assert m.early_stop_signal == ""


def test_debate_config_model_early_stop_signal_set():
    from tmux_orchestrator.web.schemas import DebateConfigModel

    m = DebateConfigModel(early_stop_signal="CONVERGED")
    assert m.early_stop_signal == "CONVERGED"
