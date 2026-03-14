"""Tests for role-specific rules embedded in CLAUDE.md (v1.2.20).

Covers:
- CLAUDE.md for a WORKER agent contains rules from worker.md (embedded, not copied)
- CLAUDE.md for a DIRECTOR agent contains rules from director.md (embedded)
- .claude/rules/ does NOT contain any role-specific files (role rules go to CLAUDE.md only)
- If no rules file exists for the role, _write_agent_claude_md() still succeeds
- ClaudeCodeAgent does NOT accept role_rules_file parameter
- worker.md and director.md exist in agent_plugin/rules/ (canonical source)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.application.config import AgentConfig, AgentRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    role: AgentRole = AgentRole.WORKER,
) -> ClaudeCodeAgent:
    """Return a minimal ClaudeCodeAgent instance without starting it."""
    bus = MagicMock()
    tmux = MagicMock()
    return ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        role=role,
        isolate=True,
    )


# ---------------------------------------------------------------------------
# ClaudeCodeAgent does NOT accept role_rules_file
# ---------------------------------------------------------------------------


class TestAgentConstructorNoRoleRulesFile:
    def test_role_rules_file_not_accepted(self) -> None:
        """ClaudeCodeAgent must NOT accept role_rules_file parameter."""
        import inspect
        sig = inspect.signature(ClaudeCodeAgent.__init__)
        assert "role_rules_file" not in sig.parameters, (
            "role_rules_file parameter should have been removed from ClaudeCodeAgent.__init__"
        )

    def test_agent_has_no_role_rules_file_attr(self) -> None:
        """ClaudeCodeAgent instances must NOT have _role_rules_file attribute."""
        agent = _make_agent()
        assert not hasattr(agent, "_role_rules_file"), (
            "_role_rules_file attribute should have been removed from ClaudeCodeAgent"
        )

    def test_agent_has_load_role_rules_method(self) -> None:
        """ClaudeCodeAgent must have _load_role_rules() method."""
        agent = _make_agent()
        assert callable(getattr(agent, "_load_role_rules", None)), (
            "_load_role_rules() method must exist on ClaudeCodeAgent"
        )

    def test_agent_has_no_copy_rules_method(self) -> None:
        """ClaudeCodeAgent must NOT have _copy_rules() method."""
        agent = _make_agent()
        assert not hasattr(agent, "_copy_rules"), (
            "_copy_rules() method should have been removed from ClaudeCodeAgent"
        )


# ---------------------------------------------------------------------------
# AgentConfig does NOT have role_rules_file
# ---------------------------------------------------------------------------


class TestAgentConfigNoRoleRulesFile:
    def test_role_rules_file_not_in_agentconfig(self) -> None:
        """AgentConfig must NOT have role_rules_file field."""
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(AgentConfig)}
        assert "role_rules_file" not in field_names, (
            "role_rules_file field should have been removed from AgentConfig"
        )

    def test_agentconfig_still_works(self) -> None:
        """AgentConfig can still be constructed without role_rules_file."""
        cfg = AgentConfig(id="a1", type="claude_code")
        assert cfg.id == "a1"


# ---------------------------------------------------------------------------
# CLAUDE.md embeds rules from worker.md / director.md
# ---------------------------------------------------------------------------


class TestClaudeMdEmbeddsRoleRules:
    def test_worker_claude_md_contains_worker_rules(self, tmp_path: Path) -> None:
        """CLAUDE.md for a WORKER agent embeds content from worker.md."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        claude_md = (tmp_path / "CLAUDE.md").read_text()

        # Must contain the ## Role Rules section
        assert "## Role Rules" in claude_md, (
            "CLAUDE.md must contain '## Role Rules' section"
        )
        # Must contain worker-specific content from worker.md
        assert "/task-complete" in claude_md, (
            "CLAUDE.md must contain /task-complete from worker.md"
        )
        assert "Worker" in claude_md, (
            "CLAUDE.md must contain 'Worker' content from worker.md"
        )

    def test_director_claude_md_contains_director_rules(self, tmp_path: Path) -> None:
        """CLAUDE.md for a DIRECTOR agent embeds content from director.md."""
        agent = _make_agent(role=AgentRole.DIRECTOR)
        agent._write_agent_claude_md(tmp_path)

        claude_md = (tmp_path / "CLAUDE.md").read_text()

        # Must contain the ## Role Rules section
        assert "## Role Rules" in claude_md, (
            "CLAUDE.md must contain '## Role Rules' section"
        )
        # Must contain director-specific content from director.md
        assert "task-complete" in claude_md, (
            "CLAUDE.md must reference task-complete from director.md"
        )
        assert "Director" in claude_md, (
            "CLAUDE.md must contain 'Director' content from director.md"
        )

    def test_no_role_rules_directory_created(self, tmp_path: Path) -> None:
        """.claude/rules/ must NOT be created — role rules go into CLAUDE.md only."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        rules_dir = tmp_path / ".claude" / "rules"
        # rules/ should not exist (or if it does, must not have role files)
        if rules_dir.exists():
            role_files = list(rules_dir.glob("*.md"))
            assert len(role_files) == 0, (
                f".claude/rules/ must not contain role-specific files; found: {role_files}"
            )

    def test_worker_rules_not_in_dot_claude_rules(self, tmp_path: Path) -> None:
        """worker.md must NOT be copied to .claude/rules/worker.md."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        assert not (tmp_path / ".claude" / "rules" / "worker.md").exists(), (
            "worker.md must not be copied to .claude/rules/; it is embedded in CLAUDE.md"
        )

    def test_director_rules_not_in_dot_claude_rules(self, tmp_path: Path) -> None:
        """director.md must NOT be copied to .claude/rules/director.md."""
        agent = _make_agent(role=AgentRole.DIRECTOR)
        agent._write_agent_claude_md(tmp_path)

        assert not (tmp_path / ".claude" / "rules" / "director.md").exists(), (
            "director.md must not be copied to .claude/rules/; it is embedded in CLAUDE.md"
        )


# ---------------------------------------------------------------------------
# _load_role_rules: unit tests
# ---------------------------------------------------------------------------


class TestLoadRoleRules:
    def test_worker_rules_loaded(self) -> None:
        """_load_role_rules() returns non-empty string for WORKER role."""
        agent = _make_agent(role=AgentRole.WORKER)
        result = agent._load_role_rules()
        assert result != "", "Expected non-empty role rules for WORKER"
        assert "## Role Rules" in result
        assert "/task-complete" in result

    def test_director_rules_loaded(self) -> None:
        """_load_role_rules() returns non-empty string for DIRECTOR role."""
        agent = _make_agent(role=AgentRole.DIRECTOR)
        result = agent._load_role_rules()
        assert result != "", "Expected non-empty role rules for DIRECTOR"
        assert "## Role Rules" in result
        assert "task-complete" in result

    def test_unknown_role_returns_empty_string(self) -> None:
        """_load_role_rules() returns '' when no rules file exists for the role."""
        # Use WORKER but patch the rules path to a nonexistent file
        import unittest.mock as mock
        agent = _make_agent(role=AgentRole.WORKER)

        # Patch the role to something unusual so no file exists
        with mock.patch.object(agent, "role") as mock_role:
            mock_role.value = "nonexistent_role_xyz"
            result = agent._load_role_rules()
        assert result == "", "Expected empty string when no rules file exists for role"

    def test_write_agent_claude_md_succeeds_without_rules_file(
        self, tmp_path: Path
    ) -> None:
        """_write_agent_claude_md() succeeds even when no rules file exists for the role."""
        import unittest.mock as mock
        agent = _make_agent(role=AgentRole.WORKER)

        # Force _load_role_rules to return empty string (simulate missing file)
        with mock.patch.object(agent, "_load_role_rules", return_value=""):
            agent._write_agent_claude_md(tmp_path)  # must not raise

        assert (tmp_path / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# Real built-in rules files exist
# ---------------------------------------------------------------------------


class TestBuiltInRulesFilesExist:
    def test_worker_md_exists(self) -> None:
        """worker.md must exist in agent_plugin/rules/."""
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        assert (rules_dir / "worker.md").exists(), "agent_plugin/rules/worker.md missing"

    def test_director_md_exists(self) -> None:
        """director.md must exist in agent_plugin/rules/."""
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        assert (rules_dir / "director.md").exists(), "agent_plugin/rules/director.md missing"

    def test_worker_md_mentions_task_complete(self) -> None:
        """worker.md should reference /task-complete."""
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        content = (rules_dir / "worker.md").read_text()
        assert "/task-complete" in content

    def test_director_md_mentions_task_complete(self) -> None:
        """director.md should reference task-complete REST endpoint."""
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        content = (rules_dir / "director.md").read_text()
        assert "task-complete" in content

    def test_worker_md_under_80_lines(self) -> None:
        """worker.md should be concise (< 80 lines)."""
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        lines = (rules_dir / "worker.md").read_text().splitlines()
        assert len(lines) < 80, f"worker.md too long: {len(lines)} lines"

    def test_director_md_under_100_lines(self) -> None:
        """director.md should be concise (< 100 lines)."""
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        lines = (rules_dir / "director.md").read_text().splitlines()
        assert len(lines) < 100, f"director.md too long: {len(lines)} lines"


# ---------------------------------------------------------------------------
# Integration: CLAUDE.md written correctly end-to-end
# ---------------------------------------------------------------------------


class TestClaudeMdIntegration:
    def test_worker_claude_md_idempotent(self, tmp_path: Path) -> None:
        """_write_agent_claude_md() can be called twice without error."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)
        first = (tmp_path / "CLAUDE.md").read_text()

        agent._write_agent_claude_md(tmp_path)  # second call overwrites
        second = (tmp_path / "CLAUDE.md").read_text()

        assert first == second

    def test_director_claude_md_idempotent(self, tmp_path: Path) -> None:
        """_write_agent_claude_md() for DIRECTOR can be called twice without error."""
        agent = _make_agent(role=AgentRole.DIRECTOR)
        agent._write_agent_claude_md(tmp_path)
        first = (tmp_path / "CLAUDE.md").read_text()

        agent._write_agent_claude_md(tmp_path)
        second = (tmp_path / "CLAUDE.md").read_text()

        assert first == second

    def test_claude_md_contains_agent_id(self, tmp_path: Path) -> None:
        """CLAUDE.md must include the agent ID."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text()
        assert "test-agent" in content

    def test_role_rules_at_end_of_claude_md(self, tmp_path: Path) -> None:
        """Role rules section should appear after the main CLAUDE.md content."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text()

        slash_cmd_table_pos = content.find("Slash Command Reference")
        role_rules_pos = content.find("## Role Rules")

        assert slash_cmd_table_pos != -1, "Slash Command Reference section must exist"
        assert role_rules_pos != -1, "## Role Rules section must exist"
        assert role_rules_pos > slash_cmd_table_pos, (
            "## Role Rules must appear after the Slash Command Reference table"
        )
