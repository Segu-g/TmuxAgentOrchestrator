"""Tests for the /spec slash command (v1.1.8).

Verifies that spec.md is present in the agent plugin commands directory,
is well-formed, and implements the Spec-First pattern (SPEC.md generation
with preconditions, postconditions, invariants, and acceptance criteria).

Design references:
- Vasilopoulos arXiv:2602.20478 "Codified Context" (2026): formal specification
  documents help agents maintain consistency across sessions.
- Hou et al. "Trustworthy AI Requires Formal Methods" (2025): preconditions,
  postconditions, and invariants are the canonical contract language.
- DESIGN.md §10.44 (v1.1.8)
"""

from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commands_dir() -> Path:
    return (
        Path(__file__).parent.parent
        / "src"
        / "tmux_orchestrator"
        / "agent_plugin"
        / "commands"
    )


def _spec_file() -> Path:
    return _commands_dir() / "spec.md"


def _read_spec() -> str:
    return _spec_file().read_text()


def _extract_python_snippet(md_content: str) -> str:
    """Extract the first Python code block from a Markdown file."""
    match = re.search(r"```python\n(.*?)```", md_content, re.DOTALL)
    assert match, "No ```python code block found in spec.md"
    return match.group(1)


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------


def test_spec_file_exists() -> None:
    """spec.md must exist in the plugin commands directory."""
    assert _spec_file().exists(), f"spec.md missing: {_spec_file()}"


def test_spec_file_nonempty() -> None:
    """spec.md must have non-empty content."""
    content = _spec_file().read_text().strip()
    assert content, "spec.md is empty"


def test_spec_has_python_snippet() -> None:
    """spec.md must contain a ```python code block."""
    content = _read_spec()
    assert "```python" in content, "spec.md has no Python code block"


def test_spec_has_usage_line() -> None:
    """spec.md must describe its usage syntax."""
    content = _read_spec()
    assert "/spec" in content, "spec.md must mention the /spec command"
    assert "$ARGUMENTS" in content or "<" in content, (
        "spec.md must show argument parameter"
    )


# ---------------------------------------------------------------------------
# Content / contract tests
# ---------------------------------------------------------------------------


def test_spec_references_preconditions() -> None:
    """spec.md must reference Preconditions."""
    content = _read_spec()
    assert "Precondition" in content or "precondition" in content, (
        "spec.md must mention Preconditions"
    )


def test_spec_references_postconditions() -> None:
    """spec.md must reference Postconditions."""
    content = _read_spec()
    assert "Postcondition" in content or "postcondition" in content, (
        "spec.md must mention Postconditions"
    )


def test_spec_references_invariants() -> None:
    """spec.md must reference Invariants."""
    content = _read_spec()
    assert "Invariant" in content or "invariant" in content, (
        "spec.md must mention Invariants"
    )


def test_spec_references_acceptance_criteria() -> None:
    """spec.md must reference Acceptance Criteria."""
    content = _read_spec()
    assert "Acceptance Criteria" in content or "acceptance criteria" in content.lower(), (
        "spec.md must mention Acceptance Criteria"
    )


def test_spec_references_edge_cases() -> None:
    """spec.md must reference Edge Cases."""
    content = _read_spec()
    assert "Edge Case" in content or "edge case" in content.lower(), (
        "spec.md must mention Edge Cases"
    )


def test_spec_references_spec_md_output() -> None:
    """spec.md must reference SPEC.md as the output artifact."""
    content = _read_spec()
    assert "SPEC.md" in content, "spec.md must reference the SPEC.md output file"


def test_spec_references_type_signatures() -> None:
    """spec.md must reference type signatures."""
    content = _read_spec()
    assert "Type" in content or "type" in content, (
        "spec.md must mention type signatures"
    )


def test_spec_python_snippet_reads_arguments() -> None:
    """Python snippet must read the $ARGUMENTS variable."""
    content = _read_spec()
    snippet = _extract_python_snippet(content)
    assert "ARGUMENTS" in snippet, (
        "Python snippet must reference $ARGUMENTS for the description"
    )


def test_spec_python_snippet_has_usage_guard() -> None:
    """Python snippet must guard against empty arguments."""
    content = _read_spec()
    snippet = _extract_python_snippet(content)
    # Should raise SystemExit or print usage when no arguments
    assert "SystemExit" in snippet or "Usage" in snippet, (
        "Python snippet must guard against empty arguments"
    )


def test_spec_has_checklist() -> None:
    """spec.md must include a checklist section."""
    content = _read_spec()
    assert "CHECKLIST" in content or "checklist" in content.lower() or "[ ]" in content, (
        "spec.md must include a checklist"
    )


def test_spec_has_next_steps() -> None:
    """spec.md must include next steps guidance."""
    content = _read_spec()
    assert "NEXT STEPS" in content or "next steps" in content.lower(), (
        "spec.md must include next steps"
    )


def test_spec_present_in_commands_directory() -> None:
    """spec.md must be present among all plugin command files."""
    all_commands = {f.name for f in _commands_dir().glob("*.md")}
    assert "spec.md" in all_commands, (
        "spec.md not found in plugin commands directory"
    )
