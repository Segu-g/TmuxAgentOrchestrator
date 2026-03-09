"""Tests for the Context Engineering 4-Strategy guide (v1.1.19).

Verifies that:
1. `.claude/prompts/context-strategies.md` exists and contains the required sections.
2. CLAUDE.md contains the Context Engineering Cheatsheet section.
3. The four strategy keywords appear in both documents.
4. Role-based recommendation table covers all primary roles.

References:
- Anthropic "Effective context engineering for AI agents" (2025-09-29)
- LangChain Blog "Context Engineering for Agents" (2025)
- JetBrains Research "Cutting Through the Noise" (2025-12)
"""
from __future__ import annotations

import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CONTEXT_STRATEGIES_FILE = PROJECT_ROOT / ".claude" / "prompts" / "context-strategies.md"
CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"

FOUR_STRATEGIES = ["Write", "Select", "Compress", "Isolate"]


# ---------------------------------------------------------------------------
# context-strategies.md existence and structure
# ---------------------------------------------------------------------------


class TestContextStrategiesFile:
    def test_file_exists(self):
        assert CONTEXT_STRATEGIES_FILE.exists(), (
            f"context-strategies.md not found at {CONTEXT_STRATEGIES_FILE}"
        )

    def test_file_is_non_empty(self):
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert len(content) > 100, "context-strategies.md appears to be empty or trivial"

    @pytest.mark.parametrize("strategy", FOUR_STRATEGIES)
    def test_strategy_section_present(self, strategy):
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert strategy in content, (
            f"Strategy '{strategy}' not found in context-strategies.md"
        )

    def test_role_matrix_present(self):
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert "Role-Based" in content or "role" in content.lower(), (
            "Role-based matrix section missing from context-strategies.md"
        )

    @pytest.mark.parametrize("role", [
        "implementer",
        "reviewer",
        "tester",
        "spec-writer",
        "planner",
        "judge",
    ])
    def test_role_in_matrix(self, role):
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert role in content, (
            f"Role '{role}' not mentioned in context-strategies.md matrix"
        )

    def test_combining_strategies_section_present(self):
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert "Combining" in content or "combin" in content.lower(), (
            "Combining strategies section missing from context-strategies.md"
        )

    def test_references_present(self):
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert "Anthropic" in content, (
            "Anthropic reference missing from context-strategies.md"
        )

    def test_anti_patterns_mentioned(self):
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert "Anti-pattern" in content or "anti-pattern" in content.lower(), (
            "Anti-patterns section missing — each strategy should document what NOT to do"
        )

    def test_scratchpad_usage_documented(self):
        """The Write strategy should mention the scratchpad REST API."""
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert "scratchpad" in content.lower(), (
            "Scratchpad usage not documented in Write strategy"
        )

    def test_summarize_command_documented(self):
        """The Compress strategy should mention /summarize."""
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert "/summarize" in content, (
            "/summarize command not documented in Compress strategy"
        )

    def test_spawn_subagent_documented(self):
        """The Isolate strategy should mention /spawn-subagent."""
        content = CONTEXT_STRATEGIES_FILE.read_text()
        assert "spawn-subagent" in content or "spawn_subagent" in content, (
            "/spawn-subagent command not documented in Isolate strategy"
        )


# ---------------------------------------------------------------------------
# CLAUDE.md cheatsheet section
# ---------------------------------------------------------------------------


class TestClaudeMdCheatsheet:
    def test_claude_md_exists(self):
        assert CLAUDE_MD.exists(), "CLAUDE.md not found"

    def test_context_engineering_section_present(self):
        content = CLAUDE_MD.read_text()
        assert "Context Engineering" in content, (
            "Context Engineering section missing from CLAUDE.md"
        )

    @pytest.mark.parametrize("strategy", FOUR_STRATEGIES)
    def test_strategy_in_claude_md(self, strategy):
        content = CLAUDE_MD.read_text()
        assert strategy in content, (
            f"Strategy '{strategy}' not mentioned in CLAUDE.md cheatsheet"
        )

    def test_cheatsheet_table_present(self):
        """CLAUDE.md should have a table mapping strategies to when/how to apply."""
        content = CLAUDE_MD.read_text()
        # A markdown table has pipe characters
        lines = content.split("\n")
        table_lines = [l for l in lines if "|" in l]
        assert len(table_lines) >= 4, (
            "CLAUDE.md cheatsheet should contain a strategy table with at least 4 rows"
        )

    def test_role_recommendations_in_claude_md(self):
        content = CLAUDE_MD.read_text()
        assert "implementer" in content or "reviewer" in content, (
            "Role-based recommendations missing from CLAUDE.md"
        )

    def test_summarize_mentioned_in_compress_context(self):
        content = CLAUDE_MD.read_text()
        # /summarize should appear near Compress in the cheatsheet
        idx_compress = content.find("Compress")
        idx_summarize = content.find("/summarize")
        assert idx_compress >= 0, "Compress strategy not found in CLAUDE.md"
        assert idx_summarize >= 0, "/summarize not found in CLAUDE.md"
        # Both should be in the same general vicinity (within 1000 chars)
        assert abs(idx_compress - idx_summarize) < 1000, (
            "/summarize should be mentioned near the Compress strategy explanation"
        )

    def test_key_rules_section_present(self):
        content = CLAUDE_MD.read_text()
        assert "Key Rules" in content or "key rule" in content.lower(), (
            "Key Rules section missing from CLAUDE.md cheatsheet"
        )

    def test_references_cited(self):
        content = CLAUDE_MD.read_text()
        assert "Anthropic" in content and "2025" in content, (
            "Anthropic 2025 reference missing from CLAUDE.md cheatsheet"
        )
