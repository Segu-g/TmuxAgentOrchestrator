"""Git worktree manager: creates and removes per-agent isolated working trees.

Infrastructure adapter for the git external system (via subprocess).
This module is the canonical home for WorktreeManager; the old path
``tmux_orchestrator.worktree`` re-exports from here (Strangler Fig shim).

Layer: infrastructure (may depend on domain/application; must NOT be imported
by domain/ or application/).

References:
    - Cockburn, Alistair. "Hexagonal Architecture Explained" (2024)
      Output adapter: wraps an external system (git) behind a stable interface.
    - Percival & Gregory, "Architecture Patterns with Python" (O'Reilly, 2020)
      Repository pattern: filesystem/subprocess I/O belongs in the infrastructure layer.
    - DESIGN.md §10.N (v1.0.17 — infrastructure/ layer continued extraction)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class WorktreeManager:
    """Manages git worktrees for agent isolation.

    Each isolated agent gets a dedicated worktree under ``{repo_root}/.worktrees/{agent_id}/``
    on a branch named ``worktree/{agent_id}``.  Agents with ``isolate=False`` share the main
    repo root and are tracked only for cleanup purposes.
    """

    def __init__(self, repo_root: Path | str) -> None:
        root = Path(repo_root).resolve()
        found = self.find_repo_root(root)
        if found is None:
            raise RuntimeError(f"Not inside a git repository: {root}")
        self._repo_root = found
        self._worktrees_dir = self._repo_root / ".worktrees"
        self._owned: dict[str, Path] = {}   # agent_id -> worktree path
        self._shared: set[str] = set()      # agent_ids using isolate=False
        self._lock = threading.Lock()
        self._ensure_gitignore()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def find_repo_root(start: Path) -> Path | None:
        """Walk up from *start* to find the nearest directory containing ``.git``."""
        current = start.resolve()
        while True:
            if (current / ".git").exists():
                return current
            parent = current.parent
            if parent == current:
                return None
            current = parent

    def setup(self, agent_id: str, *, isolate: bool = True) -> Path:
        """Create (or reuse) a worktree for *agent_id*.

        Returns the working directory path the agent should use:
        - ``isolate=True``  → ``{repo_root}/.worktrees/{agent_id}/`` (new branch)
        - ``isolate=False`` → ``{repo_root}`` (shared, no git operations)
        """
        with self._lock:
            if not isolate:
                self._shared.add(agent_id)
                return self._repo_root

            path = self._worktrees_dir / agent_id
            branch = f"worktree/{agent_id}"

            # Clean up any leftover worktree/branch from a previous run.
            if path.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=self._repo_root,
                    capture_output=True,
                )
                shutil.rmtree(path, ignore_errors=True)
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=self._repo_root,
                capture_output=True,
            )

            subprocess.run(
                ["git", "worktree", "add", str(path), "-b", branch],
                cwd=self._repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            self._owned[agent_id] = path
            return path

    def teardown(
        self,
        agent_id: str,
        *,
        merge_to_base: bool = False,
        merge_target: str | None = None,
    ) -> None:
        """Remove the worktree and branch for *agent_id*.

        No-op (beyond deregistration) for shared-worktree agents.

        Parameters
        ----------
        agent_id:
            The agent whose worktree should be torn down.
        merge_to_base:
            When ``True``, attempt a ``git merge --squash`` of the agent's
            branch into *merge_target* (or HEAD when *merge_target* is
            ``None``) before removing the worktree.  If the merge fails
            (conflicts, no new commits, etc.) the error is logged and
            teardown continues.

            Useful when the agent was asked to produce code that should land
            on a specific branch automatically after the task completes.
        merge_target:
            Name of the branch to merge into when *merge_to_base* is
            ``True``.  Defaults to ``None``, which keeps the main repo on
            whatever branch it is currently checked out to.

            Example::

                wm.teardown("agent-1", merge_to_base=True, merge_target="develop")

            The main repo will temporarily switch to *merge_target*, merge
            the squash commit, then switch back to the original branch.
        """
        with self._lock:
            if agent_id in self._shared:
                self._shared.discard(agent_id)
                return

            path = self._owned.pop(agent_id, None)
            if path is None:
                return

            branch = f"worktree/{agent_id}"

            if merge_to_base:
                self._merge_branch(branch, target=merge_target)

            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=self._repo_root,
                capture_output=True,
            )
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=self._repo_root,
                capture_output=True,
            )

    def keep_branch(self, agent_id: str) -> None:
        """Remove the worktree for *agent_id* but keep the git branch.

        This allows the caller to inspect or merge the branch manually after
        the agent stops without the commits being lost at teardown.

        The branch ``worktree/{agent_id}`` remains in the local repository;
        the caller is responsible for deleting it when no longer needed::

            git branch -D worktree/{agent_id}

        No-op for shared-worktree agents.
        """
        with self._lock:
            if agent_id in self._shared:
                self._shared.discard(agent_id)
                return

            path = self._owned.pop(agent_id, None)
            if path is None:
                return

            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=self._repo_root,
                capture_output=True,
            )
            # Branch is intentionally NOT deleted here.

    def worktree_path(self, agent_id: str) -> Path | None:
        """Return the current worktree path for *agent_id*, or ``None`` if not set up."""
        with self._lock:
            return self._owned.get(agent_id)

    def prune_stale(self) -> None:
        """Remove stale git worktree administrative files.

        Runs ``git worktree prune --expire now`` in the repository root to
        clean up metadata for worktrees whose directories no longer exist on
        disk (e.g. after an unclean shutdown / demo crash).

        This is called automatically by ``Orchestrator.start()`` before any
        agents are spawned, ensuring that stale entries from a previous run
        cannot cause ``git worktree add`` to fail with a name collision.

        Never raises — errors from the git subprocess are silently ignored so
        that a missing or misconfigured git installation does not prevent the
        orchestrator from starting.

        References:
        - git-scm.com/docs/git-worktree — ``git worktree prune --expire now``
        - anthropics/claude-code#26725 — stale worktrees never cleaned up
        - DESIGN.md §10.40 (v1.1.4)
        """
        result = subprocess.run(
            ["git", "worktree", "prune", "--expire", "now"],
            cwd=self._repo_root,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            logger.debug(
                "worktree prune returned non-zero exit code %d (stderr: %s); "
                "continuing startup — stale cleanup is best-effort",
                result.returncode,
                (stderr or "").strip(),
            )
        else:
            logger.debug("worktree prune completed successfully")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _merge_branch(self, branch: str, *, target: str | None = None) -> None:
        """Squash-merge *branch* into *target* (or the current HEAD).

        When *target* is given the main repo temporarily switches to that
        branch, performs the squash merge, then switches back to the branch
        that was checked out before.  If the checkout fails (dirty tree,
        branch does not exist, etc.) the error is logged and the merge is
        skipped.

        Uses ``--squash`` so the merge produces a single commit rather than
        replaying each agent commit, keeping the target branch history clean.
        On failure the error is logged but execution continues.
        """
        import logging as _logging  # noqa: PLC0415
        _log = _logging.getLogger(__name__)

        # Determine the currently checked-out branch so we can restore it.
        original_branch: str | None = None
        if target is not None:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            original_branch = result.stdout.strip() if result.returncode == 0 else None

            # Switch to the requested merge target
            checkout = subprocess.run(
                ["git", "checkout", target],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            if checkout.returncode != 0:
                _log.warning(
                    "merge_to_base: cannot checkout target branch %r: %s",
                    target,
                    checkout.stderr,
                )
                return

        try:
            # Check if branch has any commits not in HEAD
            diff = subprocess.run(
                ["git", "log", f"HEAD..{branch}", "--oneline"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            if not diff.stdout.strip():
                _log.info("merge_to_base: branch %s has no new commits — skipping", branch)
                return

            result = subprocess.run(
                ["git", "merge", "--squash", branch],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                _log.warning(
                    "merge_to_base: squash merge of %s failed (conflicts?):\n%s",
                    branch,
                    result.stderr,
                )
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=self._repo_root,
                    capture_output=True,
                )
                return

            # Commit the squash result
            target_label = target or "HEAD"
            commit = subprocess.run(
                ["git", "commit", "-m", f"merge: squash worktree branch {branch} into {target_label}"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            if commit.returncode == 0:
                _log.info("merge_to_base: squash-merged %s into %s", branch, target_label)
            else:
                _log.warning("merge_to_base: commit failed: %s", commit.stderr)
        finally:
            # Always restore the original branch when we switched away
            if target is not None and original_branch and original_branch != target:
                subprocess.run(
                    ["git", "checkout", original_branch],
                    cwd=self._repo_root,
                    capture_output=True,
                )

    def _ensure_gitignore(self) -> None:
        """Ensure ``.worktrees/`` is listed in the repo's ``.gitignore``."""
        gitignore = self._repo_root / ".gitignore"
        entry = ".worktrees/"
        if gitignore.exists():
            text = gitignore.read_text()
            if entry not in text.splitlines():
                gitignore.write_text(text.rstrip() + "\n" + entry + "\n")
        else:
            gitignore.write_text(entry + "\n")
