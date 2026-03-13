"""Tests for StagingStore and /staging/* REST endpoints.

Reference: DESIGN.md §10.94 (v1.2.18)
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_staging_dir(tmp_path: Path) -> Path:
    """A temporary directory for staging files."""
    d = tmp_path / "staging"
    return d  # Not created yet — StagingStore should create it


@pytest.fixture()
def store(tmp_staging_dir: Path):
    from tmux_orchestrator.application.staging_store import StagingStore
    s = StagingStore(staging_dir=tmp_staging_dir)
    yield s
    s.clear()


@pytest.fixture()
def inmem_store():
    """StagingStore with no staging_dir (in-memory only)."""
    from tmux_orchestrator.application.staging_store import StagingStore
    s = StagingStore()
    yield s
    s.clear()


class _StubHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.fixture()
def app_client(tmp_staging_dir):
    """TestClient for the FastAPI app with a real StagingStore."""
    from unittest.mock import MagicMock

    from tmux_orchestrator.bus import Bus
    from tmux_orchestrator.application.config import OrchestratorConfig
    from tmux_orchestrator.orchestrator import Orchestrator
    from tmux_orchestrator.web.app import create_app

    config = OrchestratorConfig(
        session_name="test",
        agents=[],
        p2p_permissions=[],
        task_timeout=10,
        watchdog_poll=9999.0,
        recovery_poll=9999.0,
        staging_dir=str(tmp_staging_dir),
    )
    bus = Bus()
    tmux = MagicMock()
    tmux.start_watcher = MagicMock()
    tmux.stop_watcher = MagicMock()
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    app = create_app(orch, _StubHub(), api_key="test-key")  # type: ignore[arg-type]
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


# ---------------------------------------------------------------------------
# Unit tests — StagingStore
# ---------------------------------------------------------------------------

class TestStagingStoreBasics:
    def test_put_returns_staging_file_with_correct_fields(self, store):
        from tmux_orchestrator.application.staging_store import StagingFile
        sf = store.put("hello.txt", b"hello world", content_type="text/plain")
        assert isinstance(sf, StagingFile)
        assert sf.filename == "hello.txt"
        assert sf.content_type == "text/plain"
        assert sf.size_bytes == 11
        assert sf.file_id
        assert len(sf.file_id) == 12
        assert sf.uploaded_at
        assert sf.uploaded_by is None

    def test_put_with_uploaded_by(self, store):
        sf = store.put("data.bin", b"\x00\x01\x02", uploaded_by="agent-1")
        assert sf.uploaded_by == "agent-1"

    def test_get_returns_correct_metadata(self, store):
        sf = store.put("file.py", b"print('hi')")
        retrieved = store.get(sf.file_id)
        assert retrieved is not None
        assert retrieved.file_id == sf.file_id
        assert retrieved.filename == "file.py"
        assert retrieved.size_bytes == 11

    def test_get_returns_none_for_unknown_id(self, store):
        assert store.get("nonexistent") is None

    def test_get_content_returns_uploaded_bytes(self, store):
        content = b"binary\x00data\xff"
        sf = store.put("bin.dat", content, content_type="application/octet-stream")
        retrieved = store.get_content(sf.file_id)
        assert retrieved == content

    def test_get_content_returns_none_for_unknown_id(self, store):
        assert store.get_content("nonexistent") is None

    def test_delete_removes_file_returns_true(self, store):
        sf = store.put("del.txt", b"to delete")
        result = store.delete(sf.file_id)
        assert result is True
        assert store.get(sf.file_id) is None

    def test_delete_returns_false_for_unknown_id(self, store):
        result = store.delete("nonexistent")
        assert result is False

    def test_list_all_returns_all_files(self, store):
        store.put("a.txt", b"aaa")
        store.put("b.txt", b"bbb")
        store.put("c.txt", b"ccc")
        files = store.list_all()
        assert len(files) == 3
        filenames = {f.filename for f in files}
        assert filenames == {"a.txt", "b.txt", "c.txt"}

    def test_staging_dir_created_if_not_exists(self, tmp_path):
        from tmux_orchestrator.application.staging_store import StagingStore
        d = tmp_path / "new" / "staging"
        assert not d.exists()
        StagingStore(staging_dir=d)
        assert d.exists()

    def test_file_actually_written_to_disk(self, store, tmp_staging_dir):
        sf = store.put("ondisk.txt", b"disk content")
        disk_path = tmp_staging_dir / sf.file_id
        assert disk_path.exists()
        assert disk_path.read_bytes() == b"disk content"

    def test_delete_removes_file_from_disk(self, store, tmp_staging_dir):
        sf = store.put("gone.txt", b"bye")
        disk_path = tmp_staging_dir / sf.file_id
        assert disk_path.exists()
        store.delete(sf.file_id)
        assert not disk_path.exists()

    def test_inmemory_store_put_and_get(self, inmem_store):
        sf = inmem_store.put("mem.txt", b"in memory")
        content = inmem_store.get_content(sf.file_id)
        assert content == b"in memory"

    def test_clear_removes_all(self, store):
        store.put("x.txt", b"x")
        store.put("y.txt", b"y")
        store.clear()
        assert store.list_all() == []

    def test_file_id_unique_across_puts(self, store):
        ids = {store.put(f"{i}.txt", b"data").file_id for i in range(20)}
        assert len(ids) == 20


# ---------------------------------------------------------------------------
# Integration tests — REST endpoints
# ---------------------------------------------------------------------------

AUTH = {"X-API-Key": "test-key"}


class TestStagingRestEndpoints:
    def test_post_staging_returns_file_id_and_metadata(self, app_client):
        content = b"def bubble_sort(arr): pass"
        resp = app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("sort.py", io.BytesIO(content), "text/x-python")},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "file_id" in data
        assert data["filename"] == "sort.py"
        assert data["size_bytes"] == len(content)
        assert data["content_type"] == "text/x-python"

    def test_get_staging_file_returns_content_with_correct_type(self, app_client):
        content = b"hello from agent"
        upload = app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("hello.txt", io.BytesIO(content), "text/plain")},
        )
        file_id = upload.json()["file_id"]

        download = app_client.get(f"/staging/{file_id}", headers=AUTH)
        assert download.status_code == 200
        assert download.content == content
        assert download.headers["content-type"].startswith("text/plain")
        assert 'attachment; filename="hello.txt"' in download.headers["content-disposition"]

    def test_get_staging_meta_returns_metadata_only(self, app_client):
        content = b"metadata test"
        upload = app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("meta.txt", io.BytesIO(content), "text/plain")},
        )
        file_id = upload.json()["file_id"]

        meta = app_client.get(f"/staging/{file_id}/meta", headers=AUTH)
        assert meta.status_code == 200
        data = meta.json()
        assert data["file_id"] == file_id
        assert data["filename"] == "meta.txt"
        assert data["size_bytes"] == len(content)
        # Content should NOT be present in meta response
        assert "content" not in data

    def test_get_staging_list_returns_all_files(self, app_client):
        app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("a.txt", io.BytesIO(b"a"), "text/plain")},
        )
        app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("b.txt", io.BytesIO(b"b"), "text/plain")},
        )
        resp = app_client.get("/staging", headers=AUTH)
        assert resp.status_code == 200
        files = resp.json()
        # At least 2 files (could be more if other tests ran in same app instance)
        assert any(f["filename"] == "a.txt" for f in files)
        assert any(f["filename"] == "b.txt" for f in files)

    def test_delete_staging_file_removes_it(self, app_client):
        upload = app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("delete_me.txt", io.BytesIO(b"bye"), "text/plain")},
        )
        file_id = upload.json()["file_id"]

        del_resp = app_client.delete(f"/staging/{file_id}", headers=AUTH)
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True

        get_resp = app_client.get(f"/staging/{file_id}", headers=AUTH)
        assert get_resp.status_code == 404

    def test_get_nonexistent_file_returns_404(self, app_client):
        resp = app_client.get("/staging/nonexistent123", headers=AUTH)
        assert resp.status_code == 404

    def test_get_meta_nonexistent_returns_404(self, app_client):
        resp = app_client.get("/staging/nonexistent123/meta", headers=AUTH)
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, app_client):
        resp = app_client.delete("/staging/nonexistent123", headers=AUTH)
        assert resp.status_code == 404

    def test_upload_binary_file(self, app_client):
        binary_content = bytes(range(256))
        upload = app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("binary.bin", io.BytesIO(binary_content), "application/octet-stream")},
        )
        assert upload.status_code == 201
        file_id = upload.json()["file_id"]

        download = app_client.get(f"/staging/{file_id}", headers=AUTH)
        assert download.status_code == 200
        assert download.content == binary_content

    def test_upload_with_uploaded_by_query_param(self, app_client):
        resp = app_client.post(
            "/staging?uploaded_by=producer-agent",
            headers=AUTH,
            files={"file": ("report.txt", io.BytesIO(b"report"), "text/plain")},
        )
        assert resp.status_code == 201
        assert resp.json()["uploaded_by"] == "producer-agent"

    def test_staging_pipeline_producer_consumer(self, app_client):
        """End-to-end: producer uploads file, consumer downloads it."""
        # Producer uploads
        code = b"def merge_sort(arr):\n    if len(arr) <= 1: return arr\n    mid = len(arr) // 2\n    return merge(merge_sort(arr[:mid]), merge_sort(arr[mid:]))"
        upload = app_client.post(
            "/staging",
            headers=AUTH,
            files={"file": ("algorithm.py", io.BytesIO(code), "text/x-python")},
        )
        assert upload.status_code == 201
        file_id = upload.json()["file_id"]

        # Consumer downloads
        download = app_client.get(f"/staging/{file_id}", headers=AUTH)
        assert download.status_code == 200
        assert download.content == code
