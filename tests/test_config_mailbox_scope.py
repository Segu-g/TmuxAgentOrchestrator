"""Tests for project-scoped mailbox_dir and related path resolution.

When mailbox_dir (and related path fields) are relative paths, they are
resolved relative to the cwd passed to load_config().  Absolute paths
(including ~ expansions) are unchanged.

Reference: DESIGN.md §10.62 (v1.1.30 — Project-Scoped Mailbox Directory)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tmux_orchestrator.config import load_config, OrchestratorConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(content))
    return cfg


MINIMAL_YAML = """
    session_name: test
    agents: []
"""


# ---------------------------------------------------------------------------
# 1. Default mailbox_dir is project-relative
# ---------------------------------------------------------------------------


def test_default_mailbox_dir_is_relative():
    """OrchestratorConfig default mailbox_dir is a relative string."""
    cfg = OrchestratorConfig(session_name="s", agents=[])
    assert not Path(cfg.mailbox_dir).is_absolute(), (
        f"Expected relative default mailbox_dir, got: {cfg.mailbox_dir!r}"
    )


def test_default_mailbox_dir_value():
    """OrchestratorConfig default mailbox_dir is '.orchestrator/mailbox'."""
    cfg = OrchestratorConfig(session_name="s", agents=[])
    assert cfg.mailbox_dir == ".orchestrator/mailbox"


# ---------------------------------------------------------------------------
# 2. load_config resolves relative mailbox_dir relative to cwd
# ---------------------------------------------------------------------------


def test_load_config_resolves_relative_mailbox_dir(tmp_path):
    """Relative mailbox_dir resolved to cwd / mailbox_dir."""
    cfg_file = write_yaml(tmp_path, MINIMAL_YAML)
    project_dir = tmp_path / "my_project"
    project_dir.mkdir()
    config = load_config(cfg_file, cwd=project_dir)
    expected = str(project_dir / ".orchestrator" / "mailbox")
    assert config.mailbox_dir == expected


def test_load_config_explicit_relative_mailbox_dir(tmp_path):
    """Explicit relative mailbox_dir in YAML resolved to cwd / value."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        mailbox_dir: my_mailbox
    """)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    config = load_config(cfg_file, cwd=project_dir)
    assert config.mailbox_dir == str(project_dir / "my_mailbox")


def test_load_config_absolute_mailbox_dir_unchanged(tmp_path):
    """Absolute mailbox_dir is left as-is (no cwd joining)."""
    abs_dir = str(tmp_path / "abs_mailbox")
    cfg_file = write_yaml(tmp_path, f"""
        session_name: test
        agents: []
        mailbox_dir: {abs_dir}
    """)
    config = load_config(cfg_file, cwd=tmp_path)
    assert config.mailbox_dir == abs_dir


def test_load_config_tilde_mailbox_dir_expanded(tmp_path):
    """~/.tmux_orchestrator mailbox_dir is expanduser'd, not joined with cwd."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        mailbox_dir: ~/.tmux_orchestrator
    """)
    config = load_config(cfg_file, cwd=tmp_path)
    expected = str(Path("~/.tmux_orchestrator").expanduser())
    assert config.mailbox_dir == expected


def test_load_config_no_cwd_uses_cwd_default(tmp_path, monkeypatch):
    """When cwd=None, default .orchestrator/mailbox resolved to Path.cwd()."""
    monkeypatch.chdir(tmp_path)
    cfg_file = write_yaml(tmp_path, MINIMAL_YAML)
    config = load_config(cfg_file)  # cwd=None — uses Path.cwd()
    expected = str(tmp_path / ".orchestrator" / "mailbox")
    assert config.mailbox_dir == expected


# ---------------------------------------------------------------------------
# 3. result_store_dir resolution
# ---------------------------------------------------------------------------


def test_load_config_default_result_store_dir_tilde(tmp_path, monkeypatch):
    """Default result_store_dir (~/.tmux_orchestrator/results) is expanduser'd."""
    monkeypatch.chdir(tmp_path)
    cfg_file = write_yaml(tmp_path, MINIMAL_YAML)
    config = load_config(cfg_file, cwd=tmp_path)
    # default is still the home-relative path for result_store_dir
    expected = str(Path("~/.tmux_orchestrator/results").expanduser())
    assert config.result_store_dir == expected


def test_load_config_relative_result_store_dir(tmp_path):
    """Explicit relative result_store_dir is resolved relative to cwd."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        result_store_dir: results
    """)
    config = load_config(cfg_file, cwd=tmp_path)
    assert config.result_store_dir == str(tmp_path / "results")


def test_load_config_absolute_result_store_dir_unchanged(tmp_path):
    """Absolute result_store_dir is unchanged."""
    abs_dir = str(tmp_path / "my_results")
    cfg_file = write_yaml(tmp_path, f"""
        session_name: test
        agents: []
        result_store_dir: {abs_dir}
    """)
    config = load_config(cfg_file, cwd=tmp_path)
    assert config.result_store_dir == abs_dir


# ---------------------------------------------------------------------------
# 4. checkpoint_db resolution
# ---------------------------------------------------------------------------


def test_load_config_default_checkpoint_db_tilde(tmp_path, monkeypatch):
    """Default checkpoint_db (~/.tmux_orchestrator/checkpoint.db) is expanduser'd."""
    monkeypatch.chdir(tmp_path)
    cfg_file = write_yaml(tmp_path, MINIMAL_YAML)
    config = load_config(cfg_file, cwd=tmp_path)
    expected = str(Path("~/.tmux_orchestrator/checkpoint.db").expanduser())
    assert config.checkpoint_db == expected


def test_load_config_relative_checkpoint_db(tmp_path):
    """Explicit relative checkpoint_db is resolved relative to cwd."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        checkpoint_db: .orchestrator/checkpoint.db
    """)
    config = load_config(cfg_file, cwd=tmp_path)
    assert config.checkpoint_db == str(tmp_path / ".orchestrator" / "checkpoint.db")


def test_load_config_absolute_checkpoint_db_unchanged(tmp_path):
    """Absolute checkpoint_db is unchanged."""
    abs_db = str(tmp_path / "cp.db")
    cfg_file = write_yaml(tmp_path, f"""
        session_name: test
        agents: []
        checkpoint_db: {abs_db}
    """)
    config = load_config(cfg_file, cwd=tmp_path)
    assert config.checkpoint_db == abs_db


# ---------------------------------------------------------------------------
# 5. Multiple relative paths are all resolved to the same cwd
# ---------------------------------------------------------------------------


def test_all_relative_paths_share_cwd(tmp_path):
    """mailbox_dir, result_store_dir, checkpoint_db all resolve relative to same cwd."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        mailbox_dir: .orchestrator/mailbox
        result_store_dir: .orchestrator/results
        checkpoint_db: .orchestrator/checkpoint.db
    """)
    config = load_config(cfg_file, cwd=project_dir)
    assert config.mailbox_dir == str(project_dir / ".orchestrator" / "mailbox")
    assert config.result_store_dir == str(project_dir / ".orchestrator" / "results")
    assert config.checkpoint_db == str(project_dir / ".orchestrator" / "checkpoint.db")


# ---------------------------------------------------------------------------
# 6. cwd as str (not just Path)
# ---------------------------------------------------------------------------


def test_load_config_cwd_as_string(tmp_path):
    """cwd can be provided as a string, not just a Path."""
    cfg_file = write_yaml(tmp_path, MINIMAL_YAML)
    config = load_config(cfg_file, cwd=str(tmp_path))
    expected = str(tmp_path / ".orchestrator" / "mailbox")
    assert config.mailbox_dir == expected
