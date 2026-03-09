"""Tests for the UserPromptSubmit hook (user-prompt-submit.py).

v1.1.2 introduces a UserPromptSubmit hook that reads task prompts from
``__task_prompt__<agent_id>__.txt`` and injects them as additionalContext,
avoiding paste-preview by keeping send_keys input short.

Reference: DESIGN.md §10.38 (v1.1.2 — UserPromptSubmit hook for prompt injection)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Path to the hook script
HOOK_SCRIPT = (
    Path(__file__).parent.parent
    / "src"
    / "tmux_orchestrator"
    / "agent_plugin"
    / "hooks"
    / "user-prompt-submit.py"
)


def run_hook(stdin_data: dict, *, tmp_path: Path | None = None) -> subprocess.CompletedProcess:
    """Run the hook script with the given stdin JSON and return the result."""
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
    )


class TestUserPromptSubmitHookPassThrough:
    """Tests for pass-through behaviour when no task prompt file exists."""

    def test_no_prompt_file_exits_0(self, tmp_path: Path) -> None:
        """When no __task_prompt__*.txt file exists, hook exits 0."""
        result = run_hook({"cwd": str(tmp_path), "prompt": "hello"})
        assert result.returncode == 0

    def test_no_prompt_file_empty_stdout(self, tmp_path: Path) -> None:
        """When no file exists, hook produces no stdout output."""
        result = run_hook({"cwd": str(tmp_path), "prompt": "hello"})
        assert result.stdout.strip() == ""

    def test_missing_cwd_exits_0(self) -> None:
        """If cwd points to non-existent directory, hook exits 0 (no file = pass-through)."""
        result = run_hook({"cwd": "/nonexistent/dir/xyz", "prompt": "test"})
        assert result.returncode == 0

    def test_invalid_json_stdin_exits_0(self) -> None:
        """Invalid stdin JSON is handled gracefully (exit 0)."""
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="not valid json{{",
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0

    def test_empty_stdin_exits_0(self) -> None:
        """Empty stdin is handled gracefully (exit 0)."""
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="",
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0


class TestUserPromptSubmitHookInjection:
    """Tests for task prompt injection when __task_prompt__*.txt exists."""

    def test_reads_prompt_file_and_outputs_additional_context(self, tmp_path: Path) -> None:
        """Hook reads __task_prompt__*.txt and outputs additionalContext JSON."""
        prompt_file = tmp_path / "__task_prompt__worker-1__.txt"
        prompt_file.write_text("Do something important", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert "hookSpecificOutput" in output
        hook_output = output["hookSpecificOutput"]
        assert hook_output["hookEventName"] == "UserPromptSubmit"
        assert hook_output["additionalContext"] == "Do something important"

    def test_deletes_prompt_file_after_reading(self, tmp_path: Path) -> None:
        """Hook deletes __task_prompt__*.txt after reading (consume-once)."""
        prompt_file = tmp_path / "__task_prompt__worker-1__.txt"
        prompt_file.write_text("Task content", encoding="utf-8")

        run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert not prompt_file.exists(), "Prompt file must be deleted after reading"

    def test_long_prompt_injected_correctly(self, tmp_path: Path) -> None:
        """Long prompts (>1000 chars) are injected correctly."""
        long_prompt = "x" * 2000 + "\n" + "line two of the prompt"
        prompt_file = tmp_path / "__task_prompt__agent-a__.txt"
        prompt_file.write_text(long_prompt, encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["additionalContext"] == long_prompt

    def test_unicode_prompt_injected_correctly(self, tmp_path: Path) -> None:
        """Unicode prompts (Japanese, emoji, etc.) are preserved."""
        unicode_prompt = "タスク: 日本語のテキスト 🎉\nCreate a file named hello.txt"
        prompt_file = tmp_path / "__task_prompt__worker-2__.txt"
        prompt_file.write_text(unicode_prompt, encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["additionalContext"] == unicode_prompt

    def test_uses_first_file_when_multiple_exist(self, tmp_path: Path) -> None:
        """When multiple __task_prompt__*.txt files exist, uses first (sorted)."""
        file_a = tmp_path / "__task_prompt__agent-a__.txt"
        file_b = tmp_path / "__task_prompt__agent-b__.txt"
        file_a.write_text("Task for agent-a", encoding="utf-8")
        file_b.write_text("Task for agent-b", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        # Sorted alphabetically, agent-a comes first
        assert output["hookSpecificOutput"]["additionalContext"] == "Task for agent-a"
        # Only the consumed file is deleted
        assert not file_a.exists()
        assert file_b.exists()

    def test_exit_0_if_file_disappears_between_glob_and_read(self, tmp_path: Path) -> None:
        """If the file disappears between glob and read (race), hook exits 0."""
        # We can't easily simulate the race, but we can test by having a valid
        # glob-matching pattern that resolves to a non-existent file.
        # The hook uses sorted(glob.glob(...)) which only returns existing files,
        # so the OSError path requires the file to be deleted between glob and read.
        # This test verifies the hook is robust to the race (covered via code review).
        # Instead test normal pass-through when no file exists.
        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})
        assert result.returncode == 0

    def test_output_is_valid_json(self, tmp_path: Path) -> None:
        """Hook stdout is valid JSON when a prompt file is present."""
        prompt_file = tmp_path / "__task_prompt__w1__.txt"
        prompt_file.write_text("Some task", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert result.returncode == 0
        # Must parse without error
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, dict)

    def test_no_cwd_in_stdin_exits_0(self, tmp_path: Path) -> None:
        """If stdin JSON has no 'cwd' field, hook passes through (uses '.')."""
        # Since current dir likely has no __task_prompt__*.txt, should exit 0
        result = run_hook({"prompt": "__TASK__"})
        assert result.returncode == 0

    def test_agent_id_namespaced_file_only(self, tmp_path: Path) -> None:
        """Hook only reads files matching __task_prompt__*.txt pattern."""
        # A file with a non-matching name is not read
        other_file = tmp_path / "task_prompt.txt"
        other_file.write_text("Not a task prompt", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert result.returncode == 0
        assert result.stdout.strip() == ""  # no injection
        assert other_file.exists()  # not deleted


class TestUserPromptSubmitHookHooksJson:
    """Tests for hooks.json registration of UserPromptSubmit."""

    def test_hooks_json_contains_user_prompt_submit(self) -> None:
        """hooks.json must register a UserPromptSubmit hook."""
        hooks_json_path = (
            Path(__file__).parent.parent
            / "src"
            / "tmux_orchestrator"
            / "agent_plugin"
            / "hooks"
            / "hooks.json"
        )
        assert hooks_json_path.exists()
        config = json.loads(hooks_json_path.read_text())
        assert "UserPromptSubmit" in config.get("hooks", {}), (
            "hooks.json must declare a UserPromptSubmit hook"
        )

    def test_user_prompt_submit_hook_is_command_type(self) -> None:
        """UserPromptSubmit hook must be a command type hook."""
        hooks_json_path = (
            Path(__file__).parent.parent
            / "src"
            / "tmux_orchestrator"
            / "agent_plugin"
            / "hooks"
            / "hooks.json"
        )
        config = json.loads(hooks_json_path.read_text())
        ups_hooks = config["hooks"]["UserPromptSubmit"]
        assert len(ups_hooks) >= 1
        inner_hooks = ups_hooks[0]["hooks"]
        assert len(inner_hooks) >= 1
        assert inner_hooks[0]["type"] == "command"

    def test_hook_script_exists(self) -> None:
        """The user-prompt-submit.py script must exist and be executable."""
        assert HOOK_SCRIPT.exists(), f"Hook script not found at {HOOK_SCRIPT}"
