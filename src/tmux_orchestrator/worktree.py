"""Git worktree manager: creates and removes per-agent isolated working trees."""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path


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

    def teardown(self, agent_id: str) -> None:
        """Remove the worktree and branch for *agent_id*.

        No-op (beyond deregistration) for shared-worktree agents.
        """
        with self._lock:
            if agent_id in self._shared:
                self._shared.discard(agent_id)
                return

            path = self._owned.pop(agent_id, None)
            if path is None:
                return

            branch = f"worktree/{agent_id}"
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

    def worktree_path(self, agent_id: str) -> Path | None:
        """Return the current worktree path for *agent_id*, or ``None`` if not set up."""
        with self._lock:
            return self._owned.get(agent_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
