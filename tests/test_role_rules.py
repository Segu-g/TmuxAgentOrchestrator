"""Tests for role-specific rules via env vars in a static CLAUDE.md section (v1.2.21).

Covers:
- CLAUDE.md contains the static TMUX_ORCHESTRATOR_AGENT_ROLE reference text
- CLAUDE.md does NOT contain embedded rules file content
- _load_role_rules() method no longer exists on ClaudeCodeAgent
- The launch command includes TMUX_ORCHESTRATOR_AGENT_ROLE and
  TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR env vars
- agent_plugin/docs/worker.md and director.md exist at the new path
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
# 1. CLAUDE.md contains the static TMUX_ORCHESTRATOR_AGENT_ROLE reference text
# ---------------------------------------------------------------------------


class TestClaudeMdStaticRoleSection:
    def test_worker_claude_md_contains_role_env_var_reference(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md must contain the static TMUX_ORCHESTRATOR_AGENT_ROLE reference."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "TMUX_ORCHESTRATOR_AGENT_ROLE" in content, (
            "CLAUDE.md must reference TMUX_ORCHESTRATOR_AGENT_ROLE env var"
        )

    def test_director_claude_md_contains_role_env_var_reference(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md for DIRECTOR must also contain the static env var reference."""
        agent = _make_agent(role=AgentRole.DIRECTOR)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "TMUX_ORCHESTRATOR_AGENT_ROLE" in content, (
            "CLAUDE.md must reference TMUX_ORCHESTRATOR_AGENT_ROLE env var"
        )

    def test_claude_md_contains_plugin_docs_dir_reference(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md must reference TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR" in content, (
            "CLAUDE.md must reference TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR env var"
        )

    def test_claude_md_contains_cat_command_for_role_doc(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md must contain the cat command to read the role documentation."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        assert 'cat "$TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR/$TMUX_ORCHESTRATOR_AGENT_ROLE.md"' in content, (
            "CLAUDE.md must contain the cat command to read role documentation"
        )

    def test_claude_md_contains_role_specific_instructions_section(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md must contain a '## Role-Specific Instructions' section."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "## Role-Specific Instructions" in content, (
            "CLAUDE.md must contain '## Role-Specific Instructions' section"
        )

    def test_claude_md_role_section_appears_after_slash_command_table(
        self, tmp_path: Path
    ) -> None:
        """Role-Specific Instructions section should appear after Slash Command Reference."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        slash_pos = content.find("Slash Command Reference")
        role_pos = content.find("## Role-Specific Instructions")

        assert slash_pos != -1, "Slash Command Reference section must exist"
        assert role_pos != -1, "## Role-Specific Instructions section must exist"
        assert role_pos > slash_pos, (
            "## Role-Specific Instructions must appear after the Slash Command Reference table"
        )


# ---------------------------------------------------------------------------
# 2. CLAUDE.md does NOT contain embedded rules file content
# ---------------------------------------------------------------------------


class TestClaudeMdNoEmbeddedRulesContent:
    def test_worker_claude_md_does_not_embed_worker_md_content(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md must NOT contain the literal content of worker.md."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()

        # Load worker.md and check its distinctive content is NOT embedded
        docs_dir = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "agent_plugin" / "docs"
        worker_md = (docs_dir / "worker.md").read_text()
        # Use a distinctive line from worker.md that would only appear if embedded
        distinctive_line = "Auto-generated by TmuxAgentOrchestrator. Loaded from"
        assert distinctive_line not in content, (
            "CLAUDE.md must NOT embed the raw content of worker.md"
        )

    def test_director_claude_md_does_not_embed_director_md_content(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md must NOT contain the literal content of director.md."""
        agent = _make_agent(role=AgentRole.DIRECTOR)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()

        distinctive_line = "Auto-generated by TmuxAgentOrchestrator. Loaded from"
        assert distinctive_line not in content, (
            "CLAUDE.md must NOT embed the raw content of director.md"
        )

    def test_claude_md_does_not_contain_role_rules_section_heading(
        self, tmp_path: Path
    ) -> None:
        """CLAUDE.md must NOT contain '## Role Rules' (the old embedded section heading)."""
        agent = _make_agent(role=AgentRole.WORKER)
        agent._write_agent_claude_md(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "## Role Rules" not in content, (
            "CLAUDE.md must not use the old '## Role Rules' embedded section"
        )


# ---------------------------------------------------------------------------
# 3. _load_role_rules() method no longer exists
# ---------------------------------------------------------------------------


class TestLoadRoleRulesMethodRemoved:
    def test_load_role_rules_method_does_not_exist(self) -> None:
        """_load_role_rules() must NOT exist on ClaudeCodeAgent."""
        agent = _make_agent()
        assert not hasattr(agent, "_load_role_rules"), (
            "_load_role_rules() method must be removed from ClaudeCodeAgent"
        )

    def test_load_role_rules_not_in_class_dict(self) -> None:
        """_load_role_rules must not be in ClaudeCodeAgent's method dict."""
        assert "_load_role_rules" not in ClaudeCodeAgent.__dict__, (
            "_load_role_rules must be removed from ClaudeCodeAgent class"
        )


# ---------------------------------------------------------------------------
# 4. Launch command includes TMUX_ORCHESTRATOR_AGENT_ROLE and
#    TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR env vars
# ---------------------------------------------------------------------------


def _make_fake_pane() -> tuple[MagicMock, dict[str, str]]:
    """Return a (pane_mock, captured_env) pair for use in start() tests."""
    captured_env: dict[str, str] = {}

    pane = MagicMock()
    pane.get_pane_id = MagicMock(return_value="pane-1")
    return pane, captured_env


class TestPaneEnvVars:
    @pytest.mark.asyncio
    async def test_pane_env_includes_agent_role(self) -> None:
        """start() must set TMUX_ORCHESTRATOR_AGENT_ROLE in pane_env."""
        agent = _make_agent(role=AgentRole.WORKER)
        pane, captured_env = _make_fake_pane()

        def fake_new_pane(agent_id: str, env: dict[str, str]) -> MagicMock:
            captured_env.update(env)
            return pane

        agent._tmux.new_pane.side_effect = fake_new_pane
        agent._tmux.watch_pane = MagicMock()
        agent._tmux.start_watcher = MagicMock()

        with (
            patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=None),
            patch.object(agent, "_wait_for_ready", new_callable=AsyncMock),
            patch.object(agent, "_run_loop", new_callable=AsyncMock),
            patch.object(agent, "_start_message_loop", new_callable=AsyncMock),
        ):
            await agent.start()

        assert "TMUX_ORCHESTRATOR_AGENT_ROLE" in captured_env, (
            "pane_env must contain TMUX_ORCHESTRATOR_AGENT_ROLE"
        )
        assert captured_env["TMUX_ORCHESTRATOR_AGENT_ROLE"] == "worker", (
            "TMUX_ORCHESTRATOR_AGENT_ROLE must be 'worker' for WORKER role"
        )

    @pytest.mark.asyncio
    async def test_pane_env_includes_plugin_docs_dir(self) -> None:
        """start() must set TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR in pane_env."""
        agent = _make_agent(role=AgentRole.WORKER)
        pane, captured_env = _make_fake_pane()

        def fake_new_pane(agent_id: str, env: dict[str, str]) -> MagicMock:
            captured_env.update(env)
            return pane

        agent._tmux.new_pane.side_effect = fake_new_pane
        agent._tmux.watch_pane = MagicMock()
        agent._tmux.start_watcher = MagicMock()

        with (
            patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=None),
            patch.object(agent, "_wait_for_ready", new_callable=AsyncMock),
            patch.object(agent, "_run_loop", new_callable=AsyncMock),
            patch.object(agent, "_start_message_loop", new_callable=AsyncMock),
        ):
            await agent.start()

        assert "TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR" in captured_env, (
            "pane_env must contain TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR"
        )
        docs_dir_value = captured_env["TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR"]
        assert docs_dir_value.endswith("agent_plugin/docs"), (
            f"TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR must point to agent_plugin/docs, got: {docs_dir_value}"
        )

    @pytest.mark.asyncio
    async def test_pane_env_director_role_value(self) -> None:
        """start() must set TMUX_ORCHESTRATOR_AGENT_ROLE='director' for DIRECTOR role."""
        agent = _make_agent(role=AgentRole.DIRECTOR)
        pane, captured_env = _make_fake_pane()

        def fake_new_pane(agent_id: str, env: dict[str, str]) -> MagicMock:
            captured_env.update(env)
            return pane

        agent._tmux.new_pane.side_effect = fake_new_pane
        agent._tmux.watch_pane = MagicMock()
        agent._tmux.start_watcher = MagicMock()

        with (
            patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=None),
            patch.object(agent, "_wait_for_ready", new_callable=AsyncMock),
            patch.object(agent, "_run_loop", new_callable=AsyncMock),
            patch.object(agent, "_start_message_loop", new_callable=AsyncMock),
        ):
            await agent.start()

        assert captured_env.get("TMUX_ORCHESTRATOR_AGENT_ROLE") == "director", (
            "TMUX_ORCHESTRATOR_AGENT_ROLE must be 'director' for DIRECTOR role"
        )

    @pytest.mark.asyncio
    async def test_plugin_docs_dir_env_points_to_existing_directory(self) -> None:
        """TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR must point to a real directory."""
        agent = _make_agent(role=AgentRole.WORKER)
        pane, captured_env = _make_fake_pane()

        def fake_new_pane(agent_id: str, env: dict[str, str]) -> MagicMock:
            captured_env.update(env)
            return pane

        agent._tmux.new_pane.side_effect = fake_new_pane
        agent._tmux.watch_pane = MagicMock()
        agent._tmux.start_watcher = MagicMock()

        with (
            patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=None),
            patch.object(agent, "_wait_for_ready", new_callable=AsyncMock),
            patch.object(agent, "_run_loop", new_callable=AsyncMock),
            patch.object(agent, "_start_message_loop", new_callable=AsyncMock),
        ):
            await agent.start()

        docs_dir = Path(captured_env["TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR"])
        assert docs_dir.is_dir(), (
            f"TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR={docs_dir} must be an existing directory"
        )


# ---------------------------------------------------------------------------
# 5. agent_plugin/docs/worker.md and director.md exist at the new path
# ---------------------------------------------------------------------------


class TestDocsFilesExistAtNewPath:
    def test_worker_md_exists_in_docs(self) -> None:
        """worker.md must exist in agent_plugin/docs/."""
        import tmux_orchestrator.agents.claude_code as cc
        docs_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "docs"
        assert (docs_dir / "worker.md").exists(), "agent_plugin/docs/worker.md missing"

    def test_director_md_exists_in_docs(self) -> None:
        """director.md must exist in agent_plugin/docs/."""
        import tmux_orchestrator.agents.claude_code as cc
        docs_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "docs"
        assert (docs_dir / "director.md").exists(), "agent_plugin/docs/director.md missing"

    def test_rules_directory_does_not_exist(self) -> None:
        """agent_plugin/rules/ must NOT exist (renamed to docs/)."""
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        assert not rules_dir.exists(), (
            f"agent_plugin/rules/ must not exist (renamed to docs/); found at {rules_dir}"
        )

    def test_worker_md_mentions_task_complete(self) -> None:
        """worker.md should reference /task-complete."""
        import tmux_orchestrator.agents.claude_code as cc
        docs_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "docs"
        content = (docs_dir / "worker.md").read_text()
        assert "/task-complete" in content

    def test_director_md_mentions_task_complete(self) -> None:
        """director.md should reference task-complete REST endpoint."""
        import tmux_orchestrator.agents.claude_code as cc
        docs_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "docs"
        content = (docs_dir / "director.md").read_text()
        assert "task-complete" in content

    def test_worker_md_under_80_lines(self) -> None:
        """worker.md should be concise (< 80 lines)."""
        import tmux_orchestrator.agents.claude_code as cc
        docs_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "docs"
        lines = (docs_dir / "worker.md").read_text().splitlines()
        assert len(lines) < 80, f"worker.md too long: {len(lines)} lines"

    def test_director_md_under_100_lines(self) -> None:
        """director.md should be concise (< 100 lines)."""
        import tmux_orchestrator.agents.claude_code as cc
        docs_dir = Path(cc.__file__).parent.parent / "agent_plugin" / "docs"
        lines = (docs_dir / "director.md").read_text().splitlines()
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
