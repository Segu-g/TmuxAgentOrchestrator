"""Tests for the trust dialog suppression mechanism (trust.py).

These tests verify that :func:`pre_trust_worktree` correctly writes the
``hasTrustDialogAccepted`` entry to ``~/.claude.json`` so that Claude Code
does not show the interactive trust prompt when starting in an agent worktree.

All tests use a temporary file for ``claude_json_path`` to avoid mutating
the real ``~/.claude.json`` on the developer's machine.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from tmux_orchestrator.trust import _atomic_write_json, pre_trust_worktree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------


def test_creates_entry_in_new_file(tmp_path: Path) -> None:
    """pre_trust_worktree creates a new ~/.claude.json if one doesn't exist."""
    claude_json = tmp_path / ".claude.json"
    worktree = tmp_path / "repo" / ".worktrees" / "worker-1"
    worktree.mkdir(parents=True)

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    assert data["projects"][str(worktree)]["hasTrustDialogAccepted"] is True


def test_adds_entry_to_existing_file(tmp_path: Path) -> None:
    """pre_trust_worktree adds an entry without destroying existing content."""
    claude_json = tmp_path / ".claude.json"
    existing = {
        "theme": "dark",
        "projects": {
            "/some/other/path": {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["Bash"],
            }
        },
    }
    claude_json.write_text(json.dumps(existing), encoding="utf-8")

    worktree = tmp_path / "my-worktree"
    worktree.mkdir()
    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    # Existing content preserved
    assert data["theme"] == "dark"
    assert data["projects"]["/some/other/path"]["allowedTools"] == ["Bash"]
    # New entry added
    assert data["projects"][str(worktree)]["hasTrustDialogAccepted"] is True


def test_idempotent_when_already_trusted(tmp_path: Path) -> None:
    """Calling pre_trust_worktree twice does not change the entry."""
    claude_json = tmp_path / ".claude.json"
    worktree = tmp_path / "wt"
    worktree.mkdir()

    pre_trust_worktree(worktree, claude_json_path=claude_json)
    data_first = _load(claude_json)

    pre_trust_worktree(worktree, claude_json_path=claude_json)
    data_second = _load(claude_json)

    assert data_first == data_second
    assert data_second["projects"][str(worktree)]["hasTrustDialogAccepted"] is True


def test_does_not_overwrite_existing_trusted_entry(tmp_path: Path) -> None:
    """An existing hasTrustDialogAccepted=true entry is not touched."""
    claude_json = tmp_path / ".claude.json"
    worktree = tmp_path / "wt"
    worktree.mkdir()

    initial = {
        "projects": {
            str(worktree): {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["Read", "Write"],
                "projectOnboardingSeenCount": 5,
            }
        }
    }
    claude_json.write_text(json.dumps(initial), encoding="utf-8")

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    entry = data["projects"][str(worktree)]
    # hasTrustDialogAccepted was already True; no other fields should be changed
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["allowedTools"] == ["Read", "Write"]
    assert entry["projectOnboardingSeenCount"] == 5


def test_sets_external_includes_approved(tmp_path: Path) -> None:
    """pre_trust_worktree also sets hasClaudeMdExternalIncludesApproved."""
    claude_json = tmp_path / ".claude.json"
    worktree = tmp_path / "wt"
    worktree.mkdir()

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    entry = data["projects"][str(worktree)]
    assert entry.get("hasClaudeMdExternalIncludesApproved") is True


def test_uses_resolved_absolute_path(tmp_path: Path) -> None:
    """The projects key is the resolved absolute path of cwd."""
    claude_json = tmp_path / ".claude.json"
    worktree = tmp_path / "wt"
    worktree.mkdir()

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    assert str(worktree.resolve()) in data["projects"]


def test_handles_corrupt_json_gracefully(tmp_path: Path) -> None:
    """Corrupt ~/.claude.json is treated as empty; a valid file is written."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("this is NOT json {{{", encoding="utf-8")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    # Should not raise
    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    assert data["projects"][str(worktree)]["hasTrustDialogAccepted"] is True


def test_handles_root_not_dict(tmp_path: Path) -> None:
    """If ~/.claude.json root is not a dict (e.g. a JSON array), reset to {}."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("[1, 2, 3]", encoding="utf-8")

    worktree = tmp_path / "wt"
    worktree.mkdir()

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    assert isinstance(data, dict)
    assert data["projects"][str(worktree)]["hasTrustDialogAccepted"] is True


def test_creates_parent_dirs_if_missing(tmp_path: Path) -> None:
    """pre_trust_worktree creates parent dirs of claude_json_path if needed."""
    claude_json = tmp_path / "nested" / "dir" / ".claude.json"
    worktree = tmp_path / "wt"
    worktree.mkdir()

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    assert claude_json.exists()
    data = _load(claude_json)
    assert data["projects"][str(worktree)]["hasTrustDialogAccepted"] is True


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_atomic_write_does_not_leave_temp_file(tmp_path: Path) -> None:
    """_atomic_write_json must not leave temp files behind on success."""
    target = tmp_path / ".claude.json"
    _atomic_write_json(target, {"test": 1})

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0] == target


def test_atomic_write_produces_valid_json(tmp_path: Path) -> None:
    """_atomic_write_json writes valid UTF-8 JSON."""
    target = tmp_path / ".claude.json"
    data = {"projects": {"/path": {"hasTrustDialogAccepted": True}}}
    _atomic_write_json(target, data)

    result = _load(target)
    assert result == data


def test_concurrent_calls_do_not_corrupt_file(tmp_path: Path) -> None:
    """Multiple concurrent pre_trust_worktree calls must not corrupt the file.

    With the flock-based serialisation, ALL 10 entries must be present after
    all threads finish — no entries should be lost to a write race.
    """
    claude_json = tmp_path / ".claude.json"
    lock_file = tmp_path / ".claude.json.lock"
    errors: list[Exception] = []

    def trust_one(idx: int) -> None:
        worktree = tmp_path / f"wt-{idx}"
        worktree.mkdir(exist_ok=True)
        try:
            pre_trust_worktree(worktree, claude_json_path=claude_json, lock_path=lock_file)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=trust_one, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent calls raised: {errors}"
    # File must be valid JSON after all threads finish
    data = _load(claude_json)
    assert isinstance(data, dict)
    assert "projects" in data
    # All 10 entries must be present — flock prevents write-race data loss
    assert len(data["projects"]) == 10, (
        f"Expected 10 entries but got {len(data['projects'])}: {list(data['projects'].keys())}"
    )
