"""Tests for per-agent subdir isolation when isolate=False (v1.1.35).

When multiple agents share the same cwd (``isolate: false``), all per-agent
files must be written to ``.agent/{agent_id}/`` to prevent file collisions,
especially ``.claude/settings.local.json`` (Stop hook configuration) which
was previously written to the shared cwd root.

When ``isolate=True`` (default), agents have their own worktree and no
``.agent/`` subdir is created.

Design reference: DESIGN.md §10.67 (v1.1.35 — per-agent subdir isolation)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent
from tmux_orchestrator.bus import Bus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bus() -> Bus:
    return Bus()


def make_tmux_mock():
    tmux = MagicMock()
    tmux.new_pane = MagicMock(return_value=MagicMock(id="pane-1"))
    tmux.new_subpane = MagicMock(return_value=MagicMock(id="pane-2"))
    tmux.send_keys = MagicMock()
    tmux.watch_pane = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.capture_pane = MagicMock(return_value="❯ ")
    return tmux


def make_non_isolated_agent(agent_id: str, cwd: Path, *, web_base_url: str = "") -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        agent_id=agent_id,
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=False,
        cwd_override=cwd,
        web_base_url=web_base_url,
    )


def make_isolated_agent(agent_id: str) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(
        agent_id=agent_id,
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=True,
    )


# ---------------------------------------------------------------------------
# _agent_work_dir unit tests
# ---------------------------------------------------------------------------


def test_agent_work_dir_isolated_returns_cwd_unchanged(tmp_path: Path) -> None:
    """When isolate=True, _agent_work_dir must return cwd unmodified."""
    agent = make_isolated_agent("worker-iso")
    result = agent._agent_work_dir(tmp_path)
    assert result == tmp_path, "isolate=True: cwd unchanged"
    assert not (tmp_path / ".agent").exists(), "No .agent dir created for isolated agent"


def test_agent_work_dir_non_isolated_returns_subdir(tmp_path: Path) -> None:
    """When isolate=False, _agent_work_dir must return .agent/{agent_id}/."""
    agent = make_non_isolated_agent("worker-1", tmp_path)
    result = agent._agent_work_dir(tmp_path)
    expected = tmp_path / ".agent" / "worker-1"
    assert result == expected


def test_agent_work_dir_non_isolated_creates_subdir(tmp_path: Path) -> None:
    """_agent_work_dir must create the .agent/{agent_id}/ subdir."""
    agent = make_non_isolated_agent("worker-1", tmp_path)
    agent._agent_work_dir(tmp_path)
    assert (tmp_path / ".agent" / "worker-1").is_dir()


def test_agent_work_dir_two_agents_get_different_subdirs(tmp_path: Path) -> None:
    """Two non-isolated agents sharing cwd must get distinct subdirs."""
    agent_a = make_non_isolated_agent("agent-a", tmp_path)
    agent_b = make_non_isolated_agent("agent-b", tmp_path)
    dir_a = agent_a._agent_work_dir(tmp_path)
    dir_b = agent_b._agent_work_dir(tmp_path)
    assert dir_a != dir_b
    assert dir_a == tmp_path / ".agent" / "agent-a"
    assert dir_b == tmp_path / ".agent" / "agent-b"


# ---------------------------------------------------------------------------
# Integration: start() wires all files to the subdir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_isolated_context_file_goes_to_subdir(tmp_path: Path) -> None:
    """For isolate=False, __orchestrator_context__{id}__.json must be inside .agent/{id}/."""
    agent = make_non_isolated_agent("worker-ctx", tmp_path)
    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
            with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                await agent.start()

    subdir = tmp_path / ".agent" / "worker-ctx"
    ctx_file = subdir / "__orchestrator_context__worker-ctx__.json"
    assert ctx_file.exists(), f"Context file must be in subdir: {ctx_file}"

    # Must NOT be in the shared cwd root
    root_ctx = tmp_path / "__orchestrator_context__worker-ctx__.json"
    assert not root_ctx.exists(), "Context file must not be at cwd root"

    await agent.stop()


@pytest.mark.asyncio
async def test_non_isolated_no_api_key_file_written(tmp_path: Path) -> None:
    """For isolate=False, no API key file must be written to disk (env-var only, v1.2.18+)."""
    agent = ClaudeCodeAgent(
        agent_id="worker-key",
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=False,
        cwd_override=tmp_path,
        api_key="test-key-12345",
    )
    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
            with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                await agent.start()

    # No key files must exist anywhere
    key_files = list(tmp_path.rglob("__orchestrator_api_key__*"))
    assert key_files == [], f"No API key files must be written to disk: {key_files}"

    await agent.stop()


@pytest.mark.asyncio
async def test_non_isolated_settings_local_json_goes_to_subdir(tmp_path: Path) -> None:
    """For isolate=False, .claude/settings.local.json must be inside .agent/{id}/."""
    agent = ClaudeCodeAgent(
        agent_id="worker-hook",
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=False,
        cwd_override=tmp_path,
        web_base_url="http://localhost:8000",
    )
    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
            with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                await agent.start()

    subdir = tmp_path / ".agent" / "worker-hook"
    settings_file = subdir / ".claude" / "settings.local.json"
    assert settings_file.exists(), "settings.local.json must be in the agent subdir"

    settings = json.loads(settings_file.read_text())
    assert "hooks" in settings, "settings.local.json must contain hooks"
    assert "Stop" in settings["hooks"]

    # Must NOT be at cwd root
    root_settings = tmp_path / ".claude" / "settings.local.json"
    assert not root_settings.exists(), "settings.local.json must not be at cwd root"

    await agent.stop()


@pytest.mark.asyncio
async def test_non_isolated_commands_go_to_subdir(tmp_path: Path) -> None:
    """For isolate=False, slash commands must be copied to .agent/{id}/.claude/commands/."""
    agent = make_non_isolated_agent("worker-cmd", tmp_path)

    commands_called_with = []

    def track(cwd: Path) -> None:
        commands_called_with.append(cwd)

    with patch.object(agent, "_copy_commands", side_effect=track):
        with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
            with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
                with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                    await agent.start()

    assert len(commands_called_with) == 1
    expected = tmp_path / ".agent" / "worker-cmd"
    assert commands_called_with[0] == expected

    await agent.stop()


@pytest.mark.asyncio
async def test_non_isolated_does_not_overwrite_shared_claude_md(tmp_path: Path) -> None:
    """For isolate=False, the project-level CLAUDE.md in the shared cwd must not be touched."""
    original = "# My Project\nShared project instructions."
    (tmp_path / "CLAUDE.md").write_text(original)

    agent = make_non_isolated_agent("worker-md", tmp_path)
    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
            with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                await agent.start()

    assert (tmp_path / "CLAUDE.md").read_text() == original, "Shared CLAUDE.md must not be touched"

    # Agent gets its own CLAUDE.md inside the subdir
    agent_md = tmp_path / ".agent" / "worker-md" / "CLAUDE.md"
    assert agent_md.exists(), "Agent must have CLAUDE.md in its subdir"

    await agent.stop()


@pytest.mark.asyncio
async def test_non_isolated_claude_launched_from_subdir(tmp_path: Path) -> None:
    """For isolate=False, the claude command must cd to .agent/{id}/ before launching."""
    agent = make_non_isolated_agent("worker-launch", tmp_path)
    sent_keys = []

    # Patch TmuxProcessAdapter so we can capture the send_keys call that launches claude
    mock_process = MagicMock()
    mock_process.send_keys = lambda keys: sent_keys.append(keys)
    mock_process.get_pane_id = MagicMock(return_value="pane-1")

    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
            with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                with patch(
                    "tmux_orchestrator.agents.claude_code.TmuxProcessAdapter",
                    return_value=mock_process,
                ):
                    await agent.start()

    assert sent_keys, "send_keys must have been called"
    launch_cmd = sent_keys[0]
    expected_subdir = str(tmp_path / ".agent" / "worker-launch")
    assert expected_subdir in launch_cmd, (
        f"Launch command must cd to agent subdir {expected_subdir!r}, got: {launch_cmd!r}"
    )

    await agent.stop()


# ---------------------------------------------------------------------------
# isolate=True: no .agent/ subdir created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolated_agent_no_agent_subdir_created(tmp_path: Path) -> None:
    """When isolate=True, no .agent/ directory should be created."""
    worktree = tmp_path / "worktrees" / "worker-iso"
    worktree.mkdir(parents=True)

    wm = MagicMock()
    wm.setup = MagicMock(return_value=worktree)
    wm.teardown = MagicMock()

    agent = ClaudeCodeAgent(
        agent_id="worker-iso",
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=True,
        worktree_manager=wm,
    )
    with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
        with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
            await agent.start()

    assert not (worktree / ".agent").exists(), "No .agent dir for isolated agent"
    # Files must be directly in worktree
    ctx_file = worktree / "__orchestrator_context__worker-iso__.json"
    assert ctx_file.exists(), "Context file must be directly in worktree"

    await agent.stop()


# ---------------------------------------------------------------------------
# Trust: pre_trust_worktree called with agent subdir, not shared cwd root
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_isolated_trust_called_with_subdir(tmp_path: Path) -> None:
    """pre_trust_worktree must be called with the agent subdir, not the shared cwd."""
    agent = make_non_isolated_agent("worker-trust", tmp_path)

    trusted_paths = []

    with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree", side_effect=trusted_paths.append):
        with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
            with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
                await agent.start()

    assert len(trusted_paths) == 1, "pre_trust_worktree called exactly once"
    expected = tmp_path / ".agent" / "worker-trust"
    assert trusted_paths[0] == expected, (
        f"Must trust agent subdir {expected!r}, not shared root {tmp_path!r}"
    )

    await agent.stop()


# ---------------------------------------------------------------------------
# Two agents sharing cwd: no file collisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_non_isolated_agents_no_settings_collision(tmp_path: Path) -> None:
    """Two isolate=False agents with the same cwd must have separate settings.local.json files."""
    agent_a = ClaudeCodeAgent(
        agent_id="a-worker",
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=False,
        cwd_override=tmp_path,
        web_base_url="http://localhost:8000",
    )
    agent_b = ClaudeCodeAgent(
        agent_id="b-worker",
        bus=make_bus(),
        tmux=make_tmux_mock(),
        isolate=False,
        cwd_override=tmp_path,
        web_base_url="http://localhost:8000",
    )

    for agent in (agent_a, agent_b):
        with patch.object(agent, "_wait_for_ready", new_callable=AsyncMock):
            with patch.object(agent, "_setup_worktree", new_callable=AsyncMock, return_value=tmp_path):
                with patch("tmux_orchestrator.agents.claude_code.pre_trust_worktree"):
                    await agent.start()

    settings_a = tmp_path / ".agent" / "a-worker" / ".claude" / "settings.local.json"
    settings_b = tmp_path / ".agent" / "b-worker" / ".claude" / "settings.local.json"

    assert settings_a.exists(), "Agent A must have its own settings.local.json"
    assert settings_b.exists(), "Agent B must have its own settings.local.json"
    assert settings_a != settings_b, "Distinct file paths — no collision"

    sa = json.loads(settings_a.read_text())
    sb = json.loads(settings_b.read_text())
    # Each agent's stop hook URL references its own agent_id
    stop_a = str(sa)
    stop_b = str(sb)
    assert "a-worker" in stop_a
    assert "b-worker" in stop_b

    for agent in (agent_a, agent_b):
        await agent.stop()
