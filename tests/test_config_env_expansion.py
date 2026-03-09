"""Tests for YAML environment variable expansion in WebhookConfig.

When loading OrchestratorConfig from YAML, ${ENV_VAR} and $ENV_VAR patterns in
webhook url and secret fields are expanded via os.path.expandvars().

Reference: DESIGN.md §10.N (v1.0.22 — env var expansion in YAML webhook config)
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from tmux_orchestrator.config import load_config, WebhookConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_yaml(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(content))
    return cfg


# ---------------------------------------------------------------------------
# Tests: env var expansion in load_config
# ---------------------------------------------------------------------------


def test_url_env_var_expanded(tmp_path, monkeypatch):
    """${WEBHOOK_URL} in url is expanded to the env var value."""
    monkeypatch.setenv("WEBHOOK_URL", "https://env.example.com/hook")
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "${WEBHOOK_URL}"
            events: ["task_complete"]
    """)
    config = load_config(cfg_file)
    assert len(config.webhooks) == 1
    assert config.webhooks[0].url == "https://env.example.com/hook"


def test_secret_env_var_expanded(tmp_path, monkeypatch):
    """${WEBHOOK_SECRET} in secret is expanded to the env var value."""
    monkeypatch.setenv("WEBHOOK_SECRET", "my-secret-value-123")
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "https://hook.example.com"
            events: ["*"]
            secret: "${WEBHOOK_SECRET}"
    """)
    config = load_config(cfg_file)
    assert config.webhooks[0].secret == "my-secret-value-123"


def test_undefined_var_left_unchanged(tmp_path, monkeypatch):
    """Undefined ${UNDEFINED_VAR} is left unchanged by expandvars."""
    # Ensure var is not set
    monkeypatch.delenv("UNDEFINED_VAR_12345", raising=False)
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "${UNDEFINED_VAR_12345}"
            events: ["task_complete"]
    """)
    config = load_config(cfg_file)
    # os.path.expandvars leaves undefined vars unchanged
    assert config.webhooks[0].url == "${UNDEFINED_VAR_12345}"


def test_no_env_var_left_unchanged(tmp_path):
    """A URL with no ${...} pattern is unchanged."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "https://static.example.com/hook"
            events: ["task_complete"]
    """)
    config = load_config(cfg_file)
    assert config.webhooks[0].url == "https://static.example.com/hook"


def test_no_secret_remains_none(tmp_path):
    """When secret is absent, it remains None (no crash in expandvars)."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "https://no-secret.example.com"
            events: ["task_complete"]
    """)
    config = load_config(cfg_file)
    assert config.webhooks[0].secret is None


def test_dollar_sign_in_url_no_var(tmp_path, monkeypatch):
    """URL with plain $NAME (not curly brace) is also expanded."""
    monkeypatch.setenv("MY_HOST", "myhost.io")
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "https://$MY_HOST/hook"
            events: ["task_complete"]
    """)
    config = load_config(cfg_file)
    # os.path.expandvars also expands $VAR (without braces)
    assert config.webhooks[0].url == "https://myhost.io/hook"


def test_retry_fields_loaded_from_yaml(tmp_path):
    """max_retries and retry_backoff_base are loaded from YAML."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "https://hook.example.com"
            events: ["task_complete"]
            max_retries: 5
            retry_backoff_base: 2.5
    """)
    config = load_config(cfg_file)
    assert config.webhooks[0].max_retries == 5
    assert config.webhooks[0].retry_backoff_base == 2.5


def test_retry_fields_defaults_when_absent(tmp_path):
    """max_retries and retry_backoff_base default to 3 and 1.0."""
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "https://hook.example.com"
            events: ["task_complete"]
    """)
    config = load_config(cfg_file)
    assert config.webhooks[0].max_retries == 3
    assert config.webhooks[0].retry_backoff_base == 1.0


def test_multiple_webhooks_all_expanded(tmp_path, monkeypatch):
    """Multiple webhooks all get env var expansion applied."""
    monkeypatch.setenv("HOST_A", "host-a.io")
    monkeypatch.setenv("HOST_B", "host-b.io")
    cfg_file = write_yaml(tmp_path, """
        session_name: test
        agents: []
        webhooks:
          - url: "https://${HOST_A}/hook"
            events: ["task_complete"]
          - url: "https://${HOST_B}/hook"
            events: ["task_failed"]
    """)
    config = load_config(cfg_file)
    urls = {w.url for w in config.webhooks}
    assert "https://host-a.io/hook" in urls
    assert "https://host-b.io/hook" in urls
