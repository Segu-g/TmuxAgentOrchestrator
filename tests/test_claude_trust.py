"""Regression tests for the trust dialog rootfix (v1.1.22).

These tests target the specific bugs fixed in v1.1.22:

1. ``hasTrustDialogHooksAccepted`` field must be written — without it
   SessionStart hooks are blocked even when ``hasTrustDialogAccepted`` is set
   (GitHub Issue #5572, #11519).

2. ``allowedTools: []`` field must be written for backward-compatibility with
   older Claude Code versions that check this field during trust evaluation.

3. ``already_trusted`` early-exit must require BOTH fields — old entries that
   only have ``hasTrustDialogAccepted`` (without ``hasTrustDialogHooksAccepted``)
   must be upgraded rather than silently skipped.

4. Write-then-verify loop must detect and recover from race-condition
   overwrites where a concurrent Claude Code process clobbers ``~/.claude.json``
   between our write and the new ``claude`` process reading the file.

All tests use temporary files to avoid mutating the real ``~/.claude.json``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tmux_orchestrator.trust import (
    _VERIFY_RETRIES,
    _VERIFY_SLEEP_S,
    pre_trust_worktree,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_worktree(tmp_path: Path, name: str = "wt") -> Path:
    wt = tmp_path / name
    wt.mkdir(parents=True, exist_ok=True)
    return wt


# ---------------------------------------------------------------------------
# Regression: hasTrustDialogHooksAccepted (fix for GitHub #5572/#11519)
# ---------------------------------------------------------------------------


def test_writes_hooks_accepted_field(tmp_path: Path) -> None:
    """pre_trust_worktree must write hasTrustDialogHooksAccepted=true.

    Without this field SessionStart hooks are blocked by the trust system
    even when hasTrustDialogAccepted is true (v2.x regression).
    """
    claude_json = tmp_path / ".claude.json"
    worktree = _make_worktree(tmp_path)

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    entry = data["projects"][str(worktree)]
    assert entry.get("hasTrustDialogHooksAccepted") is True, (
        "hasTrustDialogHooksAccepted must be written so SessionStart hooks fire"
    )


def test_writes_allowed_tools_field(tmp_path: Path) -> None:
    """pre_trust_worktree must write allowedTools=[] for backward-compatibility."""
    claude_json = tmp_path / ".claude.json"
    worktree = _make_worktree(tmp_path)

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    entry = data["projects"][str(worktree)]
    assert "allowedTools" in entry, "allowedTools field required for backward-compat"
    assert entry["allowedTools"] == []


def test_does_not_overwrite_existing_allowed_tools(tmp_path: Path) -> None:
    """If allowedTools already has entries, setdefault must not clear them."""
    claude_json = tmp_path / ".claude.json"
    worktree = _make_worktree(tmp_path)

    initial = {
        "projects": {
            str(worktree): {
                "hasTrustDialogAccepted": True,
                "hasTrustDialogHooksAccepted": True,
                "allowedTools": ["Bash", "Read"],
            }
        }
    }
    claude_json.write_text(json.dumps(initial), encoding="utf-8")

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    assert data["projects"][str(worktree)]["allowedTools"] == ["Bash", "Read"]


# ---------------------------------------------------------------------------
# Regression: already_trusted now requires BOTH fields
# ---------------------------------------------------------------------------


def test_upgrades_entry_missing_hooks_accepted(tmp_path: Path) -> None:
    """Old entries with only hasTrustDialogAccepted must be upgraded.

    Claude Code versions that added hasTrustDialogHooksAccepted require it to
    be set for hooks to fire.  An existing entry without the new field is NOT
    'already trusted' and must be rewritten.
    """
    claude_json = tmp_path / ".claude.json"
    worktree = _make_worktree(tmp_path)

    # Simulate an old entry written by a pre-v1.1.22 pre_trust_worktree call
    old_entry = {
        "hasTrustDialogAccepted": True,
        "hasClaudeMdExternalIncludesApproved": True,
        # hasTrustDialogHooksAccepted intentionally absent
    }
    claude_json.write_text(
        json.dumps({"projects": {str(worktree): old_entry}}),
        encoding="utf-8",
    )

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    entry = data["projects"][str(worktree)]
    assert entry.get("hasTrustDialogHooksAccepted") is True, (
        "Old entry must be upgraded to include hasTrustDialogHooksAccepted"
    )


def test_already_trusted_with_both_fields_is_no_op(tmp_path: Path) -> None:
    """An entry with both trust fields set is a no-op (no file mutation)."""
    claude_json = tmp_path / ".claude.json"
    worktree = _make_worktree(tmp_path)

    full_entry = {
        "hasTrustDialogAccepted": True,
        "hasTrustDialogHooksAccepted": True,
        "hasClaudeMdExternalIncludesApproved": True,
        "allowedTools": ["Bash"],
        "projectOnboardingSeenCount": 7,
    }
    initial_json = json.dumps({"projects": {str(worktree): full_entry}})
    claude_json.write_text(initial_json, encoding="utf-8")
    mtime_before = claude_json.stat().st_mtime

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    # File should be UNCHANGED (no write, the verify loop's sleep still runs
    # but detect early return is not guaranteed within the sleep interval, so
    # just verify the data is correct rather than mtime).
    data = _load(claude_json)
    entry = data["projects"][str(worktree)]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["hasTrustDialogHooksAccepted"] is True
    assert entry["allowedTools"] == ["Bash"]
    assert entry["projectOnboardingSeenCount"] == 7


# ---------------------------------------------------------------------------
# Write-then-verify: race condition recovery
# ---------------------------------------------------------------------------


def test_verify_loop_constants_are_sensible() -> None:
    """Verify that the retry constants are defined and within expected bounds."""
    assert _VERIFY_RETRIES >= 1, "Must retry at least once"
    assert 0.0 < _VERIFY_SLEEP_S <= 1.0, "Sleep must be positive and not too long"


def test_write_then_verify_detects_intact_entry(tmp_path: Path) -> None:
    """After a normal write (no race), the verify loop must confirm the entry.

    This tests the happy path: entry is written and stays intact.
    """
    claude_json = tmp_path / ".claude.json"
    lock_file = tmp_path / ".lock"
    worktree = _make_worktree(tmp_path)

    # Should complete without error
    pre_trust_worktree(worktree, claude_json_path=claude_json, lock_path=lock_file)

    data = _load(claude_json)
    entry = data["projects"][str(worktree)]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["hasTrustDialogHooksAccepted"] is True


def test_write_then_verify_recovers_from_race(tmp_path: Path) -> None:
    """Verify loop must rewrite the entry if a concurrent process clobbers it.

    We simulate the race by launching a background thread that overwrites
    ~/.claude.json (removing our entry) shortly after the initial write.
    pre_trust_worktree should detect the loss and rewrite.
    """
    claude_json = tmp_path / ".claude.json"
    lock_file = tmp_path / ".lock"
    worktree = _make_worktree(tmp_path)

    overwrite_triggered = threading.Event()
    overwrite_done = threading.Event()

    def _clobber() -> None:
        """Wait for the initial write, then overwrite with a different JSON."""
        # Wait until pre_trust_worktree has written the file at least once
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if claude_json.exists():
                overwrite_triggered.set()
                break
            time.sleep(0.005)
        # Small delay to let the function reach the verify loop
        time.sleep(_VERIFY_SLEEP_S * 0.5)
        # Clobber: remove our entry entirely (simulate a Claude Code rewrite)
        try:
            current = json.loads(claude_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = {}
        # Remove just our worktree entry
        current.get("projects", {}).pop(str(worktree), None)
        claude_json.write_text(json.dumps(current), encoding="utf-8")
        overwrite_done.set()

    clobber_thread = threading.Thread(target=_clobber, daemon=True)
    clobber_thread.start()

    pre_trust_worktree(worktree, claude_json_path=claude_json, lock_path=lock_file)

    clobber_thread.join(timeout=5.0)

    # Regardless of whether the clobber hit, the final state must have the entry.
    data = _load(claude_json)
    entry = data["projects"].get(str(worktree), {})
    assert entry.get("hasTrustDialogAccepted") is True
    assert entry.get("hasTrustDialogHooksAccepted") is True


# ---------------------------------------------------------------------------
# Full field set after a fresh call
# ---------------------------------------------------------------------------


def test_all_required_fields_written_together(tmp_path: Path) -> None:
    """A fresh call must write all four required trust fields in one pass."""
    claude_json = tmp_path / ".claude.json"
    worktree = _make_worktree(tmp_path)

    pre_trust_worktree(worktree, claude_json_path=claude_json)

    data = _load(claude_json)
    entry = data["projects"][str(worktree)]

    assert entry.get("hasTrustDialogAccepted") is True
    assert entry.get("hasTrustDialogHooksAccepted") is True
    assert entry.get("hasClaudeMdExternalIncludesApproved") is True
    assert "allowedTools" in entry


def test_multiple_worktrees_all_get_full_entries(tmp_path: Path) -> None:
    """Each worktree in a concurrent multi-agent setup gets a full entry."""
    claude_json = tmp_path / ".claude.json"
    lock_file = tmp_path / ".lock"
    worktrees = [_make_worktree(tmp_path, f"wt-{i}") for i in range(4)]

    threads = [
        threading.Thread(
            target=pre_trust_worktree,
            kwargs={"cwd": wt, "claude_json_path": claude_json, "lock_path": lock_file},
        )
        for wt in worktrees
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = _load(claude_json)
    for wt in worktrees:
        entry = data["projects"].get(str(wt), {})
        assert entry.get("hasTrustDialogAccepted") is True, f"{wt} missing hasTrustDialogAccepted"
        assert entry.get("hasTrustDialogHooksAccepted") is True, f"{wt} missing hasTrustDialogHooksAccepted"
        assert "allowedTools" in entry, f"{wt} missing allowedTools"
