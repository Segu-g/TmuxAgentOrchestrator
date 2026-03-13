"""Shared file staging area for inter-agent artifact handoff.

Implements a simple file staging store where agents can PUT files (binary or
text) via REST and subsequent agents can GET them by file_id.

Files are stored in ``{staging_dir}/{file_id}`` on the server.  Metadata is
kept in-memory only (lost on server restart — the staging area is intentionally
transient, like CI/CD artifact staging).

Design patterns:
- Blackboard pattern (Buschmann et al. 1996): shared working memory where one
  agent writes a result and another picks it up asynchronously.
- Azure Pipelines ArtifactStagingDirectory: each pipeline stage PUTs artifacts,
  subsequent stages GET them by ID.
- A2A Protocol artifacts (arXiv:2505.02279, 2025): task_id + artifact + metadata.

Intended usage by agents (via REST):
1. Producer calls ``POST /staging`` with a file, gets ``file_id`` back.
2. Producer writes file_id to scratchpad: ``PUT /scratchpad/my_artifact``.
3. Consumer reads file_id from scratchpad, calls ``GET /staging/{file_id}``.

Reference: DESIGN.md §10.94 (v1.2.18)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class StagingFile:
    """Metadata record for a file stored in the staging area.

    Attributes
    ----------
    file_id:
        12-hex random identifier (e.g. ``"a3f2c1b0d9e8"``).
    filename:
        Original filename provided by the uploader.
    content_type:
        MIME type (e.g. ``"text/plain"``, ``"application/octet-stream"``).
    size_bytes:
        Number of bytes in the file.
    uploaded_at:
        ISO 8601 UTC timestamp of when the file was staged.
    uploaded_by:
        Agent ID of the uploader; ``None`` if not provided.
    path:
        Absolute filesystem path where the file content is stored.
    """

    file_id: str
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: str
    uploaded_by: str | None
    path: Path


class StagingStore:
    """In-process + on-disk file staging area.

    The staging dir is created lazily on the first ``put()`` call (or at
    ``__init__`` time when *staging_dir* is provided).  Files are written to
    ``{staging_dir}/{file_id}`` so that they survive server restart on disk,
    but the **metadata** (``_files`` dict) is in-memory only — the store is
    intentionally transient (a restart clears the index even though files
    remain on disk).

    Parameters
    ----------
    staging_dir:
        Root directory for file storage.  Defaults to a temporary in-memory
        mode (no files written) when ``None``.  When provided, the directory
        is created with ``parents=True, exist_ok=True``.
    """

    def __init__(self, staging_dir: Path | None = None) -> None:
        self._dir: Path | None = staging_dir
        if staging_dir is not None:
            staging_dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, StagingFile] = {}

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def put(
        self,
        filename: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        uploaded_by: str | None = None,
    ) -> StagingFile:
        """Stage a file and return its metadata record.

        Parameters
        ----------
        filename:
            Original filename (used in ``Content-Disposition`` on download).
        content:
            Raw file bytes.
        content_type:
            MIME type of the content.
        uploaded_by:
            Agent ID of the uploader (optional, for audit trail).

        Returns
        -------
        StagingFile
            Metadata record with ``file_id`` that can be used to retrieve the
            file later.
        """
        file_id = uuid.uuid4().hex[:12]
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / file_id
            path.write_bytes(content)
        else:
            # In-memory fallback: store content as a fake path attribute.
            # We create a placeholder Path and stash content separately.
            path = Path(f"/dev/null/{file_id}")
            self._in_memory: dict[str, bytes]  # type: ignore[has-type]
            if not hasattr(self, "_in_memory"):
                self._in_memory = {}
            self._in_memory[file_id] = content

        sf = StagingFile(
            file_id=file_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            uploaded_at=datetime.now(UTC).isoformat(),
            uploaded_by=uploaded_by,
            path=path,
        )
        self._files[file_id] = sf
        return sf

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, file_id: str) -> StagingFile | None:
        """Return metadata for *file_id*, or ``None`` if not found."""
        return self._files.get(file_id)

    def get_content(self, file_id: str) -> bytes | None:
        """Return raw bytes for *file_id*, or ``None`` if not found.

        Reads from the in-memory store when no ``staging_dir`` was provided,
        otherwise reads from the on-disk file.
        """
        sf = self.get(file_id)
        if sf is None:
            return None
        if self._dir is not None:
            if sf.path.exists():
                return sf.path.read_bytes()
            return None
        # In-memory fallback
        return getattr(self, "_in_memory", {}).get(file_id)

    def list_all(self) -> list[StagingFile]:
        """Return all staged file metadata records (in insertion order)."""
        return list(self._files.values())

    # ------------------------------------------------------------------
    # Delete operations
    # ------------------------------------------------------------------

    def delete(self, file_id: str) -> bool:
        """Remove a staged file.

        Parameters
        ----------
        file_id:
            ID of the file to delete.

        Returns
        -------
        bool
            ``True`` if the file existed and was removed; ``False`` if not
            found.
        """
        sf = self._files.pop(file_id, None)
        if sf is None:
            return False
        if self._dir is not None and sf.path.exists():
            sf.path.unlink()
        elif hasattr(self, "_in_memory") and file_id in self._in_memory:
            del self._in_memory[file_id]
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Remove all staged files (used in tests for fixture cleanup)."""
        for file_id in list(self._files):
            self.delete(file_id)
