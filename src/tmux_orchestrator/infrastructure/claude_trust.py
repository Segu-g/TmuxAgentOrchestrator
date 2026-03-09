"""Claude Code workspace trust pre-population.

Infrastructure adapter for the Claude Code filesystem trust configuration.
This module is the canonical home for ``pre_trust_worktree``; the old path
``tmux_orchestrator.trust`` re-exports from here (Strangler Fig shim).

Layer: infrastructure (filesystem adapter — writes to ~/.claude.json).
domain/ and application/ MUST NOT import from this module.

When Claude Code starts in an unknown directory it shows an interactive
"Do you trust the files in this folder?" prompt before running any hooks.
This blocks :func:`ClaudeCodeAgent._wait_for_ready`, which waits for the
``SessionStart`` hook to fire — causing a 60-second timeout.

:func:`pre_trust_worktree` solves this by writing the trust entry into
``~/.claude.json`` before launching the ``claude`` process.

Storage format (verified against Claude Code v2.x on Linux, 2026-03):

.. code-block:: json

    {
      "projects": {
        "/absolute/path/to/dir": {
          "hasTrustDialogAccepted": true,
          "hasClaudeMdExternalIncludesApproved": true
        }
      }
    }

Claude Code walks parent directories when checking trust, so a single
home-directory entry cascades to all subdirectories.  We write the exact
worktree path for precision so as not to over-trust unrelated directories.

References
----------
- GitHub Issue #23109 "Trusted workspace patterns for git worktrees"
  https://github.com/anthropics/claude-code/issues/23109
- GitHub Issue #2147 "Claude forgets trust dialog acceptance"
  https://github.com/anthropics/claude-code/issues/2147
- GitHub Issue #9113 "Workspace Trust Dialog Not Respecting ~/.claude.json"
  https://github.com/anthropics/claude-code/issues/9113
- DESIGN.md §10.N (v1.0.16 — infrastructure/ layer extraction)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level advisory lock file for cross-process serialisation of the
# read-modify-write cycle on ~/.claude.json.  Multiple agents starting in
# parallel each call pre_trust_worktree(); without a lock the last writer
# wins and clobbers the earlier entries.
_TRUST_LOCK_PATH = Path.home() / ".claude.json.lock"

# Default location of the Claude Code global config file.
_DEFAULT_CLAUDE_JSON = Path.home() / ".claude.json"


def pre_trust_worktree(
    cwd: Path,
    *,
    claude_json_path: Path | None = None,
    lock_path: Path | None = None,
) -> None:
    """Ensure *cwd* has a trust entry in ``~/.claude.json``.

    This is **idempotent** — calling it multiple times for the same path is
    safe; an existing ``hasTrustDialogAccepted: true`` entry is preserved.

    The entire read-modify-write cycle is **serialised** using a POSIX
    advisory lock (``~/.claude.json.lock``).  This prevents the race condition
    where multiple agents starting in parallel each read the same stale
    ``~/.claude.json`` and then the last writer clobbers the earlier trust
    entries.

    The write itself is **atomic**: the JSON is serialised to a temporary file
    in the same directory as ``~/.claude.json`` and then renamed into place
    using :func:`os.replace`.

    Parameters
    ----------
    cwd:
        Absolute path to the directory that should be pre-trusted.
    claude_json_path:
        Override the global config file location.  Defaults to
        ``~/.claude.json``.  Useful in tests.
    lock_path:
        Override the lock file path.  Defaults to ``~/.claude.json.lock``.
        Useful in tests.
    """
    target = claude_json_path or _DEFAULT_CLAUDE_JSON
    lpath = lock_path or _TRUST_LOCK_PATH
    path_key = str(cwd.resolve())

    # Serialise the read-modify-write cycle across processes with a POSIX
    # advisory lock so that parallel agent startups do not clobber each other.
    lpath.parent.mkdir(parents=True, exist_ok=True)
    with open(lpath, "a") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            logger.warning("trust: could not acquire lock %s (%s) — proceeding without lock", lpath, exc)

        # Load existing config (or start with an empty dict).
        data: dict = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "trust: could not parse %s (%s) — treating as empty", target, exc
                )
                data = {}

        if not isinstance(data, dict):
            logger.warning(
                "trust: %s root is not a JSON object — resetting to {}", target
            )
            data = {}

        projects: dict = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            logger.warning("trust: 'projects' key in %s is not a dict — resetting", target)
            projects = {}
            data["projects"] = projects

        entry: dict = projects.setdefault(path_key, {})
        if not isinstance(entry, dict):
            logger.warning(
                "trust: projects[%r] in %s is not a dict — resetting", path_key, target
            )
            entry = {}
            projects[path_key] = entry

        # Only touch the fields we care about; leave everything else intact.
        already_trusted = entry.get("hasTrustDialogAccepted") is True
        if already_trusted:
            logger.debug("trust: %s is already trusted in %s — no-op", path_key, target)
            return

        entry["hasTrustDialogAccepted"] = True
        # Also approve CLAUDE.md external includes to avoid a second blocking prompt.
        entry.setdefault("hasClaudeMdExternalIncludesApproved", True)

        _atomic_write_json(target, data)
        logger.info("trust: pre-trusted %s in %s", path_key, target)
        # Lock is released when the `with` block exits.


def _atomic_write_json(target: Path, data: dict) -> None:
    """Serialise *data* to *target* atomically using a temp-file + rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    # Write to a temp file in the same directory so rename() is atomic
    # (same filesystem guaranteed).
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".claude_json_", suffix=".tmp"
    )
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, str(target))
