"""Git worktree integrity checker for TmuxAgentOrchestrator.

Validates that agent worktrees remain consistent and uncorrupted throughout
the orchestrator's lifecycle.  Used both proactively (before task dispatch)
and reactively (after agent stop).

Integrity checks performed:
1. **Path existence** — the worktree directory must exist on disk.
2. **Index lock** — ``.git/index.lock`` must not be present (indicates a
   crashed git process left a stale lock).
3. **HEAD resolution** — ``git rev-parse HEAD`` must succeed; detached HEAD
   and unresolvable references are flagged.
4. **Branch name** — the checked-out branch must match the expected
   ``worktree/{agent_id}`` pattern (configurable; just reported, not failed).
5. **Dirty state** — ``git status --porcelain`` reports uncommitted changes.

``git fsck`` is intentionally run only in ``check_path`` and omitted from the
fast pre-dispatch path because fsck is O(objects) and adds latency.  The
dispatch hook uses the lighter checks (1–4) to remain low-latency.

Design references:
- git-fsck(1): https://git-scm.com/docs/git-fsck
- git-worktree(1): https://git-scm.com/docs/git-worktree
- GitLab "Repository checks":
  https://docs.gitlab.com/ee/administration/repository_checks.html
- GitLab "Repository consistency checks":
  https://docs.gitlab.com/administration/gitaly/consistency_checks/
- DESIGN.md §10.17 (v0.43.0)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tmux_orchestrator.bus import Message, MessageType

if TYPE_CHECKING:
    from tmux_orchestrator.bus import Bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WorktreeStatus:
    """Integrity report for a single agent's git worktree.

    All fields are safe to serialise as JSON via ``to_dict()``.
    """

    agent_id: str
    path: str | None  # None when agent uses isolate=False (shared repo)

    # Core integrity flags
    is_valid: bool = False      # True iff the worktree is structurally sound
    is_dirty: bool = False      # True iff uncommitted changes are present
    is_locked: bool = False     # True iff .git/index.lock exists

    # Git state
    head_sha: str | None = None   # 40-char SHA of HEAD, or None
    branch: str | None = None     # current branch name, or None

    # Diagnostics
    errors: list[str] = field(default_factory=list)  # fsck/repair error messages

    # Timestamp (ISO 8601)
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict of all fields."""
        return {
            "agent_id": self.agent_id,
            "path": self.path,
            "is_valid": self.is_valid,
            "is_dirty": self.is_dirty,
            "is_locked": self.is_locked,
            "head_sha": self.head_sha,
            "branch": self.branch,
            "errors": self.errors,
            "checked_at": self.checked_at,
        }


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class WorktreeIntegrityChecker:
    """Validates the git state of agent worktrees.

    Parameters
    ----------
    repo_root:
        The root of the main git repository (used as cwd for git commands).
    bus:
        Optional message bus.  When provided, ``check_and_publish_dirty``
        and ``check_and_publish_integrity`` publish bus events.
    """

    def __init__(
        self,
        repo_root: Path | str,
        bus: "Bus | None" = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._bus = bus

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_agent(
        self,
        agent_id: str,
        worktree_path: Path | None,
    ) -> WorktreeStatus | None:
        """Check integrity for *agent_id*.

        Returns ``None`` when *worktree_path* is ``None`` (the agent uses
        ``isolate=False`` and shares the main repo; no dedicated worktree to
        validate).

        Otherwise delegates to ``check_path`` and returns a ``WorktreeStatus``.
        """
        if worktree_path is None:
            return None
        return await self.check_path(agent_id, worktree_path)

    async def check_path(
        self,
        agent_id: str,
        path: Path,
    ) -> WorktreeStatus:
        """Perform all integrity checks on *path* for *agent_id*.

        Checks performed (all non-blocking, run via asyncio executor):
        1. Path existence
        2. index.lock presence
        3. HEAD resolution (``git rev-parse HEAD``)
        4. Branch name (``git rev-parse --abbrev-ref HEAD``)
        5. Dirty state (``git status --porcelain``)
        6. Fsck (``git fsck --no-dangling --no-progress``) — structural check

        Parameters
        ----------
        agent_id:
            ID of the agent (used to label the status record).
        path:
            Filesystem path to the worktree directory.

        Returns
        -------
        WorktreeStatus
            Populated with results of all checks.
        """
        path = Path(path)
        status = WorktreeStatus(agent_id=agent_id, path=str(path))

        # 1. Path existence
        if not path.exists():
            status.errors.append(f"Worktree directory does not exist: {path}")
            return status  # is_valid stays False; skip further checks

        # 2. Index lock
        # In a linked worktree, .git is a FILE containing "gitdir: <path>".
        # The actual git metadata directory is that resolved path.
        # In the main worktree, .git is a directory.
        # We resolve the actual git dir to find index.lock.
        git_dir_path = path / ".git"
        actual_git_dir: Path | None = None
        if git_dir_path.is_file():
            # Linked worktree: parse the gitdir pointer
            try:
                gitdir_content = git_dir_path.read_text().strip()
                if gitdir_content.startswith("gitdir: "):
                    resolved = gitdir_content[len("gitdir: "):]
                    actual_git_dir = Path(resolved)
                    if not actual_git_dir.is_absolute():
                        actual_git_dir = (path / actual_git_dir).resolve()
            except OSError:
                pass
        elif git_dir_path.is_dir():
            actual_git_dir = git_dir_path

        if actual_git_dir is not None:
            index_lock = actual_git_dir / "index.lock"
            if index_lock.exists():
                status.is_locked = True
                status.errors.append(
                    f"Stale index.lock found at {index_lock}. "
                    "A previous git operation may have crashed."
                )

        # 3. HEAD resolution
        head_sha, head_error = await _run_git(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
        )
        if head_error or not head_sha.strip():
            status.errors.append(
                f"Cannot resolve HEAD: {head_error or 'empty output'}"
            )
        else:
            status.head_sha = head_sha.strip()

        # 4. Branch name
        branch_out, branch_err = await _run_git(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
        )
        if not branch_err and branch_out.strip():
            status.branch = branch_out.strip()

        # 5. Dirty state
        porcelain_out, porcelain_err = await _run_git(
            ["git", "status", "--porcelain"],
            cwd=path,
        )
        if not porcelain_err and porcelain_out.strip():
            status.is_dirty = True

        # 6. Fsck (structural object-store check)
        fsck_out, fsck_err = await _run_git(
            ["git", "fsck", "--no-dangling", "--no-progress"],
            cwd=path,
        )
        if fsck_err and fsck_err.strip():
            # git fsck writes diagnostics to stderr; filter for error: lines
            error_lines = [
                line for line in fsck_err.splitlines()
                if line.startswith("error:") or line.startswith("fatal:")
            ]
            if error_lines:
                status.errors.extend(error_lines)

        # Determine overall validity:
        # Valid iff path exists AND HEAD resolves AND no fsck fatal errors.
        fatal_errors = [e for e in status.errors if "index.lock" not in e]
        status.is_valid = (
            path.exists()
            and status.head_sha is not None
            and len(fatal_errors) == 0
        )

        logger.debug(
            "WorktreeIntegrityChecker: agent=%s path=%s valid=%s dirty=%s locked=%s",
            agent_id, path, status.is_valid, status.is_dirty, status.is_locked,
        )
        return status

    async def check_all(
        self,
        agent_paths: dict[str, Path | None],
    ) -> list[WorktreeStatus]:
        """Check all agents in *agent_paths* concurrently.

        Parameters
        ----------
        agent_paths:
            Mapping from agent_id to worktree path (or None for shared agents).

        Returns
        -------
        list[WorktreeStatus]
            Status for each agent that has a non-None worktree path.
            Shared agents (None path) are excluded.
        """
        tasks = [
            self.check_path(agent_id, path)
            for agent_id, path in agent_paths.items()
            if path is not None
        ]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(results)

    async def check_and_publish_dirty(
        self,
        agent_id: str,
        path: Path,
    ) -> WorktreeStatus:
        """Check integrity and publish ``dirty_worktree`` bus event if dirty.

        Called after an agent stops to notify the orchestrator of any
        uncommitted work left in the worktree.

        Parameters
        ----------
        agent_id:
            Agent whose worktree to check.
        path:
            Filesystem path to the worktree.

        Returns
        -------
        WorktreeStatus
            Full status result.
        """
        status = await self.check_path(agent_id, path)
        if status.is_dirty and self._bus is not None:
            await self._bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__worktree_integrity__",
                payload={
                    "event": "dirty_worktree",
                    "agent_id": agent_id,
                    "path": str(path),
                    "head_sha": status.head_sha,
                    "branch": status.branch,
                },
            ))
            logger.warning(
                "WorktreeIntegrityChecker: dirty_worktree for agent=%s path=%s",
                agent_id, path,
            )
        return status

    async def check_and_publish_integrity(
        self,
        agent_id: str,
        path: Path,
    ) -> WorktreeStatus:
        """Check integrity and publish ``integrity_check_failed`` bus event if invalid.

        Used as a pre-dispatch hook to prevent tasks from being sent to
        agents with broken worktrees.

        Parameters
        ----------
        agent_id:
            Agent whose worktree to check.
        path:
            Filesystem path to the worktree.

        Returns
        -------
        WorktreeStatus
            Full status result.  Callers should check ``status.is_valid``
            before dispatching a task.
        """
        status = await self.check_path(agent_id, path)
        if not status.is_valid and self._bus is not None:
            await self._bus.publish(Message(
                type=MessageType.STATUS,
                from_id="__worktree_integrity__",
                payload={
                    "event": "integrity_check_failed",
                    "agent_id": agent_id,
                    "path": str(path),
                    "errors": status.errors,
                },
            ))
            logger.error(
                "WorktreeIntegrityChecker: integrity_check_failed for agent=%s errors=%s",
                agent_id, status.errors,
            )
        return status


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _run_git(
    cmd: list[str],
    *,
    cwd: Path,
) -> tuple[str, str]:
    """Run a git command asynchronously; return (stdout, stderr).

    Never raises — returns (stdout, stderr) where stderr is non-empty on failure.
    Stderr is used to detect git errors without raising exceptions, so that the
    integrity checker can aggregate all diagnostics rather than short-circuiting
    on the first failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        if proc.returncode != 0 and not stderr.strip():
            # Non-zero exit with no stderr: surface returncode as error message
            stderr = f"git exited with code {proc.returncode}"
        return stdout, stderr
    except Exception as exc:  # noqa: BLE001
        return "", str(exc)
