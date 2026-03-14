"""Tests for the __COMPRESS_CONTEXT__ UserPromptSubmit hook behaviour (v1.1.13).

v1.1.13 extends ``user-prompt-submit.py`` to handle the ``__COMPRESS_CONTEXT__``
trigger in addition to ``__TASK__``.  When the ContextMonitor fires auto-compression,
it:

1. Writes compressed pane text to ``__compress_context__<agent_id>__.txt`` in the
   agent's worktree directory.
2. Sends only the short ``__COMPRESS_CONTEXT__`` trigger via send_keys (no
   paste-preview risk).

The UserPromptSubmit hook intercepts the trigger, reads the file, deletes it
(consume-once), wraps it in a clear framing, and returns it as ``additionalContext``
so Claude receives the compressed context summary.

Reference: DESIGN.md §10.38 (v1.1.13 — __COMPRESS_CONTEXT__ UserPromptSubmit hook)
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
    / "scripts"
    / "user-prompt-submit.py"
)


def run_hook(stdin_data: dict) -> subprocess.CompletedProcess:
    """Run the hook script with the given stdin JSON and return the result."""
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
    )


class TestCompressContextHookBasic:
    """Basic __COMPRESS_CONTEXT__ trigger handling."""

    def test_compress_trigger_with_file_returns_additional_context(
        self, tmp_path: Path
    ) -> None:
        """Hook injects __compress_context__*.txt content as additionalContext."""
        compress_file = tmp_path / "__compress_context__worker-1__.txt"
        compress_file.write_text("line 1 of compressed\nline 2 of compressed", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert "hookSpecificOutput" in output
        hook_output = output["hookSpecificOutput"]
        assert hook_output["hookEventName"] == "UserPromptSubmit"
        # additionalContext must contain the compressed text
        assert "line 1 of compressed" in hook_output["additionalContext"]
        assert "line 2 of compressed" in hook_output["additionalContext"]

    def test_compress_trigger_deletes_file_after_reading(self, tmp_path: Path) -> None:
        """Hook deletes __compress_context__*.txt after reading (consume-once)."""
        compress_file = tmp_path / "__compress_context__worker-1__.txt"
        compress_file.write_text("compressed content here", encoding="utf-8")

        run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert not compress_file.exists(), "Compress file must be deleted after reading"

    def test_compress_trigger_no_file_exits_0_passthrough(self, tmp_path: Path) -> None:
        """If no __compress_context__*.txt file found, hook exits 0 with no output."""
        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_compress_trigger_output_is_valid_json(self, tmp_path: Path) -> None:
        """Hook stdout is valid JSON when a compress file is present."""
        compress_file = tmp_path / "__compress_context__w1__.txt"
        compress_file.write_text("Some compressed text", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, dict)


class TestCompressContextHookFraming:
    """Tests that the framing around compressed text is correct."""

    def test_framing_contains_header(self, tmp_path: Path) -> None:
        """additionalContext includes a clear framing header."""
        compress_file = tmp_path / "__compress_context__agent-x__.txt"
        compress_file.write_text("Some important line", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        output = json.loads(result.stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "Context Summary" in additional

    def test_framing_explains_auto_compression(self, tmp_path: Path) -> None:
        """additionalContext tells Claude this is an auto-compressed context."""
        compress_file = tmp_path / "__compress_context__agent-x__.txt"
        compress_file.write_text("Content line", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        output = json.loads(result.stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "TmuxAgentOrchestrator" in additional or "auto-compressed" in additional

    def test_framing_preserves_original_compressed_text(self, tmp_path: Path) -> None:
        """The original compressed text is preserved within the framed output."""
        unique_text = "UNIQUE_MARKER_12345 important context line"
        compress_file = tmp_path / "__compress_context__agent-y__.txt"
        compress_file.write_text(unique_text, encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        output = json.loads(result.stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert unique_text in additional

    def test_framing_mentions_task_unchanged(self, tmp_path: Path) -> None:
        """additionalContext reassures Claude that the original task is unchanged."""
        compress_file = tmp_path / "__compress_context__z__.txt"
        compress_file.write_text("x", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        output = json.loads(result.stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        # Should reassure that original task is unchanged
        assert "task" in additional.lower() or "unchanged" in additional.lower()


class TestCompressContextHookEdgeCases:
    """Edge cases for __COMPRESS_CONTEXT__ handling."""

    def test_unicode_compressed_text_preserved(self, tmp_path: Path) -> None:
        """Unicode content in the compressed file is preserved correctly."""
        unicode_text = "日本語のテキスト 🎉\nCompressed context line"
        compress_file = tmp_path / "__compress_context__worker-jp__.txt"
        compress_file.write_text(unicode_text, encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "日本語のテキスト" in additional

    def test_large_compressed_text_injected(self, tmp_path: Path) -> None:
        """Large compressed text (>10000 chars) is injected correctly."""
        large_text = "\n".join(f"line {i}: some important context" for i in range(300))
        compress_file = tmp_path / "__compress_context__big__.txt"
        compress_file.write_text(large_text, encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "line 0: some important context" in additional
        assert "line 299: some important context" in additional

    def test_compress_trigger_uses_first_file_when_multiple_exist(
        self, tmp_path: Path
    ) -> None:
        """When multiple __compress_context__*.txt files exist, uses first (sorted)."""
        file_a = tmp_path / "__compress_context__agent-a__.txt"
        file_b = tmp_path / "__compress_context__agent-b__.txt"
        file_a.write_text("Context for agent-a", encoding="utf-8")
        file_b.write_text("Context for agent-b", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        # Sorted alphabetically, agent-a comes first
        assert "Context for agent-a" in additional
        # Only the consumed file is deleted
        assert not file_a.exists()
        assert file_b.exists()

    def test_nonmatching_filename_not_read(self, tmp_path: Path) -> None:
        """Files not matching __compress_context__*.txt pattern are ignored."""
        other_file = tmp_path / "compress_context.txt"
        other_file.write_text("Should not be read", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        assert result.stdout.strip() == ""  # no injection
        assert other_file.exists()  # not deleted


class TestTriggerCoexistence:
    """Tests that __TASK__ and __COMPRESS_CONTEXT__ triggers coexist correctly."""

    def test_task_trigger_still_works_with_compress_file_present(
        self, tmp_path: Path
    ) -> None:
        """__TASK__ trigger still reads __task_prompt__*.txt even if compress file exists."""
        task_file = tmp_path / "__task_prompt__worker-1__.txt"
        task_file.write_text("Do the important task", encoding="utf-8")
        compress_file = tmp_path / "__compress_context__worker-1__.txt"
        compress_file.write_text("Compressed stuff", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__TASK__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["additionalContext"] == "Do the important task"
        # Task file consumed, compress file untouched
        assert not task_file.exists()
        assert compress_file.exists()

    def test_compress_trigger_does_not_read_task_file(self, tmp_path: Path) -> None:
        """__COMPRESS_CONTEXT__ trigger reads compress file, not task file."""
        task_file = tmp_path / "__task_prompt__worker-1__.txt"
        task_file.write_text("Task content", encoding="utf-8")
        compress_file = tmp_path / "__compress_context__worker-1__.txt"
        compress_file.write_text("Compressed context", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        # Should get compressed context, not task content
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "Compressed context" in additional
        assert "Task content" not in additional
        # Compress file consumed, task file untouched
        assert not compress_file.exists()
        assert task_file.exists()

    def test_unknown_trigger_exits_0_passthrough(self, tmp_path: Path) -> None:
        """Unknown trigger strings pass through without injection."""
        result = run_hook({"cwd": str(tmp_path), "prompt": "__UNKNOWN_TRIGGER__"})
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_plain_human_prompt_exits_0_passthrough(self, tmp_path: Path) -> None:
        """Plain human-typed prompts pass through without injection."""
        result = run_hook({
            "cwd": str(tmp_path),
            "prompt": "Please write a function that adds two numbers",
        })
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestContextMonitorFileDelivery:
    """Tests for the context_monitor.py file-based delivery changes."""

    def test_compress_file_naming_pattern(self, tmp_path: Path) -> None:
        """The __compress_context__<agent_id>__.txt naming pattern is correct."""
        # Verify the pattern the hook expects matches what context_monitor writes.
        # The hook uses glob: __compress_context__*.txt
        # context_monitor writes: __compress_context__{agent.id}__.txt
        agent_id = "worker-test-1"
        compress_file = tmp_path / f"__compress_context__{agent_id}__.txt"
        compress_file.write_text("test content", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert "test content" in output["hookSpecificOutput"]["additionalContext"]

    def test_compress_file_naming_with_dashes_and_underscores(
        self, tmp_path: Path
    ) -> None:
        """Agent IDs with dashes and underscores in their names are handled."""
        agent_id = "worker-a-1_sub_b2"
        compress_file = tmp_path / f"__compress_context__{agent_id}__.txt"
        compress_file.write_text("compressed output", encoding="utf-8")

        result = run_hook({"cwd": str(tmp_path), "prompt": "__COMPRESS_CONTEXT__"})

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert "compressed output" in output["hookSpecificOutput"]["additionalContext"]
