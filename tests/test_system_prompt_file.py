"""Tests for system_prompt_file YAML field and role template loading.

TDD Red phase: these tests must fail before implementation.

References:
- ChatEval ICLR 2024 (arXiv:2308.07201): Role diversity is critical
- CONSENSAGENT ACL 2025: sycophancy suppression via role prompts
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tmux_orchestrator.config import AgentConfig, load_config


# ---------------------------------------------------------------------------
# AgentConfig.system_prompt_file field
# ---------------------------------------------------------------------------

class TestAgentConfigSystemPromptFile:
    def test_default_is_none(self):
        cfg = AgentConfig(id="w1", type="claude_code")
        assert cfg.system_prompt_file is None

    def test_can_set_system_prompt_file(self):
        cfg = AgentConfig(id="w1", type="claude_code", system_prompt_file="roles/tester.md")
        assert cfg.system_prompt_file == "roles/tester.md"

    def test_system_prompt_file_and_system_prompt_are_independent(self):
        """Both fields can coexist; explicit system_prompt takes precedence."""
        cfg = AgentConfig(
            id="w1", type="claude_code",
            system_prompt="explicit prompt",
            system_prompt_file="roles/tester.md",
        )
        assert cfg.system_prompt == "explicit prompt"
        assert cfg.system_prompt_file == "roles/tester.md"


# ---------------------------------------------------------------------------
# load_config: system_prompt_file parsed from YAML
# ---------------------------------------------------------------------------

class TestLoadConfigSystemPromptFile:
    def test_system_prompt_file_loaded_from_yaml(self, tmp_path):
        yaml_content = """\
session_name: test
agents:
  - id: tester-agent
    type: claude_code
    system_prompt_file: .claude/prompts/roles/tester.md
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)
        config = load_config(cfg_path)
        assert config.agents[0].system_prompt_file == ".claude/prompts/roles/tester.md"

    def test_system_prompt_file_defaults_to_none_when_absent(self, tmp_path):
        yaml_content = """\
session_name: test
agents:
  - id: worker-1
    type: claude_code
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)
        config = load_config(cfg_path)
        assert config.agents[0].system_prompt_file is None

    def test_system_prompt_file_and_system_prompt_coexist_in_yaml(self, tmp_path):
        yaml_content = """\
session_name: test
agents:
  - id: worker-1
    type: claude_code
    system_prompt: "explicit prompt here"
    system_prompt_file: .claude/prompts/roles/tester.md
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)
        config = load_config(cfg_path)
        assert config.agents[0].system_prompt == "explicit prompt here"
        assert config.agents[0].system_prompt_file == ".claude/prompts/roles/tester.md"


# ---------------------------------------------------------------------------
# factory.py: build_system resolves system_prompt_file
# ---------------------------------------------------------------------------

class TestBuildSystemResolvesSystemPromptFile:
    def test_system_prompt_file_content_passed_to_agent(self, tmp_path):
        """build_system reads system_prompt_file and passes content to ClaudeCodeAgent."""
        role_dir = tmp_path / ".claude" / "prompts" / "roles"
        role_dir.mkdir(parents=True)
        role_file = role_dir / "tester.md"
        role_file.write_text("# Tester Role\nYou are a test writer.")

        yaml_content = f"""\
session_name: test
mailbox_dir: /tmp/orch-test
agents:
  - id: tester-agent
    type: claude_code
    system_prompt_file: {role_file}
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)

        with (
            patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
            patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError("not git")),
            patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent,
        ):
            MockTmux.return_value = MagicMock()
            mock_agent = MagicMock()
            mock_agent.id = "tester-agent"
            MockAgent.return_value = mock_agent

            from tmux_orchestrator.factory import build_system
            build_system(cfg_path)

            # Check that ClaudeCodeAgent was called with system_prompt containing file content
            call_kwargs = MockAgent.call_args[1]
            assert "# Tester Role" in call_kwargs["system_prompt"]
            assert "You are a test writer." in call_kwargs["system_prompt"]

    def test_system_prompt_explicit_overrides_file(self, tmp_path):
        """When both system_prompt and system_prompt_file given, system_prompt wins."""
        role_dir = tmp_path / ".claude" / "prompts" / "roles"
        role_dir.mkdir(parents=True)
        role_file = role_dir / "tester.md"
        role_file.write_text("# Tester Role\nContent from file.")

        yaml_content = f"""\
session_name: test
mailbox_dir: /tmp/orch-test
agents:
  - id: tester-agent
    type: claude_code
    system_prompt: "I am the explicit prompt"
    system_prompt_file: {role_file}
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)

        with (
            patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
            patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError("not git")),
            patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent,
        ):
            MockTmux.return_value = MagicMock()
            mock_agent = MagicMock()
            mock_agent.id = "tester-agent"
            MockAgent.return_value = mock_agent

            from tmux_orchestrator.factory import build_system
            build_system(cfg_path)

            call_kwargs = MockAgent.call_args[1]
            assert call_kwargs["system_prompt"] == "I am the explicit prompt"

    def test_missing_system_prompt_file_raises_file_not_found(self, tmp_path):
        yaml_content = """\
session_name: test
mailbox_dir: /tmp/orch-test
agents:
  - id: tester-agent
    type: claude_code
    system_prompt_file: /nonexistent/path/to/role.md
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)

        with (
            patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
            patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError("not git")),
        ):
            MockTmux.return_value = MagicMock()
            from tmux_orchestrator.factory import build_system
            with pytest.raises(FileNotFoundError):
                build_system(cfg_path)

    def test_relative_system_prompt_file_resolved_from_config_dir(self, tmp_path):
        """Relative paths in system_prompt_file are resolved from the config file's directory."""
        role_dir = tmp_path / ".claude" / "prompts" / "roles"
        role_dir.mkdir(parents=True)
        role_file = role_dir / "reviewer.md"
        role_file.write_text("# Reviewer Role\nYou are a code reviewer.")

        yaml_content = """\
session_name: test
mailbox_dir: /tmp/orch-test
agents:
  - id: reviewer-agent
    type: claude_code
    system_prompt_file: .claude/prompts/roles/reviewer.md
"""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml_content)

        with (
            patch("tmux_orchestrator.factory.TmuxInterface") as MockTmux,
            patch("tmux_orchestrator.factory.WorktreeManager", side_effect=RuntimeError("not git")),
            patch("tmux_orchestrator.agents.claude_code.ClaudeCodeAgent") as MockAgent,
        ):
            MockTmux.return_value = MagicMock()
            mock_agent = MagicMock()
            mock_agent.id = "reviewer-agent"
            MockAgent.return_value = mock_agent

            from tmux_orchestrator.factory import build_system
            build_system(cfg_path)

            call_kwargs = MockAgent.call_args[1]
            assert "# Reviewer Role" in call_kwargs["system_prompt"]


# ---------------------------------------------------------------------------
# Role template files exist
# ---------------------------------------------------------------------------

ROLES_DIR = Path(__file__).parent.parent / ".claude" / "prompts" / "roles"


@pytest.mark.parametrize("role_file", [
    "advocate.md",
    "critic.md",
    "judge.md",
    "tester.md",
    "implementer.md",
    "reviewer.md",
    "spec-writer.md",
])
def test_role_template_file_exists(role_file):
    """All 7 role template files must exist in .claude/prompts/roles/."""
    path = ROLES_DIR / role_file
    assert path.exists(), f"Role template missing: {path}"


@pytest.mark.parametrize("role_file", [
    "tester.md",
    "implementer.md",
    "reviewer.md",
    "spec-writer.md",
])
def test_new_role_template_contains_sycophancy_suppression(role_file):
    """New role templates must include sycophancy suppression instruction (CONSENSAGENT ACL 2025)."""
    path = ROLES_DIR / role_file
    content = path.read_text().lower()
    # Must mention sycoph* OR "do not agree" OR "maintain your" OR "independent"
    has_suppression = (
        "sycophancy" in content
        or "do not agree" in content
        or "maintain your" in content
        or "independent" in content
        or "without simply agreeing" in content
    )
    assert has_suppression, f"{role_file} missing sycophancy suppression instruction"


@pytest.mark.parametrize("role_file", [
    "tester.md",
    "implementer.md",
    "reviewer.md",
    "spec-writer.md",
])
def test_new_role_template_contains_completion_criteria(role_file):
    """Each role template must specify completion criteria."""
    path = ROLES_DIR / role_file
    content = path.read_text().lower()
    assert "completion" in content or "complete when" in content or "done when" in content


@pytest.mark.parametrize("role_file", [
    "tester.md",
    "implementer.md",
    "reviewer.md",
    "spec-writer.md",
])
def test_new_role_template_contains_prohibited_behaviours(role_file):
    """Each role template must list prohibited behaviours."""
    path = ROLES_DIR / role_file
    content = path.read_text().lower()
    assert "prohibit" in content or "do not" in content or "never" in content
