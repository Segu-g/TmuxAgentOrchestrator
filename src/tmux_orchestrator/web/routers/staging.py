"""Staging area APIRouter — /staging/* endpoints.

Provides a simple file staging area where agents can PUT files (binary or
text) via multipart upload and retrieve them later by file_id.

Intended agent workflow:
1. Producer: ``curl -X POST /staging -F file=@result.py`` → ``{"file_id": "abc123"}``
2. Producer stores file_id in scratchpad: ``PUT /scratchpad/my_artifact_id``
3. Consumer reads file_id from scratchpad, downloads:
   ``curl /staging/abc123 -o received.py``

Design patterns:
- Blackboard (Buschmann et al. POSA 1996): shared working memory, indirect
  collaboration without direct coupling.
- Azure Pipelines ArtifactStagingDirectory: pipeline-stage artifact handoff.
- A2A artifact protocol (arXiv:2505.02279, 2025): task + artifact + metadata.

Reference: DESIGN.md §10.94 (v1.2.18)
FastAPI File Upload: https://fastapi.tiangolo.com/reference/uploadfile/
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import Response

from tmux_orchestrator.application.staging_store import StagingStore


def build_staging_router(
    auth: Callable,
    staging_store: StagingStore,
) -> APIRouter:
    """Build and return the staging area APIRouter.

    Parameters
    ----------
    auth:
        Authentication dependency callable (combined session + API key).
    staging_store:
        Shared :class:`~tmux_orchestrator.application.staging_store.StagingStore`
        instance.  Pass the same object that ``create_app`` created so all
        routers share state.
    """
    router = APIRouter()

    @router.post(
        "/staging",
        status_code=201,
        summary="Upload a file to the staging area",
        dependencies=[Depends(auth)],
    )
    async def upload_file(
        file: UploadFile,
        uploaded_by: str | None = Query(None, description="Agent ID of the uploader"),
    ) -> dict:
        """Stage a file for later retrieval by a downstream agent.

        Accepts a ``multipart/form-data`` request with a single ``file`` field.

        Returns a JSON object with ``file_id``, ``filename``, and
        ``size_bytes``.  Store the ``file_id`` in the scratchpad so that
        downstream agents can retrieve the file.

        ```bash
        curl -X POST {base_url}/staging \\
          -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY" \\
          -F "file=@myfile.py" \\
          -F "uploaded_by=producer-agent"
        ```

        Reference: DESIGN.md §10.94 (v1.2.18)
        """
        content = await file.read()
        sf = staging_store.put(
            file.filename or "unnamed",
            content,
            content_type=file.content_type or "application/octet-stream",
            uploaded_by=uploaded_by,
        )
        return {
            "file_id": sf.file_id,
            "filename": sf.filename,
            "size_bytes": sf.size_bytes,
            "content_type": sf.content_type,
            "uploaded_at": sf.uploaded_at,
            "uploaded_by": sf.uploaded_by,
        }

    @router.get(
        "/staging",
        summary="List all staged files (metadata only)",
        dependencies=[Depends(auth)],
    )
    async def list_files() -> list[dict]:
        """Return metadata for all currently staged files.

        Does NOT return file contents.  Use ``GET /staging/{file_id}`` to
        download a specific file.

        Reference: DESIGN.md §10.94 (v1.2.18)
        """
        return [
            {
                "file_id": sf.file_id,
                "filename": sf.filename,
                "content_type": sf.content_type,
                "size_bytes": sf.size_bytes,
                "uploaded_at": sf.uploaded_at,
                "uploaded_by": sf.uploaded_by,
            }
            for sf in staging_store.list_all()
        ]

    @router.get(
        "/staging/{file_id}/meta",
        summary="Get staged file metadata (no content)",
        dependencies=[Depends(auth)],
    )
    async def get_file_meta(file_id: str) -> dict:
        """Return metadata for a specific staged file without downloading content.

        Useful when a consumer agent only needs to verify that a file exists
        and check its size/type before downloading.

        Reference: DESIGN.md §10.94 (v1.2.18)
        """
        sf = staging_store.get(file_id)
        if sf is None:
            raise HTTPException(status_code=404, detail=f"Staged file {file_id!r} not found")
        return {
            "file_id": sf.file_id,
            "filename": sf.filename,
            "content_type": sf.content_type,
            "size_bytes": sf.size_bytes,
            "uploaded_at": sf.uploaded_at,
            "uploaded_by": sf.uploaded_by,
        }

    @router.get(
        "/staging/{file_id}",
        summary="Download a staged file",
        dependencies=[Depends(auth)],
    )
    async def download_file(file_id: str) -> Response:
        """Download the content of a staged file.

        Returns the raw file bytes with the original ``Content-Type`` and a
        ``Content-Disposition: attachment`` header so that ``curl -O`` saves
        the file under the original filename.

        ```bash
        curl -s {base_url}/staging/{file_id} \\
          -H "X-API-Key: $TMUX_ORCHESTRATOR_API_KEY" \\
          -o received.py
        ```

        Reference: DESIGN.md §10.94 (v1.2.18)
        """
        sf = staging_store.get(file_id)
        if sf is None:
            raise HTTPException(status_code=404, detail=f"Staged file {file_id!r} not found")
        content = staging_store.get_content(file_id)
        if content is None:
            raise HTTPException(status_code=404, detail=f"Staged file {file_id!r} content not available")
        return Response(
            content=content,
            media_type=sf.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{sf.filename}"',
            },
        )

    @router.delete(
        "/staging/{file_id}",
        summary="Delete a staged file",
        dependencies=[Depends(auth)],
    )
    async def delete_file(file_id: str) -> dict:
        """Remove a staged file from the staging area.

        Returns 404 if the file does not exist.

        Reference: DESIGN.md §10.94 (v1.2.18)
        """
        deleted = staging_store.delete(file_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Staged file {file_id!r} not found")
        return {"file_id": file_id, "deleted": True}

    return router
