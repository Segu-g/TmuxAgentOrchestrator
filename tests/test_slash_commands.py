"""Tests for the agent plugin's bundled slash commands.

Since v1.0.10, slash commands are delivered via the ``agent_plugin/commands/``
directory in the installed package, loaded by Claude Code via ``--plugin-dir``.
The old ``_copy_slash_commands()`` method and ``src/tmux_orchestrator/commands/``
directory have been removed.

Design reference: DESIGN.md §10.latest (v1.0.10)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent


# ---------------------------------------------------------------------------
# Plugin directory structure tests
# ---------------------------------------------------------------------------


def _plugin_dir() -> Path:
    return Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "agent_plugin"


def _plugin_commands_dir() -> Path:
    return _plugin_dir() / "commands"


def test_plugin_dir_exists() -> None:
    """The agent_plugin directory must exist in the package source tree."""
    assert _plugin_dir().is_dir(), f"Plugin directory missing: {_plugin_dir()}"


def test_plugin_json_exists() -> None:
    """The plugin must have a .claude-plugin/plugin.json manifest."""
    plugin_json = _plugin_dir() / ".claude-plugin" / "plugin.json"
    assert plugin_json.exists(), f"plugin.json missing: {plugin_json}"


def test_plugin_json_has_required_fields() -> None:
    """plugin.json must contain name, version, and description fields."""
    import json

    plugin_json = _plugin_dir() / ".claude-plugin" / "plugin.json"
    data = json.loads(plugin_json.read_text())
    assert "name" in data, "plugin.json must have 'name' field"
    assert "version" in data, "plugin.json must have 'version' field"
    assert "description" in data, "plugin.json must have 'description' field"
    assert data["name"] == "tmux-orchestrator"


def test_hooks_json_exists() -> None:
    """The plugin must have a hooks/hooks.json file."""
    hooks_json = _plugin_dir() / "hooks" / "hooks.json"
    assert hooks_json.exists(), f"hooks.json missing: {hooks_json}"


def test_hooks_json_has_session_start() -> None:
    """hooks.json must define a SessionStart hook."""
    import json

    hooks_json = _plugin_dir() / "hooks" / "hooks.json"
    data = json.loads(hooks_json.read_text())
    assert "SessionStart" in data.get("hooks", {}), "hooks.json must define SessionStart hook"


def test_session_start_sh_exists_and_is_executable() -> None:
    """session-start.sh must exist and be executable."""
    import os

    script = _plugin_dir() / "scripts" / "session-start.sh"
    assert script.exists(), f"session-start.sh missing: {script}"
    assert os.access(str(script), os.X_OK), "session-start.sh must be executable"


def test_session_start_sh_calls_ready_endpoint() -> None:
    """session-start.sh must call the /ready endpoint using env vars."""
    script = _plugin_dir() / "scripts" / "session-start.sh"
    content = script.read_text()
    assert "TMUX_ORCHESTRATOR_WEB_BASE_URL" in content
    assert "TMUX_ORCHESTRATOR_AGENT_ID" in content
    assert "/ready" in content


def test_plugin_commands_dir_exists() -> None:
    """The bundled commands directory must exist inside the plugin."""
    assert _plugin_commands_dir().is_dir(), (
        f"Plugin commands directory missing: {_plugin_commands_dir()}"
    )


def test_plugin_commands_has_core_files() -> None:
    """All core slash command files must be present in the plugin."""
    commands_dir = _plugin_commands_dir()
    md_files = {f.name for f in commands_dir.glob("*.md")}

    expected_core = {
        "send-message.md",
        "check-inbox.md",
        "read-message.md",
        "spawn-subagent.md",
        "list-agents.md",
        "progress.md",
        "summarize.md",
        "plan.md",
        "task-complete.md",
    }
    missing = expected_core - md_files
    assert not missing, f"Missing slash command files in plugin: {missing}"


def test_plugin_commands_not_empty() -> None:
    """Each bundled command file must have non-empty content."""
    for cmd_file in _plugin_commands_dir().glob("*.md"):
        content = cmd_file.read_text().strip()
        assert content, f"{cmd_file.name} is empty"


def test_old_commands_dir_is_gone() -> None:
    """src/tmux_orchestrator/commands/ must no longer exist (replaced by plugin)."""
    old_dir = Path(__file__).parent.parent / "src" / "tmux_orchestrator" / "commands"
    assert not old_dir.exists(), (
        f"Old commands directory still exists: {old_dir} — it should have been removed"
    )


def test_copy_slash_commands_method_is_removed() -> None:
    """ClaudeCodeAgent must no longer have a _copy_slash_commands method."""
    assert not hasattr(ClaudeCodeAgent, "_copy_slash_commands"), (
        "_copy_slash_commands was deleted; it should not exist on ClaudeCodeAgent"
    )


def test_write_startup_hook_method_is_removed() -> None:
    """ClaudeCodeAgent must no longer have a _write_startup_hook method."""
    assert not hasattr(ClaudeCodeAgent, "_write_startup_hook"), (
        "_write_startup_hook was deleted; it should not exist on ClaudeCodeAgent"
    )
