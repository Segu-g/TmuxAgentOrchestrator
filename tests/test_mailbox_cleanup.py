"""Tests for mailbox auto-cleanup on Orchestrator.stop().

Feature: v1.1.34 — mailbox_cleanup_on_stop (DESIGN.md §10.66)

When ``OrchestratorConfig.mailbox_cleanup_on_stop`` is True (default), calling
``Orchestrator.stop()`` must delete the session-scoped mailbox directory at
``{mailbox_dir}/{session_name}/`` after all agents have stopped.

When the flag is False, the directory must be left untouched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tmux_orchestrator.application.config import OrchestratorConfig
from tmux_orchestrator.application.orchestrator import Orchestrator
from tmux_orchestrator.bus import Bus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path, *, cleanup: bool = True, session: str = "test") -> OrchestratorConfig:
    """Return a minimal OrchestratorConfig pointing at tmp_path for mailbox."""
    return OrchestratorConfig(
        session_name=session,
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        mailbox_dir=str(tmp_path / "mailbox"),
        mailbox_cleanup_on_stop=cleanup,
        # Disable watchdog so stop() is fast in tests
        watchdog_poll=10.0,
    )


def make_tmux_mock() -> MagicMock:
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    return tmux


def create_session_mailbox(mailbox_dir: str, session_name: str) -> Path:
    """Create the session-scoped mailbox directory and a dummy file inside it."""
    session_dir = Path(mailbox_dir) / session_name
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "msg-001.json").write_text('{"id": "1"}')
    return session_dir


# ---------------------------------------------------------------------------
# Unit tests — OrchestratorConfig field
# ---------------------------------------------------------------------------


def test_default_mailbox_cleanup_on_stop_is_true() -> None:
    """OrchestratorConfig.mailbox_cleanup_on_stop defaults to True."""
    cfg = OrchestratorConfig(task_timeout=10)
    assert cfg.mailbox_cleanup_on_stop is True


def test_mailbox_cleanup_on_stop_can_be_disabled() -> None:
    """OrchestratorConfig.mailbox_cleanup_on_stop can be set to False."""
    cfg = OrchestratorConfig(task_timeout=10, mailbox_cleanup_on_stop=False)
    assert cfg.mailbox_cleanup_on_stop is False


# ---------------------------------------------------------------------------
# Unit tests — load_config YAML round-trip
# ---------------------------------------------------------------------------


def test_load_config_default_mailbox_cleanup(tmp_path: Path) -> None:
    """load_config without mailbox_cleanup_on_stop key defaults to True."""
    from tmux_orchestrator.application.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "session_name: demo\n"
        "agents: []\n"
        "task_timeout: 120\n"
        "watchdog_poll: 40\n"
    )
    cfg = load_config(cfg_file, cwd=tmp_path)
    assert cfg.mailbox_cleanup_on_stop is True


def test_load_config_mailbox_cleanup_false(tmp_path: Path) -> None:
    """load_config reads mailbox_cleanup_on_stop: false from YAML."""
    from tmux_orchestrator.application.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "session_name: demo\n"
        "agents: []\n"
        "task_timeout: 120\n"
        "watchdog_poll: 40\n"
        "mailbox_cleanup_on_stop: false\n"
    )
    cfg = load_config(cfg_file, cwd=tmp_path)
    assert cfg.mailbox_cleanup_on_stop is False


def test_load_config_mailbox_cleanup_true_explicit(tmp_path: Path) -> None:
    """load_config reads mailbox_cleanup_on_stop: true from YAML."""
    from tmux_orchestrator.application.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "session_name: demo\n"
        "agents: []\n"
        "task_timeout: 120\n"
        "watchdog_poll: 40\n"
        "mailbox_cleanup_on_stop: true\n"
    )
    cfg = load_config(cfg_file, cwd=tmp_path)
    assert cfg.mailbox_cleanup_on_stop is True


# ---------------------------------------------------------------------------
# Integration tests — Orchestrator.stop() with real mailbox directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_deletes_session_mailbox_when_cleanup_enabled(tmp_path: Path) -> None:
    """stop() deletes {mailbox_dir}/{session_name}/ when cleanup is True."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(tmp_path, cleanup=True, session="myproject")
    session_dir = create_session_mailbox(config.mailbox_dir, config.session_name)

    assert session_dir.exists(), "Precondition: session_dir must exist before stop()"

    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    await orch.stop()

    assert not session_dir.exists(), (
        f"stop() should have deleted {session_dir} when mailbox_cleanup_on_stop=True"
    )


@pytest.mark.asyncio
async def test_stop_retains_session_mailbox_when_cleanup_disabled(tmp_path: Path) -> None:
    """stop() leaves {mailbox_dir}/{session_name}/ intact when cleanup is False."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(tmp_path, cleanup=False, session="myproject")
    session_dir = create_session_mailbox(config.mailbox_dir, config.session_name)

    assert session_dir.exists(), "Precondition: session_dir must exist before stop()"

    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    await orch.stop()

    assert session_dir.exists(), (
        f"stop() should have retained {session_dir} when mailbox_cleanup_on_stop=False"
    )
    # Verify the dummy message is still there
    assert (session_dir / "msg-001.json").exists()


@pytest.mark.asyncio
async def test_stop_noop_when_mailbox_dir_does_not_exist(tmp_path: Path) -> None:
    """stop() does not raise when session mailbox directory does not exist."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(tmp_path, cleanup=True, session="ghost")
    # Do NOT create the directory — simulate a fresh run with no prior messages
    session_dir = Path(config.mailbox_dir) / config.session_name
    assert not session_dir.exists()

    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    # Must not raise
    await orch.stop()
    assert not session_dir.exists()


@pytest.mark.asyncio
async def test_stop_only_deletes_session_subdir_not_mailbox_root(tmp_path: Path) -> None:
    """stop() deletes only {session_name}/ subdir, NOT the mailbox_dir root."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(tmp_path, cleanup=True, session="proj-a")
    session_dir = create_session_mailbox(config.mailbox_dir, config.session_name)

    # Create a sibling session directory for a different session (should survive)
    sibling_dir = Path(config.mailbox_dir) / "proj-b"
    sibling_dir.mkdir(parents=True, exist_ok=True)
    (sibling_dir / "msg.json").write_text("{}")

    mailbox_root = Path(config.mailbox_dir)

    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    await orch.stop()

    # Only proj-a/ deleted; mailbox root and proj-b/ survive
    assert not session_dir.exists(), "proj-a session dir should be deleted"
    assert mailbox_root.exists(), "mailbox root should survive"
    assert sibling_dir.exists(), "proj-b session dir should survive"


@pytest.mark.asyncio
async def test_stop_cleanup_non_empty_directory(tmp_path: Path) -> None:
    """stop() recursively deletes a session mailbox with nested sub-directories."""
    bus = Bus()
    tmux = make_tmux_mock()
    config = make_config(tmp_path, cleanup=True, session="nested")
    session_dir = create_session_mailbox(config.mailbox_dir, config.session_name)

    # Create a nested inbox structure: session/agent-1/inbox/
    inbox = session_dir / "agent-1" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "msg.json").write_text('{"id": "x"}')

    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    await orch.stop()

    assert not session_dir.exists(), "Recursive rmtree must delete nested structure"


@pytest.mark.asyncio
async def test_stop_cleanup_different_session_names(tmp_path: Path) -> None:
    """Cleanup uses the configured session_name, not a hardcoded string."""
    for session in ("alpha", "beta", "gamma-01"):
        bus = Bus()
        tmux = make_tmux_mock()
        config = make_config(tmp_path, cleanup=True, session=session)
        session_dir = create_session_mailbox(config.mailbox_dir, session)
        assert session_dir.exists()

        orch = Orchestrator(bus=bus, tmux=tmux, config=config)
        await orch.start()
        await orch.stop()

        assert not session_dir.exists(), (
            f"Session mailbox for '{session}' should be deleted after stop()"
        )
