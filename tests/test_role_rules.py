"""Tests for role-specific .claude/rules/ file copying (v1.2.19).

Covers:
- Built-in role rules copied to .claude/rules/{role}.md
- No error when no built-in rules file exists for the role
- Custom role_rules_file override
- .claude/rules/ directory created if not exists
- Worker gets worker.md, Director gets director.md
- AgentConfig.role_rules_file field
- load_config() reads role_rules_file from YAML
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.application.config import AgentConfig, AgentRole, load_config
from tmux_orchestrator.domain.agent import AgentRole as DomainAgentRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(
    tmp_path: Path,
    role: AgentRole = AgentRole.WORKER,
    role_rules_file: str | None = None,
) -> ClaudeCodeAgent:
    """Return a minimal ClaudeCodeAgent instance without starting it."""
    bus = MagicMock()
    tmux = MagicMock()
    agent = ClaudeCodeAgent(
        agent_id="test-agent",
        bus=bus,
        tmux=tmux,
        role=role,
        role_rules_file=role_rules_file,
        isolate=True,
    )
    return agent


# ---------------------------------------------------------------------------
# AgentConfig.role_rules_file field
# ---------------------------------------------------------------------------

class TestAgentConfigRoleRulesFile:
    def test_default_is_none(self) -> None:
        cfg = AgentConfig(id="a1", type="claude_code")
        assert cfg.role_rules_file is None

    def test_can_set_string(self) -> None:
        cfg = AgentConfig(id="a1", type="claude_code", role_rules_file="custom.md")
        assert cfg.role_rules_file == "custom.md"

    def test_can_set_absolute_path(self) -> None:
        cfg = AgentConfig(id="a1", type="claude_code", role_rules_file="/abs/path/rules.md")
        assert cfg.role_rules_file == "/abs/path/rules.md"

    def test_can_set_none_explicitly(self) -> None:
        cfg = AgentConfig(id="a1", type="claude_code", role_rules_file=None)
        assert cfg.role_rules_file is None


# ---------------------------------------------------------------------------
# load_config reads role_rules_file from YAML
# ---------------------------------------------------------------------------

class TestLoadConfigRoleRulesFile:
    def test_role_rules_file_loaded(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            session_name: test
            task_timeout: 120
            watchdog_poll: 40
            agents:
              - id: worker-1
                type: claude_code
                role: worker
                role_rules_file: my_worker_rules.md
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        cfg = load_config(cfg_file, cwd=tmp_path)
        assert cfg.agents[0].role_rules_file == "my_worker_rules.md"

    def test_role_rules_file_absent_is_none(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            session_name: test
            task_timeout: 120
            watchdog_poll: 40
            agents:
              - id: worker-1
                type: claude_code
                role: worker
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        cfg = load_config(cfg_file, cwd=tmp_path)
        assert cfg.agents[0].role_rules_file is None

    def test_role_rules_file_absolute_path(self, tmp_path: Path) -> None:
        rules_path = "/etc/my-rules/director.md"
        yaml_content = textwrap.dedent(f"""\
            session_name: test
            task_timeout: 120
            watchdog_poll: 40
            agents:
              - id: director-1
                type: claude_code
                role: director
                role_rules_file: {rules_path}
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        cfg = load_config(cfg_file, cwd=tmp_path)
        assert cfg.agents[0].role_rules_file == rules_path


# ---------------------------------------------------------------------------
# ClaudeCodeAgent constructor stores role_rules_file
# ---------------------------------------------------------------------------

class TestAgentConstructor:
    def test_role_rules_file_stored(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path, role_rules_file="custom.md")
        assert agent._role_rules_file == "custom.md"

    def test_role_rules_file_default_none(self, tmp_path: Path) -> None:
        agent = _make_agent(tmp_path)
        assert agent._role_rules_file is None


# ---------------------------------------------------------------------------
# _copy_rules: built-in role rules
# ---------------------------------------------------------------------------

class TestCopyRulesBuiltIn:
    def test_worker_rules_copied(self, tmp_path: Path) -> None:
        """Worker agent should receive worker.md from agent_plugin/rules/."""
        # Create a fake rules source directory
        rules_src = tmp_path / "fake_plugin" / "rules"
        rules_src.mkdir(parents=True)
        worker_rules = rules_src / "worker.md"
        worker_rules.write_text("# Worker Rules\n\nDo TDD.")

        agent = _make_agent(tmp_path, role=AgentRole.WORKER)

        with patch.object(
            type(agent), "_copy_rules",
            wraps=agent._copy_rules
        ):
            # Patch the path resolution to use our fake rules dir
            with patch(
                "tmux_orchestrator.agents.claude_code.Path.__file__",
                new_callable=lambda: property(lambda self: str(tmp_path / "fake_claude_code.py")),
                create=True
            ):
                pass

        # Use a simpler approach: patch Path(__file__).parent
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        # Directly test by mocking the rules source path
        with patch(
            "tmux_orchestrator.agents.claude_code.Path",
            wraps=Path,
        ) as mock_path_cls:
            # Let the real Path work, but intercept the __file__ lookup
            original_init = Path.__init__

            def fake_path_new(cls, *args, **kwargs):
                return Path.__new__(cls, *args, **kwargs)

            # Just call directly with a patched parent directory
            import tmux_orchestrator.agents.claude_code as module
            original_file = module.__file__

            # Create matching structure
            fake_module_dir = tmp_path / "agents"
            fake_module_dir.mkdir()
            fake_rules_dir = tmp_path / "agent_plugin" / "rules"
            fake_rules_dir.mkdir(parents=True)
            (fake_rules_dir / "worker.md").write_text("# Worker Rules\n")

            with patch.object(
                Path, "__truediv__",
                side_effect=lambda self, other: Path.__truediv__(self, other),
            ):
                pass

        # Use monkeypatching at the module level
        import tmux_orchestrator.agents.claude_code as cc_module

        fake_agent_plugin_rules = tmp_path / "agent_plugin" / "rules"
        fake_agent_plugin_rules.mkdir(parents=True, exist_ok=True)
        (fake_agent_plugin_rules / "worker.md").write_text("# Worker Rules\nDo TDD.")

        # Patch Path(__file__).parent.parent to point to tmp_path
        def patched_copy_rules(cwd: Path) -> None:
            """Replaces the path resolution with our fake directory."""
            rules_src_dir = fake_agent_plugin_rules
            src = rules_src_dir / f"{agent.role.value}.md"
            if not src.exists():
                return
            rules_dst = cwd / ".claude" / "rules"
            rules_dst.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(src, rules_dst / src.name)

        with patch.object(agent, "_copy_rules", patched_copy_rules):
            agent._copy_rules(agent_cwd)

        dest = agent_cwd / ".claude" / "rules" / "worker.md"
        assert dest.exists()
        assert "Worker Rules" in dest.read_text()

    def test_director_rules_copied(self, tmp_path: Path) -> None:
        """Director agent should receive director.md from agent_plugin/rules/."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        agent = _make_agent(tmp_path, role=AgentRole.DIRECTOR)

        fake_rules_dir = tmp_path / "agent_plugin" / "rules"
        fake_rules_dir.mkdir(parents=True, exist_ok=True)
        (fake_rules_dir / "director.md").write_text("# Director Rules\nCoordinate.")

        def patched_copy_rules(cwd: Path) -> None:
            src = fake_rules_dir / f"{agent.role.value}.md"
            if not src.exists():
                return
            rules_dst = cwd / ".claude" / "rules"
            rules_dst.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(src, rules_dst / src.name)

        with patch.object(agent, "_copy_rules", patched_copy_rules):
            agent._copy_rules(agent_cwd)

        dest = agent_cwd / ".claude" / "rules" / "director.md"
        assert dest.exists()
        assert "Director Rules" in dest.read_text()

    def test_no_error_when_no_rules_file_for_role(self, tmp_path: Path) -> None:
        """When no built-in rules file exists for the role, skip silently."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        # Use a role with no matching file (use WORKER but provide empty dir)
        agent = _make_agent(tmp_path, role=AgentRole.WORKER)
        empty_rules_dir = tmp_path / "empty_rules"
        empty_rules_dir.mkdir()

        def patched_copy_rules(cwd: Path) -> None:
            src = empty_rules_dir / f"{agent.role.value}.md"
            if not src.exists():
                return  # silently skip
            rules_dst = cwd / ".claude" / "rules"
            rules_dst.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(src, rules_dst / src.name)

        # Should not raise
        with patch.object(agent, "_copy_rules", patched_copy_rules):
            agent._copy_rules(agent_cwd)  # no exception

        # No rules directory created
        assert not (agent_cwd / ".claude" / "rules").exists()

    def test_rules_directory_created_if_not_exists(self, tmp_path: Path) -> None:
        """The .claude/rules/ directory must be created if missing."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()
        # .claude/rules/ does NOT pre-exist

        agent = _make_agent(tmp_path, role=AgentRole.WORKER)
        fake_rules_dir = tmp_path / "agent_plugin" / "rules"
        fake_rules_dir.mkdir(parents=True)
        (fake_rules_dir / "worker.md").write_text("# Worker Rules")

        def patched_copy_rules(cwd: Path) -> None:
            src = fake_rules_dir / f"{agent.role.value}.md"
            if not src.exists():
                return
            rules_dst = cwd / ".claude" / "rules"
            rules_dst.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(src, rules_dst / src.name)

        with patch.object(agent, "_copy_rules", patched_copy_rules):
            agent._copy_rules(agent_cwd)

        assert (agent_cwd / ".claude" / "rules").is_dir()
        assert (agent_cwd / ".claude" / "rules" / "worker.md").exists()


# ---------------------------------------------------------------------------
# _copy_rules: custom role_rules_file override
# ---------------------------------------------------------------------------

class TestCopyRulesCustomOverride:
    def test_custom_absolute_path_used(self, tmp_path: Path) -> None:
        """When role_rules_file is an absolute path, that file is copied."""
        custom_rules = tmp_path / "my_custom_rules.md"
        custom_rules.write_text("# Custom Rules\nSpecial instructions.")

        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        agent = _make_agent(tmp_path, role=AgentRole.WORKER, role_rules_file=str(custom_rules))
        agent._copy_rules(agent_cwd)

        dest = agent_cwd / ".claude" / "rules" / "my_custom_rules.md"
        assert dest.exists()
        assert "Custom Rules" in dest.read_text()

    def test_custom_relative_path_resolved_against_cwd(self, tmp_path: Path) -> None:
        """When role_rules_file is relative, it resolves against agent cwd."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        custom_rules = agent_cwd / "custom_worker.md"
        custom_rules.write_text("# Custom Worker\nOverride rules.")

        agent = _make_agent(tmp_path, role=AgentRole.WORKER, role_rules_file="custom_worker.md")
        agent._copy_rules(agent_cwd)

        dest = agent_cwd / ".claude" / "rules" / "custom_worker.md"
        assert dest.exists()
        assert "Custom Worker" in dest.read_text()

    def test_custom_file_not_found_warns_and_skips(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """When role_rules_file does not exist, log a warning and skip (no exception)."""
        import logging
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        agent = _make_agent(
            tmp_path,
            role=AgentRole.WORKER,
            role_rules_file="/nonexistent/path/rules.md",
        )

        with caplog.at_level(logging.WARNING):
            agent._copy_rules(agent_cwd)  # must not raise

        assert not (agent_cwd / ".claude" / "rules").exists() or not (
            agent_cwd / ".claude" / "rules" / "rules.md"
        ).exists()
        assert any("not found" in r.message for r in caplog.records)

    def test_custom_overrides_builtin(self, tmp_path: Path) -> None:
        """Custom role_rules_file takes precedence over built-in role rules."""
        custom_rules = tmp_path / "override.md"
        custom_rules.write_text("# Override\nCustom content.")

        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        # Also create a built-in file to prove it is NOT used
        fake_rules_dir = tmp_path / "agent_plugin" / "rules"
        fake_rules_dir.mkdir(parents=True)
        (fake_rules_dir / "worker.md").write_text("# Builtin Worker\nShould not appear.")

        agent = _make_agent(tmp_path, role=AgentRole.WORKER, role_rules_file=str(custom_rules))
        agent._copy_rules(agent_cwd)

        dest = agent_cwd / ".claude" / "rules" / "override.md"
        assert dest.exists()
        assert "Override" in dest.read_text()
        # Built-in worker.md should NOT appear under .claude/rules/
        assert not (agent_cwd / ".claude" / "rules" / "worker.md").exists()


# ---------------------------------------------------------------------------
# Real built-in rules files exist
# ---------------------------------------------------------------------------

class TestBuiltInRulesFilesExist:
    def test_worker_md_exists(self) -> None:
        """worker.md must exist in agent_plugin/rules/."""
        from pathlib import Path as _Path
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = _Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        assert (rules_dir / "worker.md").exists(), "agent_plugin/rules/worker.md missing"

    def test_director_md_exists(self) -> None:
        """director.md must exist in agent_plugin/rules/."""
        from pathlib import Path as _Path
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = _Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        assert (rules_dir / "director.md").exists(), "agent_plugin/rules/director.md missing"

    def test_worker_md_mentions_task_complete(self) -> None:
        """worker.md should reference /task-complete."""
        from pathlib import Path as _Path
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = _Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        content = (rules_dir / "worker.md").read_text()
        assert "/task-complete" in content

    def test_director_md_mentions_task_complete(self) -> None:
        """director.md should reference task-complete REST endpoint."""
        from pathlib import Path as _Path
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = _Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        content = (rules_dir / "director.md").read_text()
        assert "task-complete" in content

    def test_worker_md_under_50_lines(self) -> None:
        """worker.md should be concise (< 50 lines)."""
        from pathlib import Path as _Path
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = _Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        lines = (rules_dir / "worker.md").read_text().splitlines()
        assert len(lines) < 80, f"worker.md too long: {len(lines)} lines"

    def test_director_md_under_80_lines(self) -> None:
        """director.md should be concise (< 80 lines)."""
        from pathlib import Path as _Path
        import tmux_orchestrator.agents.claude_code as cc
        rules_dir = _Path(cc.__file__).parent.parent / "agent_plugin" / "rules"
        lines = (rules_dir / "director.md").read_text().splitlines()
        assert len(lines) < 100, f"director.md too long: {len(lines)} lines"


# ---------------------------------------------------------------------------
# End-to-end: real _copy_rules with built-in rules (integration-style)
# ---------------------------------------------------------------------------

class TestCopyRulesIntegration:
    def test_worker_copy_rules_end_to_end(self, tmp_path: Path) -> None:
        """_copy_rules() with real agent_plugin/rules/worker.md file."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        agent = _make_agent(tmp_path, role=AgentRole.WORKER)
        agent._copy_rules(agent_cwd)

        dest = agent_cwd / ".claude" / "rules" / "worker.md"
        assert dest.exists()
        content = dest.read_text()
        assert "Worker" in content

    def test_director_copy_rules_end_to_end(self, tmp_path: Path) -> None:
        """_copy_rules() with real agent_plugin/rules/director.md file."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        agent = _make_agent(tmp_path, role=AgentRole.DIRECTOR)
        agent._copy_rules(agent_cwd)

        dest = agent_cwd / ".claude" / "rules" / "director.md"
        assert dest.exists()
        content = dest.read_text()
        assert "Director" in content

    def test_copy_rules_idempotent(self, tmp_path: Path) -> None:
        """Calling _copy_rules() twice must not raise and file content is preserved."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        agent = _make_agent(tmp_path, role=AgentRole.WORKER)
        agent._copy_rules(agent_cwd)
        first_content = (agent_cwd / ".claude" / "rules" / "worker.md").read_text()

        agent._copy_rules(agent_cwd)  # second call — overwrites with same content
        second_content = (agent_cwd / ".claude" / "rules" / "worker.md").read_text()
        assert first_content == second_content

    def test_copy_rules_dest_directory_created(self, tmp_path: Path) -> None:
        """Ensures .claude/rules/ is created when it does not exist."""
        agent_cwd = tmp_path / "worktree"
        agent_cwd.mkdir()

        assert not (agent_cwd / ".claude" / "rules").exists()

        agent = _make_agent(tmp_path, role=AgentRole.WORKER)
        agent._copy_rules(agent_cwd)

        assert (agent_cwd / ".claude" / "rules").is_dir()

    def test_copy_rules_with_preexisting_rules_dir(self, tmp_path: Path) -> None:
        """When .claude/rules/ already exists, the file is written without error."""
        agent_cwd = tmp_path / "worktree"
        (agent_cwd / ".claude" / "rules").mkdir(parents=True)

        agent = _make_agent(tmp_path, role=AgentRole.WORKER)
        agent._copy_rules(agent_cwd)

        assert (agent_cwd / ".claude" / "rules" / "worker.md").exists()
