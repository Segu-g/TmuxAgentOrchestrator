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

    def create_from_branch(self, agent_id: str, source_branch: str) -> Path:
        """Create a worktree for *agent_id*, branching from *source_branch*.

        Unlike :meth:`setup` (which always branches from the current HEAD),
        this method creates a new worktree and branch that starts from
        *source_branch*.  This enables sequential branch chaining: phase N+1
        sees all commits made by phase N without requiring a merge.

        The new branch is named ``worktree/{agent_id}`` and the worktree is
        placed at ``{repo_root}/.worktrees/{agent_id}/``.

        Parameters
        ----------
        agent_id:
            Unique ID for the new agent/worktree.
        source_branch:
            Existing branch to branch from (e.g. ``"worktree/worker-ephemeral-abc12345"``).

        Returns
        -------
        Path
            Filesystem path of the newly created worktree directory.

        Raises
        ------
        RuntimeError
            If *source_branch* does not exist or the git operation fails.

        Design reference: DESIGN.md §10.80 (v1.2.4)
        Research: git-worktree add -b <branch> <path> <source>;
        Codex worktree Handoff pattern (developers.openai.com/codex);
        Git Worktrees in the Age of AI Coding Agents (knowledge.buka.sh, 2025).
        """
        branch = f"worktree/{agent_id}"
        wt_path = self._worktrees_dir / agent_id

        with self._lock:
            # Clean up any leftover worktree/branch from a previous run.
            if wt_path.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=self._repo_root,
                    capture_output=True,
                )
                shutil.rmtree(wt_path, ignore_errors=True)
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=self._repo_root,
                capture_output=True,
            )

            result = subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(wt_path), source_branch],
                cwd=self._repo_root,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git worktree add from branch {source_branch!r} failed: "
                    f"{result.stderr.strip()}"
                )
            self._owned[agent_id] = wt_path
            return wt_path

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

    def is_isolated(self, agent_id: str) -> bool:
        """Return ``True`` if *agent_id* uses an isolated worktree (``isolate=True``).

        Returns ``False`` for shared-worktree agents (``isolate=False``).
        Returns ``False`` for agents that are not registered.
        """
        with self._lock:
            return agent_id in self._owned

    def sync_to_branch(
        self,
        agent_id: str,
        *,
        strategy: str = "merge",
        target_branch: str = "master",
        message: str = "",
    ) -> dict:
        """Sync the agent's worktree branch into *target_branch*.

        Supports three strategies:

        - **merge**: ``git merge --no-ff worktree/{agent_id}`` into *target_branch*.
          Preserves the full commit history of the agent branch as a merge commit.
        - **cherry-pick**: Find commits on ``worktree/{agent_id}`` not present in
          *target_branch* and apply them one-by-one with ``git cherry-pick``.
        - **rebase**: Rebase ``worktree/{agent_id}`` onto *target_branch*,
          producing a linear history.

        Parameters
        ----------
        agent_id:
            The agent whose worktree branch should be synced.
        strategy:
            One of ``"merge"``, ``"cherry-pick"``, or ``"rebase"``.
        target_branch:
            The branch to merge/cherry-pick/rebase into.  Defaults to
            ``"master"``.
        message:
            Optional commit message override for merge strategy.  When empty,
            a default message is used.

        Returns
        -------
        dict with keys:
            - ``agent_id``: str
            - ``strategy``: str
            - ``source_branch``: str (``worktree/{agent_id}``)
            - ``target_branch``: str
            - ``commits_synced``: int (number of commits applied)
            - ``merge_commit``: str | None (SHA of resulting merge/final commit)

        Raises
        ------
        ValueError
            When *agent_id* is not an isolated agent (no worktree branch).
        RuntimeError
            When the git operation fails due to merge conflicts or other errors.

        References:
            - git-merge(1): https://git-scm.com/docs/git-merge
            - git-cherry-pick(1): https://git-scm.com/docs/git-cherry-pick
            - git-rebase(1): https://git-scm.com/docs/git-rebase
            - Python cherry-picker (CPython): https://github.com/python/cherry-picker
            - DESIGN.md §10.71 (v1.1.39 — Worktree ↔ Branch Sync)
        """
        with self._lock:
            if agent_id not in self._owned:
                raise ValueError(
                    f"Agent {agent_id!r} does not have an isolated worktree. "
                    "sync_to_branch() is only supported for isolate=True agents."
                )

        source_branch = f"worktree/{agent_id}"

        # Validate strategy
        valid_strategies = ("merge", "cherry-pick", "rebase")
        if strategy not in valid_strategies:
            raise ValueError(
                f"Unknown strategy {strategy!r}. Valid strategies: {valid_strategies}"
            )

        # Discover commits on source_branch not in target_branch
        log_result = subprocess.run(
            ["git", "log", f"{target_branch}..{source_branch}", "--format=%H"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
        )
        if log_result.returncode != 0:
            raise RuntimeError(
                f"git log failed: {log_result.stderr.strip()}"
            )
        commit_shas = [s for s in log_result.stdout.strip().splitlines() if s]
        commits_synced = len(commit_shas)

        if commits_synced == 0:
            # Nothing to sync — return early with zero commits
            head_sha = self._resolve_head(target_branch)
            return {
                "agent_id": agent_id,
                "strategy": strategy,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "commits_synced": 0,
                "merge_commit": head_sha,
            }

        # Worktree path is needed for rebase (which must run inside the worktree)
        worktree_dir = self._worktrees_dir / agent_id

        if strategy == "merge":
            merge_commit = self._sync_merge(
                source_branch, target_branch, message=message
            )
        elif strategy == "cherry-pick":
            # cherry-pick expects oldest first; log returns newest first
            merge_commit = self._sync_cherry_pick(
                list(reversed(commit_shas)), target_branch
            )
        else:  # rebase
            merge_commit = self._sync_rebase(source_branch, target_branch, worktree_dir=worktree_dir)

        return {
            "agent_id": agent_id,
            "strategy": strategy,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "commits_synced": commits_synced,
            "merge_commit": merge_commit,
        }

    # ------------------------------------------------------------------
    # sync_to_branch helpers
    # ------------------------------------------------------------------

    def _resolve_head(self, branch: str) -> str | None:
        """Return the current HEAD SHA of *branch*, or ``None`` on failure."""
        result = subprocess.run(
            ["git", "rev-parse", branch],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None

    def _checkout_branch(self, branch: str) -> str | None:
        """Checkout *branch* in the main repo; return the branch we were on before."""
        orig = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
        )
        original_branch = orig.stdout.strip() if orig.returncode == 0 else None

        if original_branch == branch:
            return branch

        checkout = subprocess.run(
            ["git", "checkout", branch],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
        )
        if checkout.returncode != 0:
            raise RuntimeError(
                f"Cannot checkout {branch!r}: {checkout.stderr.strip()}"
            )
        return original_branch

    def _restore_branch(self, original_branch: str | None, current_branch: str) -> None:
        """Switch back to *original_branch* if we changed branches."""
        if original_branch and original_branch != current_branch:
            subprocess.run(
                ["git", "checkout", original_branch],
                cwd=self._repo_root,
                capture_output=True,
            )

    def _sync_merge(
        self, source_branch: str, target_branch: str, *, message: str = ""
    ) -> str | None:
        """Merge *source_branch* into *target_branch* with ``--no-ff``."""
        original = self._checkout_branch(target_branch)
        try:
            commit_msg = (
                message
                or f"sync: merge {source_branch} into {target_branch} [worktree-sync]"
            )
            result = subprocess.run(
                ["git", "merge", "--no-ff", "-m", commit_msg, source_branch],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                # Abort merge on failure
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=self._repo_root,
                    capture_output=True,
                )
                raise RuntimeError(
                    f"Merge conflict or error merging {source_branch} into "
                    f"{target_branch}: {result.stderr.strip()}"
                )
            return self._resolve_head(target_branch)
        finally:
            self._restore_branch(original, target_branch)

    def _sync_cherry_pick(
        self, commit_shas: list[str], target_branch: str
    ) -> str | None:
        """Cherry-pick *commit_shas* (oldest first) onto *target_branch*."""
        original = self._checkout_branch(target_branch)
        try:
            result = subprocess.run(
                ["git", "cherry-pick", "--allow-empty", "--keep-redundant-commits", *commit_shas],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                # Abort cherry-pick on failure
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=self._repo_root,
                    capture_output=True,
                )
                raise RuntimeError(
                    f"Cherry-pick failed on {target_branch}: {result.stderr.strip()}"
                )
            return self._resolve_head(target_branch)
        finally:
            self._restore_branch(original, target_branch)

    def _sync_rebase(
        self, source_branch: str, target_branch: str, *, worktree_dir: Path
    ) -> str | None:
        """Rebase *source_branch* onto *target_branch*.

        Since ``git rebase <target> <source-branch>`` fails when
        *source_branch* is currently checked out by a worktree (git refuses
        to operate on a branch that is active in another worktree), we run
        ``git rebase <target_branch>`` from *inside* the worktree directory
        instead.  This replays the worktree commits on top of *target_branch*
        and updates the worktree's HEAD in place.

        After a successful rebase the worktree branch (*source_branch*) is
        the new tip.  We then fast-forward *target_branch* to match it.

        Parameters
        ----------
        source_branch:
            The worktree branch to rebase (``worktree/{agent_id}``).
        target_branch:
            The branch to rebase onto.
        worktree_dir:
            Filesystem path of the agent's worktree directory.  git operations
            are run from this directory so that the locked branch is handled
            correctly.
        """
        # Run rebase inside the worktree (avoids "already used by worktree" error)
        result = subprocess.run(
            ["git", "rebase", target_branch],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=worktree_dir,
                capture_output=True,
            )
            raise RuntimeError(
                f"Rebase of {source_branch} onto {target_branch} failed: "
                f"{result.stderr.strip()}"
            )
        # Fast-forward target_branch to the rebased worktree tip
        original = self._checkout_branch(target_branch)
        try:
            ff = subprocess.run(
                ["git", "merge", "--ff-only", source_branch],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
            )
            if ff.returncode != 0:
                raise RuntimeError(
                    f"Fast-forward of {target_branch} to rebased {source_branch} failed: "
                    f"{ff.stderr.strip()}"
                )
            return self._resolve_head(target_branch)
        finally:
            self._restore_branch(original, target_branch)

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
