"""Tests for examples/workflows/ YAML template library.

Verifies that every YAML template in examples/workflows/ can be:
  1. Loaded and parsed as valid YAML.
  2. Validated against the corresponding Pydantic schema.
  3. Contains the required fields for the endpoint.

The ``workflow.endpoint`` metadata key is stripped before schema validation.

Design references:
- CrewAI YAML-driven workflow approach (2025): declarative YAML decouples
  configuration from code; templates should be self-documenting.
- Microsoft Agent Framework Declarative Workflows (2025): YAML enables
  version-controlled, CI/CD-integrated workflow definitions.
- Pydantic YAML validation guide (betterprogramming.pub 2025): validate YAML
  configs against Pydantic models; catch errors early in the pipeline.
- DESIGN.md §10.48 (v1.1.16)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tmux_orchestrator.web.schemas import (
    AdrWorkflowSubmit,
    CleanArchWorkflowSubmit,
    CompetitionWorkflowSubmit,
    DDDWorkflowSubmit,
    DebateWorkflowSubmit,
    DelphiWorkflowSubmit,
    PairWorkflowSubmit,
    RedBlueWorkflowSubmit,
    SocraticWorkflowSubmit,
    SpecFirstWorkflowSubmit,
    TddWorkflowSubmit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).parent.parent / "examples" / "workflows"

TEMPLATE_SCHEMAS: dict[str, type] = {
    "tdd.yaml": TddWorkflowSubmit,
    "pair.yaml": PairWorkflowSubmit,
    "debate.yaml": DebateWorkflowSubmit,
    "adr.yaml": AdrWorkflowSubmit,
    "delphi.yaml": DelphiWorkflowSubmit,
    "redblue.yaml": RedBlueWorkflowSubmit,
    "socratic.yaml": SocraticWorkflowSubmit,
    "spec-first.yaml": SpecFirstWorkflowSubmit,
    "clean-arch.yaml": CleanArchWorkflowSubmit,
    "ddd.yaml": DDDWorkflowSubmit,
    "competition.yaml": CompetitionWorkflowSubmit,
}


def load_template(filename: str) -> dict:
    """Load a YAML template file and strip the ``workflow`` metadata key."""
    path = TEMPLATES_DIR / filename
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # Strip the informational ``workflow`` key before schema validation.
    data.pop("workflow", None)
    return data


# ---------------------------------------------------------------------------
# Test: all expected template files exist
# ---------------------------------------------------------------------------


class TestTemplateFilesExist:
    """All template YAML files must be present in examples/workflows/."""

    def test_templates_directory_exists(self):
        assert TEMPLATES_DIR.is_dir(), f"{TEMPLATES_DIR} does not exist"

    @pytest.mark.parametrize("filename", list(TEMPLATE_SCHEMAS.keys()))
    def test_template_file_exists(self, filename: str):
        path = TEMPLATES_DIR / filename
        assert path.exists(), f"Template file missing: {path}"

    def test_readme_exists(self):
        readme = TEMPLATES_DIR / "README.md"
        assert readme.exists(), "README.md missing from examples/workflows/"


# ---------------------------------------------------------------------------
# Test: YAML is parseable
# ---------------------------------------------------------------------------


class TestTemplatesAreValidYaml:
    """Every template file must be parseable as valid YAML."""

    @pytest.mark.parametrize("filename", list(TEMPLATE_SCHEMAS.keys()))
    def test_yaml_parseable(self, filename: str):
        path = TEMPLATES_DIR / filename
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict), f"{filename} must parse to a dict"

    @pytest.mark.parametrize("filename", list(TEMPLATE_SCHEMAS.keys()))
    def test_workflow_metadata_key_present(self, filename: str):
        """Each template should have an informational 'workflow' key."""
        path = TEMPLATES_DIR / filename
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "workflow" in data, f"{filename} missing 'workflow' metadata key"

    @pytest.mark.parametrize("filename", list(TEMPLATE_SCHEMAS.keys()))
    def test_workflow_metadata_has_endpoint(self, filename: str):
        """The 'workflow' key must contain an 'endpoint' sub-key."""
        path = TEMPLATES_DIR / filename
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "endpoint" in data.get("workflow", {}), (
            f"{filename} workflow.endpoint missing"
        )


# ---------------------------------------------------------------------------
# Test: Pydantic schema validation
# ---------------------------------------------------------------------------


class TestTemplateSchemaValidation:
    """Every template must pass Pydantic schema validation after stripping metadata."""

    @pytest.mark.parametrize("filename,schema", list(TEMPLATE_SCHEMAS.items()))
    def test_schema_validates(self, filename: str, schema: type):
        data = load_template(filename)
        # Should not raise ValidationError
        instance = schema.model_validate(data)
        assert instance is not None

    def test_tdd_feature_field(self):
        data = load_template("tdd.yaml")
        instance = TddWorkflowSubmit.model_validate(data)
        assert instance.feature, "tdd.yaml: feature must not be empty"

    def test_pair_task_field(self):
        data = load_template("pair.yaml")
        instance = PairWorkflowSubmit.model_validate(data)
        assert instance.task, "pair.yaml: task must not be empty"

    def test_debate_topic_and_rounds(self):
        data = load_template("debate.yaml")
        instance = DebateWorkflowSubmit.model_validate(data)
        assert instance.topic, "debate.yaml: topic must not be empty"
        assert 1 <= instance.max_rounds <= 3, "debate.yaml: max_rounds must be 1-3"

    def test_adr_topic_field(self):
        data = load_template("adr.yaml")
        instance = AdrWorkflowSubmit.model_validate(data)
        assert instance.topic, "adr.yaml: topic must not be empty"

    def test_delphi_experts_count(self):
        data = load_template("delphi.yaml")
        instance = DelphiWorkflowSubmit.model_validate(data)
        assert instance.topic, "delphi.yaml: topic must not be empty"
        assert 2 <= len(instance.experts) <= 5, (
            "delphi.yaml: experts must be between 2 and 5"
        )

    def test_redblue_topic_field(self):
        data = load_template("redblue.yaml")
        instance = RedBlueWorkflowSubmit.model_validate(data)
        assert instance.topic, "redblue.yaml: topic must not be empty"

    def test_socratic_topic_field(self):
        data = load_template("socratic.yaml")
        instance = SocraticWorkflowSubmit.model_validate(data)
        assert instance.topic, "socratic.yaml: topic must not be empty"

    def test_spec_first_requires_topic_and_requirements(self):
        data = load_template("spec-first.yaml")
        instance = SpecFirstWorkflowSubmit.model_validate(data)
        assert instance.topic, "spec-first.yaml: topic must not be empty"
        assert instance.requirements, (
            "spec-first.yaml: requirements must not be empty"
        )

    def test_clean_arch_feature_field(self):
        data = load_template("clean-arch.yaml")
        instance = CleanArchWorkflowSubmit.model_validate(data)
        assert instance.feature, "clean-arch.yaml: feature must not be empty"

    def test_ddd_topic_field(self):
        data = load_template("ddd.yaml")
        instance = DDDWorkflowSubmit.model_validate(data)
        assert instance.topic, "ddd.yaml: topic must not be empty"

    def test_competition_strategies_count(self):
        data = load_template("competition.yaml")
        instance = CompetitionWorkflowSubmit.model_validate(data)
        assert instance.problem, "competition.yaml: problem must not be empty"
        assert 2 <= len(instance.strategies) <= 10, (
            "competition.yaml: strategies must be between 2 and 10"
        )


# ---------------------------------------------------------------------------
# Test: optional tag fields have list defaults
# ---------------------------------------------------------------------------


class TestOptionalTagDefaults:
    """Optional *_tags fields must default to empty lists."""

    def test_tdd_tags_default_to_empty_lists(self):
        data = load_template("tdd.yaml")
        instance = TddWorkflowSubmit.model_validate(data)
        assert instance.test_writer_tags == []
        assert instance.implementer_tags == []
        assert instance.refactorer_tags == []

    def test_pair_tags_default_to_empty_lists(self):
        data = load_template("pair.yaml")
        instance = PairWorkflowSubmit.model_validate(data)
        assert instance.navigator_tags == []
        assert instance.driver_tags == []

    def test_debate_tags_default_to_empty_lists(self):
        data = load_template("debate.yaml")
        instance = DebateWorkflowSubmit.model_validate(data)
        assert instance.advocate_tags == []
        assert instance.critic_tags == []
        assert instance.judge_tags == []

    def test_competition_tags_default_to_empty_lists(self):
        data = load_template("competition.yaml")
        instance = CompetitionWorkflowSubmit.model_validate(data)
        assert instance.solver_tags == []
        assert instance.judge_tags == []

    def test_delphi_tags_default_to_empty_lists(self):
        data = load_template("delphi.yaml")
        instance = DelphiWorkflowSubmit.model_validate(data)
        assert instance.expert_tags == []
        assert instance.moderator_tags == []

    def test_reply_to_defaults_to_none(self):
        """All templates must have reply_to: null by default."""
        for filename, schema in TEMPLATE_SCHEMAS.items():
            data = load_template(filename)
            instance = schema.model_validate(data)
            assert instance.reply_to is None, (
                f"{filename}: reply_to must default to None"
            )


# ---------------------------------------------------------------------------
# Test: invalid template data raises ValidationError
# ---------------------------------------------------------------------------


class TestSchemaRejectionOfInvalidData:
    """Schema validation must reject obviously bad data."""

    def test_tdd_rejects_empty_feature(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TddWorkflowSubmit.model_validate({"feature": ""})

    def test_debate_rejects_empty_topic(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DebateWorkflowSubmit.model_validate({"topic": ""})

    def test_debate_rejects_invalid_max_rounds(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DebateWorkflowSubmit.model_validate({"topic": "test", "max_rounds": 5})

    def test_competition_rejects_single_strategy(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CompetitionWorkflowSubmit.model_validate(
                {"problem": "test", "strategies": ["only-one"]}
            )

    def test_delphi_rejects_single_expert(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DelphiWorkflowSubmit.model_validate(
                {"topic": "test", "experts": ["solo"]}
            )

    def test_spec_first_rejects_missing_requirements(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SpecFirstWorkflowSubmit.model_validate(
                {"topic": "test", "requirements": ""}
            )


# ---------------------------------------------------------------------------
# Test: template endpoint metadata consistency
# ---------------------------------------------------------------------------


class TestEndpointMetadataConsistency:
    """The workflow.endpoint key must match the expected endpoint for each template."""

    EXPECTED_ENDPOINTS = {
        "tdd.yaml": "/workflows/tdd",
        "pair.yaml": "/workflows/pair",
        "debate.yaml": "/workflows/debate",
        "adr.yaml": "/workflows/adr",
        "delphi.yaml": "/workflows/delphi",
        "redblue.yaml": "/workflows/redblue",
        "socratic.yaml": "/workflows/socratic",
        "spec-first.yaml": "/workflows/spec-first",
        "clean-arch.yaml": "/workflows/clean-arch",
        "ddd.yaml": "/workflows/ddd",
        "competition.yaml": "/workflows/competition",
    }

    @pytest.mark.parametrize(
        "filename,expected_endpoint", list(EXPECTED_ENDPOINTS.items())
    )
    def test_endpoint_metadata_matches(
        self, filename: str, expected_endpoint: str
    ):
        path = TEMPLATES_DIR / filename
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        actual = data.get("workflow", {}).get("endpoint", "")
        assert actual == expected_endpoint, (
            f"{filename}: expected endpoint {expected_endpoint!r}, got {actual!r}"
        )
