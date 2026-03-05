"""OpenAPI schema contract regression test — zero extra dependencies.

Usage
-----
- First run (or after intentional API changes): set UPDATE_SNAPSHOTS=1 to regenerate.
  ``UPDATE_SNAPSHOTS=1 pytest tests/test_openapi_schema.py``
- Normal CI run: compare against committed snapshot; fail on divergence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tmux_orchestrator.web.app import create_app

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "openapi_schema.json"


class _MockHub:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def handle(self, ws) -> None:
        pass


class _MockOrchestrator:
    _dispatch_task = None

    def list_agents(self) -> list:
        return []

    def list_tasks(self) -> list:
        return []

    def get_agent(self, agent_id: str):
        return None

    def get_director(self):
        return None

    def flush_director_pending(self) -> list:
        return []

    def list_dlq(self) -> list:
        return []

    @property
    def is_paused(self) -> bool:
        return False

    def get_rate_limiter_status(self) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def reconfigure_rate_limiter(self, *, rate: float, burst: int) -> dict:
        return {"enabled": False, "rate": 0.0, "burst": 0, "available_tokens": 0.0}

    def get_agent_context_stats(self, agent_id: str) -> dict | None:
        return None

    def all_agent_context_stats(self) -> list:
        return []

    def get_agent_history(self, agent_id: str, limit: int = 50) -> list | None:
        return None


@pytest.fixture
def fastapi_app():
    return create_app(_MockOrchestrator(), _MockHub(), api_key="test-key")


def test_openapi_schema_contract(fastapi_app):
    """Fail when the OpenAPI schema diverges from the committed snapshot.

    This guards against unintentional REST API contract changes. When you
    intentionally change the API, regenerate the snapshot:
        UPDATE_SNAPSHOTS=1 pytest tests/test_openapi_schema.py
    """
    schema = fastapi_app.openapi()
    # Strip auto-incremented version to avoid false positives
    schema_comparable = {k: v for k, v in schema.items() if k != "info"}

    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(schema_comparable, indent=2, sort_keys=True))
        pytest.skip(f"Snapshot updated at {SNAPSHOT_PATH}")

    if not SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(schema_comparable, indent=2, sort_keys=True))
        pytest.skip(f"Snapshot created at {SNAPSHOT_PATH} — commit it to lock the contract")

    saved = json.loads(SNAPSHOT_PATH.read_text())
    assert schema_comparable == saved, (
        "OpenAPI schema changed. If intentional, regenerate with: "
        "UPDATE_SNAPSHOTS=1 pytest tests/test_openapi_schema.py"
    )
