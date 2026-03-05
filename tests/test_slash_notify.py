"""Unit tests for slash_notify — parent notification from /plan and /tdd commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import urllib.error

import pytest

from tmux_orchestrator.slash_notify import notify_parent, build_parent_message


class TestBuildParentMessage:
    """Tests for the message payload builder."""

    def test_build_plan_created_event(self) -> None:
        payload = build_parent_message(
            agent_id="worker-1",
            event_type="plan_created",
            extra={"description": "implement auth", "plan_path": "PLAN.md"},
        )
        assert payload["event"] == "plan_created"
        assert payload["from_id"] == "worker-1"
        assert payload["description"] == "implement auth"
        assert payload["plan_path"] == "PLAN.md"
        assert "timestamp" in payload

    def test_build_tdd_cycle_started_event(self) -> None:
        payload = build_parent_message(
            agent_id="worker-2",
            event_type="tdd_cycle_started",
            extra={"feature": "token refresh", "phase": "red"},
        )
        assert payload["event"] == "tdd_cycle_started"
        assert payload["from_id"] == "worker-2"
        assert payload["feature"] == "token refresh"
        assert payload["phase"] == "red"

    def test_build_message_has_timestamp(self) -> None:
        payload = build_parent_message(
            agent_id="a",
            event_type="test_event",
            extra={},
        )
        assert isinstance(payload["timestamp"], str)
        assert "T" in payload["timestamp"]  # ISO 8601 format

    def test_extra_fields_merged_into_payload(self) -> None:
        payload = build_parent_message(
            agent_id="a",
            event_type="plan_created",
            extra={"foo": "bar", "baz": 42},
        )
        assert payload["foo"] == "bar"
        assert payload["baz"] == 42


class TestNotifyParent:
    """Tests for notify_parent() — the end-to-end notification function."""

    def test_no_op_when_context_file_missing(self, tmp_path: Path) -> None:
        """notify_parent silently returns False when context file doesn't exist."""
        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            result = notify_parent(
                event_type="plan_created",
                extra={"description": "test"},
            )
        assert result is False

    def test_no_op_when_no_parent_id(self, tmp_path: Path) -> None:
        """notify_parent returns False when agent has no parent."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

        # GET /agents returns agent with parent_id=None
        agents_response = [{"id": "worker-1", "parent_id": None}]

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                result = notify_parent(
                    event_type="plan_created",
                    extra={"description": "test"},
                )
        assert result is False

    def test_sends_post_when_parent_exists(self, tmp_path: Path) -> None:
        """notify_parent sends POST /agents/{parent_id}/message when parent is set."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

        agents_response = [
            {"id": "worker-1", "parent_id": "director"},
            {"id": "director", "parent_id": None},
        ]
        posted_data: list[dict] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            if hasattr(req, "data") and req.data is not None:
                posted_data.append(json.loads(req.data.decode()))
                mock_resp.read.return_value = json.dumps({"message_id": "msg-1", "to_id": "director"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                result = notify_parent(
                    event_type="plan_created",
                    extra={"description": "implement auth"},
                )

        assert result is True
        assert len(posted_data) == 1
        body = posted_data[0]
        assert body["type"] == "PEER_MSG"
        assert body["payload"]["event"] == "plan_created"
        assert body["payload"]["from_id"] == "worker-1"
        assert body["payload"]["description"] == "implement auth"

    def test_returns_false_on_http_error(self, tmp_path: Path) -> None:
        """notify_parent returns False (does not raise) on HTTP error."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

        agents_response = [{"id": "worker-1", "parent_id": "director"}]
        call_count = [0]

        def mock_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: GET /agents succeeds
                mock_resp = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_resp.read.return_value = json.dumps(agents_response).encode()
                return mock_resp
            else:
                # Second call: POST fails
                raise urllib.error.HTTPError(
                    url="http://localhost:8000/agents/director/message",
                    code=500,
                    msg="Internal Server Error",
                    hdrs=None,  # type: ignore[arg-type]
                    fp=None,
                )

        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                result = notify_parent(
                    event_type="plan_created",
                    extra={"description": "test"},
                )
        assert result is False

    def test_returns_false_on_connection_error(self, tmp_path: Path) -> None:
        """notify_parent returns False (does not raise) on connection error."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

        def mock_urlopen(req, timeout=None):
            raise OSError("Connection refused")

        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                result = notify_parent(
                    event_type="plan_created",
                    extra={"description": "test"},
                )
        assert result is False

    def test_plan_content_included_when_plan_md_exists(self, tmp_path: Path) -> None:
        """notify_parent includes PLAN.md content in payload when file exists."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))
        plan_content = "# Plan\n\n## Objective\nImplement auth\n"
        (tmp_path / "PLAN.md").write_text(plan_content)

        agents_response = [{"id": "worker-1", "parent_id": "director"}]
        posted_data: list[dict] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            if hasattr(req, "data") and req.data is not None:
                posted_data.append(json.loads(req.data.decode()))
                mock_resp.read.return_value = json.dumps({"message_id": "msg-1"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                result = notify_parent(
                    event_type="plan_created",
                    extra={"description": "implement auth"},
                )

        assert result is True
        assert posted_data[0]["payload"]["plan_content"] == plan_content

    def test_post_url_uses_parent_id(self, tmp_path: Path) -> None:
        """The POST is sent to /agents/{parent_id}/message."""
        ctx = {
            "agent_id": "child-agent",
            "web_base_url": "http://localhost:9000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

        agents_response = [{"id": "child-agent", "parent_id": "parent-agent"}]
        captured_urls: list[str] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            if hasattr(req, "full_url"):
                captured_urls.append(req.full_url)
            elif hasattr(req, "get_full_url"):
                captured_urls.append(req.get_full_url())
            if hasattr(req, "data") and req.data is not None:
                mock_resp.read.return_value = json.dumps({"message_id": "m"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                notify_parent(
                    event_type="tdd_cycle_started",
                    extra={"feature": "login"},
                )

        # The second URL should be the POST
        post_url = next((u for u in captured_urls if "message" in u), None)
        assert post_url is not None
        assert "parent-agent" in post_url
        assert post_url == "http://localhost:9000/agents/parent-agent/message"

    def test_tdd_notification_payload(self, tmp_path: Path) -> None:
        """TDD cycle start notification has correct event type and fields."""
        ctx = {
            "agent_id": "worker-1",
            "web_base_url": "http://localhost:8000",
            "session_name": "orch",
            "mailbox_dir": str(tmp_path),
            "worktree_path": str(tmp_path),
        }
        (tmp_path / "__orchestrator_context__.json").write_text(json.dumps(ctx))

        agents_response = [{"id": "worker-1", "parent_id": "director"}]
        posted_data: list[dict] = []

        def mock_urlopen(req, timeout=None):
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            if hasattr(req, "data") and req.data is not None:
                posted_data.append(json.loads(req.data.decode()))
                mock_resp.read.return_value = json.dumps({"message_id": "m"}).encode()
            else:
                mock_resp.read.return_value = json.dumps(agents_response).encode()
            return mock_resp

        with patch("tmux_orchestrator.slash_notify._cwd", return_value=tmp_path):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                notify_parent(
                    event_type="tdd_cycle_started",
                    extra={"feature": "token refresh", "phase": "red"},
                )

        assert len(posted_data) == 1
        payload = posted_data[0]["payload"]
        assert payload["event"] == "tdd_cycle_started"
        assert payload["feature"] == "token refresh"
        assert payload["phase"] == "red"
