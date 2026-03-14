#!/usr/bin/env bash
# Signal orchestrator that this agent's session has started.
# Env vars set by ClaudeCodeAgent._set_session_env_vars() via tmux set-environment.
if [[ -z "${TMUX_ORCHESTRATOR_WEB_BASE_URL:-}" ]] || [[ -z "${TMUX_ORCHESTRATOR_AGENT_ID:-}" ]]; then
    exit 0
fi
curl -sf -X POST "${TMUX_ORCHESTRATOR_WEB_BASE_URL}/agents/${TMUX_ORCHESTRATOR_AGENT_ID}/ready" \
    --max-time 10 || true
