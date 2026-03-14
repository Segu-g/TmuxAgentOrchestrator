"""Tests for TDD role-specific docs: tester.md, coder.md, reviewer.md (v1.2.22).

Covers:
- tester.md, coder.md, reviewer.md exist in agent_plugin/docs/
- Each file is non-empty and contains role-relevant keywords
- tdd.yaml exists in examples/workflows/
- tdd.yaml contains role field or role-related annotation
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOCS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "tmux_orchestrator"
    / "agent_plugin"
    / "docs"
)

WORKFLOWS_DIR = Path(__file__).parent.parent / "examples" / "workflows"


# ---------------------------------------------------------------------------
# 1. Role doc files exist
# ---------------------------------------------------------------------------


class TestRoleDocFilesExist:
    def test_tester_md_exists(self) -> None:
        """tester.md must exist in agent_plugin/docs/."""
        assert (DOCS_DIR / "tester.md").exists(), (
            f"agent_plugin/docs/tester.md missing (looked in {DOCS_DIR})"
        )

    def test_coder_md_exists(self) -> None:
        """coder.md must exist in agent_plugin/docs/."""
        assert (DOCS_DIR / "coder.md").exists(), (
            f"agent_plugin/docs/coder.md missing (looked in {DOCS_DIR})"
        )

    def test_reviewer_md_exists(self) -> None:
        """reviewer.md must exist in agent_plugin/docs/."""
        assert (DOCS_DIR / "reviewer.md").exists(), (
            f"agent_plugin/docs/reviewer.md missing (looked in {DOCS_DIR})"
        )


# ---------------------------------------------------------------------------
# 2. Role doc files are non-empty
# ---------------------------------------------------------------------------


class TestRoleDocFilesNonEmpty:
    def test_tester_md_is_non_empty(self) -> None:
        content = (DOCS_DIR / "tester.md").read_text()
        assert len(content.strip()) > 0, "tester.md must not be empty"

    def test_coder_md_is_non_empty(self) -> None:
        content = (DOCS_DIR / "coder.md").read_text()
        assert len(content.strip()) > 0, "coder.md must not be empty"

    def test_reviewer_md_is_non_empty(self) -> None:
        content = (DOCS_DIR / "reviewer.md").read_text()
        assert len(content.strip()) > 0, "reviewer.md must not be empty"


# ---------------------------------------------------------------------------
# 3. Role doc files contain role-relevant keywords
# ---------------------------------------------------------------------------


class TestTesterMdContent:
    @pytest.fixture(scope="class")
    def content(self) -> str:
        return (DOCS_DIR / "tester.md").read_text()

    def test_mentions_red_phase(self, content: str) -> None:
        assert "Red" in content or "red" in content, (
            "tester.md must mention Red phase"
        )

    def test_mentions_failing_test(self, content: str) -> None:
        assert "failing test" in content.lower() or "fail" in content.lower(), (
            "tester.md must mention failing tests"
        )

    def test_mentions_task_complete(self, content: str) -> None:
        assert "/task-complete" in content, (
            "tester.md must reference /task-complete"
        )

    def test_mentions_one_test_at_a_time(self, content: str) -> None:
        # Check for the concept of writing one test at a time
        lower = content.lower()
        assert "one test" in lower or "one failing" in lower or "one at a time" in lower, (
            "tester.md must emphasise writing one test at a time"
        )

    def test_mentions_handoff_protocol(self, content: str) -> None:
        lower = content.lower()
        assert "handoff" in lower or "commit" in lower, (
            "tester.md must mention handoff/commit protocol"
        )

    def test_mentions_context_management(self, content: str) -> None:
        lower = content.lower()
        assert "context" in lower, "tester.md must mention context management"

    def test_under_150_lines(self, content: str) -> None:
        lines = content.splitlines()
        assert len(lines) < 150, f"tester.md too long: {len(lines)} lines"


class TestCoderMdContent:
    @pytest.fixture(scope="class")
    def content(self) -> str:
        return (DOCS_DIR / "coder.md").read_text()

    def test_mentions_green_phase(self, content: str) -> None:
        assert "Green" in content or "green" in content, (
            "coder.md must mention Green phase"
        )

    def test_mentions_minimal_code(self, content: str) -> None:
        lower = content.lower()
        assert "minimal" in lower or "minimum" in lower, (
            "coder.md must mention minimal code requirement"
        )

    def test_mentions_yagni(self, content: str) -> None:
        assert "YAGNI" in content or "You Aren't Gonna Need It" in content, (
            "coder.md must mention YAGNI principle"
        )

    def test_mentions_task_complete(self, content: str) -> None:
        assert "/task-complete" in content, (
            "coder.md must reference /task-complete"
        )

    def test_mentions_no_refactoring(self, content: str) -> None:
        lower = content.lower()
        assert "refactor" in lower, (
            "coder.md must mention not refactoring in this phase"
        )

    def test_mentions_tests_pass(self, content: str) -> None:
        lower = content.lower()
        assert "pass" in lower, "coder.md must mention making tests pass"

    def test_under_150_lines(self, content: str) -> None:
        lines = content.splitlines()
        assert len(lines) < 150, f"coder.md too long: {len(lines)} lines"


class TestReviewerMdContent:
    @pytest.fixture(scope="class")
    def content(self) -> str:
        return (DOCS_DIR / "reviewer.md").read_text()

    def test_mentions_refactor(self, content: str) -> None:
        lower = content.lower()
        assert "refactor" in lower, "reviewer.md must mention refactoring"

    def test_mentions_no_behaviour_change(self, content: str) -> None:
        lower = content.lower()
        assert "behaviour" in lower or "behavior" in lower, (
            "reviewer.md must mention not changing behaviour"
        )

    def test_mentions_review_md(self, content: str) -> None:
        assert "REVIEW.md" in content, (
            "reviewer.md must reference REVIEW.md output file"
        )

    def test_mentions_task_complete(self, content: str) -> None:
        assert "/task-complete" in content, (
            "reviewer.md must reference /task-complete"
        )

    def test_mentions_severity_levels(self, content: str) -> None:
        assert "CRITICAL" in content or "HIGH" in content, (
            "reviewer.md must mention severity levels (CRITICAL/HIGH)"
        )

    def test_mentions_no_new_features(self, content: str) -> None:
        lower = content.lower()
        assert "feature" in lower or "new feature" in lower, (
            "reviewer.md must mention not adding new features"
        )

    def test_mentions_tests_green(self, content: str) -> None:
        lower = content.lower()
        assert "green" in lower or "pass" in lower, (
            "reviewer.md must mention keeping tests green/passing"
        )

    def test_under_150_lines(self, content: str) -> None:
        lines = content.splitlines()
        assert len(lines) < 150, f"reviewer.md too long: {len(lines)} lines"


# ---------------------------------------------------------------------------
# 4. tdd.yaml exists and contains role annotation
# ---------------------------------------------------------------------------


class TestTddYaml:
    @pytest.fixture(scope="class")
    def tdd_yaml_path(self) -> Path:
        return WORKFLOWS_DIR / "tdd.yaml"

    @pytest.fixture(scope="class")
    def content(self, tdd_yaml_path: Path) -> str:
        return tdd_yaml_path.read_text()

    def test_tdd_yaml_exists(self, tdd_yaml_path: Path) -> None:
        assert tdd_yaml_path.exists(), (
            f"examples/workflows/tdd.yaml missing (looked in {WORKFLOWS_DIR})"
        )

    def test_tdd_yaml_contains_role_annotation(self, content: str) -> None:
        """tdd.yaml must mention role field or role-related comment."""
        lower = content.lower()
        assert "role:" in lower or "role" in lower, (
            "tdd.yaml must contain role field or role annotation comment"
        )

    def test_tdd_yaml_mentions_tester_role(self, content: str) -> None:
        assert "tester" in content, (
            "tdd.yaml must mention tester role"
        )

    def test_tdd_yaml_mentions_coder_role(self, content: str) -> None:
        assert "coder" in content, (
            "tdd.yaml must mention coder role"
        )

    def test_tdd_yaml_mentions_reviewer_role(self, content: str) -> None:
        assert "reviewer" in content, (
            "tdd.yaml must mention reviewer role"
        )

    def test_tdd_yaml_mentions_agent_plugin_docs(self, content: str) -> None:
        assert "agent_plugin/docs" in content, (
            "tdd.yaml must reference agent_plugin/docs"
        )


# ---------------------------------------------------------------------------
# 5. AgentRole enum includes TDD specialist roles
# ---------------------------------------------------------------------------


class TestAgentRoleEnum:
    def test_tester_role_exists(self) -> None:
        from tmux_orchestrator.domain.agent import AgentRole
        assert AgentRole.TESTER == "tester"

    def test_coder_role_exists(self) -> None:
        from tmux_orchestrator.domain.agent import AgentRole
        assert AgentRole.CODER == "coder"

    def test_reviewer_role_exists(self) -> None:
        from tmux_orchestrator.domain.agent import AgentRole
        assert AgentRole.REVIEWER == "reviewer"

    def test_tdd_roles_are_dispatchable(self) -> None:
        """TDD specialist roles must be in _DISPATCHABLE_ROLES so they receive tasks."""
        from tmux_orchestrator.application.registry import _DISPATCHABLE_ROLES
        from tmux_orchestrator.domain.agent import AgentRole
        assert AgentRole.TESTER in _DISPATCHABLE_ROLES
        assert AgentRole.CODER in _DISPATCHABLE_ROLES
        assert AgentRole.REVIEWER in _DISPATCHABLE_ROLES

    def test_director_not_dispatchable(self) -> None:
        """DIRECTOR must NOT be in _DISPATCHABLE_ROLES (directors coordinate, not execute)."""
        from tmux_orchestrator.application.registry import _DISPATCHABLE_ROLES
        from tmux_orchestrator.domain.agent import AgentRole
        assert AgentRole.DIRECTOR not in _DISPATCHABLE_ROLES

    def test_worker_still_dispatchable(self) -> None:
        """WORKER must remain in _DISPATCHABLE_ROLES for backward compatibility."""
        from tmux_orchestrator.application.registry import _DISPATCHABLE_ROLES
        from tmux_orchestrator.domain.agent import AgentRole
        assert AgentRole.WORKER in _DISPATCHABLE_ROLES


# ---------------------------------------------------------------------------
# 6. All four role docs coexist with pre-existing worker/director docs
# ---------------------------------------------------------------------------


class TestAllRoleDocsCoexist:
    def test_worker_md_still_exists(self) -> None:
        assert (DOCS_DIR / "worker.md").exists(), "worker.md must still exist"

    def test_director_md_still_exists(self) -> None:
        assert (DOCS_DIR / "director.md").exists(), "director.md must still exist"

    def test_five_docs_total(self) -> None:
        """docs/ should contain exactly worker, director, tester, coder, reviewer."""
        md_files = {p.name for p in DOCS_DIR.glob("*.md")}
        expected = {"worker.md", "director.md", "tester.md", "coder.md", "reviewer.md"}
        assert expected.issubset(md_files), (
            f"Missing docs: {expected - md_files}. Found: {md_files}"
        )
