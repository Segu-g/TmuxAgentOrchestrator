"""Tests for workflow_defaults — YAML template parameter inheritance.

Verifies that:
1. ``deep_merge_defaults`` correctly merges defaults into base dicts.
2. ``apply_workflow_defaults`` strips metadata keys and applies defaults.
3. ``load_workflow_template`` reads a YAML file and returns the merged dict.
4. YAML templates with a ``defaults:`` section validate correctly via Pydantic.
5. Edge cases: empty defaults, nested dicts, list fields, None values.

Design references:
- GitLab CI/CD ``default:`` keyword — job-level inheritance pattern.
- HiYaPyCo deep-merge — hierarchical YAML Python config library.
- DESIGN.md §10.65 (v1.1.33)
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tmux_orchestrator.workflow_defaults import (
    apply_workflow_defaults,
    deep_merge_defaults,
    load_workflow_template,
)
from tmux_orchestrator.web.schemas import (
    TddWorkflowSubmit,
    CompetitionWorkflowSubmit,
    DebateWorkflowSubmit,
    PairWorkflowSubmit,
)


# ---------------------------------------------------------------------------
# deep_merge_defaults
# ---------------------------------------------------------------------------


class TestDeepMergeDefaults:
    """Unit tests for the deep_merge_defaults() function."""

    def test_base_value_wins_for_scalars(self):
        """Scalar in base must not be overwritten by default."""
        result = deep_merge_defaults({"a": 1}, {"a": 99})
        assert result["a"] == 1

    def test_missing_key_gets_default(self):
        """Key absent from base must be filled from defaults."""
        result = deep_merge_defaults({"a": 1}, {"b": 2})
        assert result["b"] == 2
        assert result["a"] == 1

    def test_nested_dict_merges_recursively(self):
        """Nested dict defaults fill in missing sub-keys without overwriting existing ones."""
        base = {"nested": {"x": 1}}
        defaults = {"nested": {"x": 0, "y": 2}}
        result = deep_merge_defaults(base, defaults)
        assert result["nested"]["x"] == 1  # base wins
        assert result["nested"]["y"] == 2  # default fills in

    def test_empty_base_gets_all_defaults(self):
        """Empty base receives all default values."""
        defaults = {"language": "python", "reply_to": None, "tags": ["gpu"]}
        result = deep_merge_defaults({}, defaults)
        assert result == defaults

    def test_empty_defaults_returns_base(self):
        """Empty defaults dict must return base unchanged."""
        base = {"feature": "a stack", "language": "typescript"}
        result = deep_merge_defaults(base, {})
        assert result == base

    def test_list_field_not_merged(self):
        """List in base is kept; list from defaults is NOT appended."""
        base = {"tags": ["cpu"]}
        defaults = {"tags": ["gpu", "high-mem"]}
        result = deep_merge_defaults(base, defaults)
        assert result["tags"] == ["cpu"]

    def test_absent_list_field_gets_default(self):
        """List field absent from base is filled from defaults."""
        result = deep_merge_defaults({}, {"tags": ["gpu"]})
        assert result["tags"] == ["gpu"]

    def test_none_value_in_base_preserved(self):
        """Explicit None in base must not be overwritten by a non-None default."""
        result = deep_merge_defaults({"reply_to": None}, {"reply_to": "agent-1"})
        assert result["reply_to"] is None

    def test_false_value_in_base_preserved(self):
        """Explicit False in base must not be overwritten by True default."""
        result = deep_merge_defaults({"flag": False}, {"flag": True})
        assert result["flag"] is False

    def test_empty_string_in_base_preserved(self):
        """Empty string in base must not be overwritten by non-empty default."""
        result = deep_merge_defaults({"language": ""}, {"language": "python"})
        assert result["language"] == ""

    def test_returns_new_dict_not_mutating_base(self):
        """deep_merge_defaults must not mutate its input dicts."""
        base = {"a": 1}
        defaults = {"b": 2}
        result = deep_merge_defaults(base, defaults)
        assert "b" not in base
        assert result is not base

    def test_deeply_nested_three_levels(self):
        """Three levels of nesting must be handled correctly."""
        base = {"l1": {"l2": {"l3_base": "kept"}}}
        defaults = {"l1": {"l2": {"l3_base": "overridden", "l3_new": "added"}}}
        result = deep_merge_defaults(base, defaults)
        assert result["l1"]["l2"]["l3_base"] == "kept"
        assert result["l1"]["l2"]["l3_new"] == "added"

    def test_base_non_dict_where_default_is_dict_kept(self):
        """If base has a scalar for a key where defaults has a dict, base wins."""
        result = deep_merge_defaults({"x": "scalar"}, {"x": {"nested": 1}})
        assert result["x"] == "scalar"


# ---------------------------------------------------------------------------
# apply_workflow_defaults
# ---------------------------------------------------------------------------


class TestApplyWorkflowDefaults:
    """Unit tests for the apply_workflow_defaults() function."""

    def test_strips_workflow_metadata_key(self):
        """The 'workflow' key must not appear in the returned dict."""
        data = {
            "workflow": {"endpoint": "/workflows/tdd"},
            "feature": "a stack",
        }
        result = apply_workflow_defaults(data)
        assert "workflow" not in result

    def test_strips_defaults_key(self):
        """The 'defaults' key must not appear in the returned dict."""
        data = {
            "defaults": {"language": "python"},
            "feature": "a stack",
        }
        result = apply_workflow_defaults(data)
        assert "defaults" not in result

    def test_defaults_fill_missing_fields(self):
        """Fields from defaults must appear in result when absent from body."""
        data = {
            "workflow": {"endpoint": "/workflows/tdd"},
            "defaults": {"language": "python", "reply_to": None},
            "feature": "a stack",
        }
        result = apply_workflow_defaults(data)
        assert result["feature"] == "a stack"
        assert result["language"] == "python"
        assert result["reply_to"] is None

    def test_body_field_overrides_default(self):
        """Field explicitly set in body must override the default."""
        data = {
            "defaults": {"language": "python"},
            "feature": "a stack",
            "language": "typescript",
        }
        result = apply_workflow_defaults(data)
        assert result["language"] == "typescript"

    def test_no_defaults_section(self):
        """Templates without a 'defaults:' key must pass through unchanged (minus workflow:)."""
        data = {
            "workflow": {"endpoint": "/workflows/tdd"},
            "feature": "a stack",
            "language": "python",
        }
        result = apply_workflow_defaults(data)
        assert result == {"feature": "a stack", "language": "python"}

    def test_empty_defaults_section(self):
        """An empty 'defaults: {}' must return body unchanged."""
        data = {
            "workflow": {"endpoint": "/workflows/tdd"},
            "defaults": {},
            "feature": "a stack",
        }
        result = apply_workflow_defaults(data)
        assert result == {"feature": "a stack"}

    def test_none_defaults_section(self):
        """A 'defaults: null' must be treated as no defaults."""
        data = {
            "defaults": None,
            "feature": "a stack",
        }
        result = apply_workflow_defaults(data)
        assert result == {"feature": "a stack"}

    def test_both_workflow_and_defaults_stripped(self):
        """Both 'workflow' and 'defaults' must be stripped together."""
        data = {
            "workflow": {"endpoint": "/workflows/competition"},
            "defaults": {"scoring_criterion": "correctness"},
            "problem": "Write a sieve",
            "strategies": ["greedy", "dp"],
        }
        result = apply_workflow_defaults(data)
        assert "workflow" not in result
        assert "defaults" not in result
        assert result["scoring_criterion"] == "correctness"
        assert result["problem"] == "Write a sieve"


# ---------------------------------------------------------------------------
# load_workflow_template
# ---------------------------------------------------------------------------


class TestLoadWorkflowTemplate:
    """Tests for load_workflow_template() reading from real/mock files."""

    def test_loads_and_applies_defaults(self, tmp_path: Path):
        """load_workflow_template must read YAML and apply defaults."""
        template = {
            "workflow": {"endpoint": "/workflows/tdd"},
            "defaults": {"language": "python"},
            "feature": "a stack",
        }
        p = tmp_path / "test.yaml"
        p.write_text(yaml.safe_dump(template), encoding="utf-8")

        result = load_workflow_template(p)
        assert result["feature"] == "a stack"
        assert result["language"] == "python"
        assert "workflow" not in result
        assert "defaults" not in result

    def test_strips_workflow_metadata(self, tmp_path: Path):
        """load_workflow_template must strip 'workflow' key."""
        template = {"workflow": {"endpoint": "/w/tdd"}, "feature": "x"}
        p = tmp_path / "t.yaml"
        p.write_text(yaml.safe_dump(template), encoding="utf-8")
        result = load_workflow_template(p)
        assert "workflow" not in result

    def test_loads_real_tdd_template(self):
        """The real tdd.yaml template must load without errors."""
        templates_dir = (
            Path(__file__).parent.parent / "examples" / "workflows"
        )
        result = load_workflow_template(templates_dir / "tdd.yaml")
        assert "feature" in result
        assert "workflow" not in result

    def test_loads_real_competition_template(self):
        """The real competition.yaml template must load without errors."""
        templates_dir = (
            Path(__file__).parent.parent / "examples" / "workflows"
        )
        result = load_workflow_template(templates_dir / "competition.yaml")
        assert "problem" in result
        assert "strategies" in result


# ---------------------------------------------------------------------------
# Pydantic schema integration: templates with defaults: validate correctly
# ---------------------------------------------------------------------------


class TestDefaultsWithPydanticSchemas:
    """Verify that defaults-merged dicts validate correctly via Pydantic."""

    def test_tdd_with_defaults_validates(self):
        """TddWorkflowSubmit validates from a dict produced by apply_workflow_defaults."""
        data = {
            "workflow": {"endpoint": "/workflows/tdd"},
            "defaults": {"language": "python", "reply_to": None},
            "feature": "a priority queue",
        }
        merged = apply_workflow_defaults(data)
        instance = TddWorkflowSubmit.model_validate(merged)
        assert instance.feature == "a priority queue"
        assert instance.language == "python"
        assert instance.reply_to is None

    def test_tdd_body_language_overrides_default(self):
        """Explicit body language must override the default."""
        data = {
            "defaults": {"language": "python"},
            "feature": "a binary tree",
            "language": "typescript",
        }
        merged = apply_workflow_defaults(data)
        instance = TddWorkflowSubmit.model_validate(merged)
        assert instance.language == "typescript"

    def test_competition_with_defaults_validates(self):
        """CompetitionWorkflowSubmit validates with defaults-supplied criterion."""
        data = {
            "workflow": {"endpoint": "/workflows/competition"},
            "defaults": {
                "scoring_criterion": "correctness, performance, and code clarity",
                "solver_tags": [],
                "judge_tags": [],
                "reply_to": None,
            },
            "problem": "Write a sieve of Eratosthenes function.",
            "strategies": ["iterative", "bitarray"],
        }
        merged = apply_workflow_defaults(data)
        instance = CompetitionWorkflowSubmit.model_validate(merged)
        assert instance.scoring_criterion == "correctness, performance, and code clarity"
        assert len(instance.strategies) == 2

    def test_debate_with_defaults_validates(self):
        """DebateWorkflowSubmit validates with defaults-supplied max_rounds."""
        data = {
            "workflow": {"endpoint": "/workflows/debate"},
            "defaults": {
                "max_rounds": 2,
                "advocate_tags": [],
                "critic_tags": [],
                "judge_tags": [],
                "reply_to": None,
            },
            "topic": "Is Python better than Go for data pipelines?",
        }
        merged = apply_workflow_defaults(data)
        instance = DebateWorkflowSubmit.model_validate(merged)
        assert instance.topic == "Is Python better than Go for data pipelines?"
        assert instance.max_rounds == 2

    def test_body_max_rounds_overrides_default(self):
        """Explicit max_rounds in body must override default."""
        data = {
            "defaults": {"max_rounds": 2},
            "topic": "Test topic",
            "max_rounds": 1,
        }
        merged = apply_workflow_defaults(data)
        instance = DebateWorkflowSubmit.model_validate(merged)
        assert instance.max_rounds == 1


# ---------------------------------------------------------------------------
# YAML templates: defaults section in real template files
# ---------------------------------------------------------------------------


class TestRealTemplateDefaultsSection:
    """Verify the defaults: section in real template files validates correctly."""

    TEMPLATES_DIR = Path(__file__).parent.parent / "examples" / "workflows"

    def _load(self, filename: str) -> dict:
        return load_workflow_template(self.TEMPLATES_DIR / filename)

    def test_tdd_defaults_produce_valid_schema(self):
        merged = self._load("tdd.yaml")
        instance = TddWorkflowSubmit.model_validate(merged)
        assert instance is not None

    def test_competition_defaults_produce_valid_schema(self):
        merged = self._load("competition.yaml")
        instance = CompetitionWorkflowSubmit.model_validate(merged)
        assert instance is not None

    def test_debate_defaults_produce_valid_schema(self):
        merged = self._load("debate.yaml")
        instance = DebateWorkflowSubmit.model_validate(merged)
        assert instance is not None

    def test_pair_defaults_produce_valid_schema(self):
        merged = self._load("pair.yaml")
        instance = PairWorkflowSubmit.model_validate(merged)
        assert instance is not None
