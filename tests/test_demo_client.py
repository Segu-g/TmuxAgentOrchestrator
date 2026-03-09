"""Tests for tmux_orchestrator.testing.demo_client.

Tests cover:
1. wait_for_agent_done — success, timeout, task not yet in history, agent not found,
   finished_at=None (task exists but incomplete)
2. wait_for_server — success and timeout
3. api — basic GET/POST round-trip (mocked)

All HTTP calls are intercepted via a custom urllib opener or by patching
urllib.request.urlopen directly.

References:
    - DESIGN.md §10.N (v1.0.18 — demo stability tests)
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from tmux_orchestrator.testing.demo_client import api, wait_for_agent_done, wait_for_server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(body: object, status: int = 200) -> MagicMock:
    """Return a mock urllib response object."""
    data = json.dumps(body).encode()
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost/agents/a/history",
        code=code,
        msg="Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )


# ---------------------------------------------------------------------------
# wait_for_agent_done
# ---------------------------------------------------------------------------


class TestWaitForAgentDone:
    def test_success_immediate(self) -> None:
        """Task found with finished_at set on the first poll."""
        record = {
            "task_id": "task-123",
            "prompt": "hello",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "duration_s": 60.0,
            "status": "success",
            "error": None,
        }
        resp = _make_response([record])

        with patch("urllib.request.urlopen", return_value=resp):
            result = wait_for_agent_done(
                "http://localhost:8000", "agent-a", "task-123", api_key="key"
            )

        assert result["task_id"] == "task-123"
        assert result["finished_at"] == "2026-01-01T00:01:00Z"
        assert result["status"] == "success"

    def test_timeout_when_history_always_empty(self) -> None:
        """TimeoutError raised if history never contains the task_id."""
        resp = _make_response([])  # always empty

        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(TimeoutError, match="task-999"):
                wait_for_agent_done(
                    "http://localhost:8000",
                    "agent-a",
                    "task-999",
                    timeout=0.1,
                    poll_interval=0.05,
                )

    def test_not_yet_in_history_then_appears(self) -> None:
        """Task missing for first N polls, then appears with finished_at set."""
        record = {
            "task_id": "task-456",
            "finished_at": "2026-01-01T00:02:00Z",
            "status": "success",
            "error": None,
        }
        call_count = 0

        def side_effect(req, timeout=None):  # noqa: ANN001
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return _make_response([])  # not yet
            return _make_response([record])

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = wait_for_agent_done(
                "http://localhost:8000",
                "agent-a",
                "task-456",
                timeout=5.0,
                poll_interval=0.05,
            )

        assert result["task_id"] == "task-456"
        assert call_count >= 3

    def test_agent_not_found_raises_runtime_error(self) -> None:
        """RuntimeError raised on HTTP 404."""
        with patch(
            "urllib.request.urlopen", side_effect=_make_http_error(404)
        ):
            with pytest.raises(RuntimeError, match="not found"):
                wait_for_agent_done(
                    "http://localhost:8000", "no-such-agent", "task-1"
                )

    def test_finished_at_none_continues_polling(self) -> None:
        """Record exists but finished_at=None → keep polling until finished_at is set."""
        incomplete = {
            "task_id": "task-789",
            "finished_at": None,
            "status": "running",
            "error": None,
        }
        complete = {
            "task_id": "task-789",
            "finished_at": "2026-01-01T00:05:00Z",
            "status": "success",
            "error": None,
        }
        call_count = 0

        def side_effect(req, timeout=None):  # noqa: ANN001
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return _make_response([incomplete])
            return _make_response([complete])

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = wait_for_agent_done(
                "http://localhost:8000",
                "agent-a",
                "task-789",
                timeout=5.0,
                poll_interval=0.05,
            )

        assert result["finished_at"] == "2026-01-01T00:05:00Z"

    def test_non_404_http_error_retries(self) -> None:
        """HTTP 500 is retried, not raised immediately."""
        record = {
            "task_id": "task-500",
            "finished_at": "2026-01-01T00:01:00Z",
            "status": "success",
            "error": None,
        }
        call_count = 0

        def side_effect(req, timeout=None):  # noqa: ANN001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _make_http_error(500)
            return _make_response([record])

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = wait_for_agent_done(
                "http://localhost:8000",
                "agent-a",
                "task-500",
                timeout=5.0,
                poll_interval=0.05,
            )

        assert result["task_id"] == "task-500"
        assert call_count == 2

    def test_invalid_args_raise_value_error(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            wait_for_agent_done("", "agent-a", "task-1")
        with pytest.raises(ValueError, match="agent_id"):
            wait_for_agent_done("http://localhost", "", "task-1")
        with pytest.raises(ValueError, match="task_id"):
            wait_for_agent_done("http://localhost", "agent-a", "")

    def test_record_without_task_id_key_skipped(self) -> None:
        """Records without 'task_id' key are skipped (no KeyError)."""
        records = [
            {"prompt": "old task"},  # no task_id key
            {"task_id": "task-ok", "finished_at": "2026-01-01T00:01:00Z", "status": "success"},
        ]
        resp = _make_response(records)

        with patch("urllib.request.urlopen", return_value=resp):
            result = wait_for_agent_done(
                "http://localhost:8000", "agent-a", "task-ok"
            )

        assert result["task_id"] == "task-ok"


# ---------------------------------------------------------------------------
# wait_for_server
# ---------------------------------------------------------------------------


class TestWaitForServer:
    def test_server_up_immediately(self) -> None:
        resp = _make_response([])  # empty agents list

        with patch("urllib.request.urlopen", return_value=resp):
            result = wait_for_server("http://localhost:8000", api_key="key", timeout=5.0)

        assert result is True

    def test_timeout_when_server_never_responds(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = wait_for_server("http://localhost:8000", timeout=0.15)

        assert result is False

    def test_http_error_below_500_is_success(self) -> None:
        """A 401 response means the server is up (auth gate)."""
        with patch(
            "urllib.request.urlopen",
            side_effect=_make_http_error(401),
        ):
            result = wait_for_server("http://localhost:8000", timeout=5.0)

        assert result is True


# ---------------------------------------------------------------------------
# api
# ---------------------------------------------------------------------------


class TestApi:
    def test_get_request(self) -> None:
        agents = [{"id": "agent-a", "status": "IDLE"}]
        resp = _make_response(agents)

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = api("GET", "http://localhost:8000", "/agents", api_key="key")

        assert result == agents
        req = mock_open.call_args[0][0]
        assert req.get_method() == "GET"
        assert req.get_header("X-api-key") == "key"

    def test_post_request_with_data(self) -> None:
        task = {"task_id": "task-abc", "status": "queued"}
        resp = _make_response(task)

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = api(
                "POST",
                "http://localhost:8000",
                "/tasks",
                {"prompt": "hello"},
                api_key="key",
            )

        assert result["task_id"] == "task-abc"
        req = mock_open.call_args[0][0]
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {"prompt": "hello"}
