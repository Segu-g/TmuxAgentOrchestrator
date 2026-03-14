"""YAML-driven workflow template loader — v1.2.28.

Loads phase-based workflow templates from YAML files and renders them into
``WorkflowSubmit``-compatible dicts by substituting ``{variable}`` placeholders
using Python's ``str.format_map()``.

This enables new workflows to be added via YAML alone without any Python code
changes — satisfying the user's requirement for maximum generality.

Design references:
- Argo Workflows parameters: ``{{inputs.parameters.message}}`` substitution
  https://argo-workflows.readthedocs.io/en/latest/walk-through/parameters/ (2025)
- Azure Pipelines template variable substitution: ``${{ parameters.x }}``
  https://learn.microsoft.com/en-us/azure/devops/pipelines/process/templates (2025)
- Python str.format_map(): safe, stdlib, supports custom mappings for
  missing-key detection. Faster than Jinja2 for simple ``{var}`` substitution.
  Jinja2 is heavier and requires an extra dependency; format_map is sufficient
  for single-level variable injection in prompt strings.
- DESIGN.md §10.103 (v1.2.28)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import]
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class VariableSpec:
    """Metadata about a single declared template variable.

    Attributes
    ----------
    description:
        Human-readable explanation of what this variable controls.
    required:
        When ``True``, callers must supply this variable or
        :func:`render_template` raises :exc:`ValueError`.
    default:
        Fallback value used when the variable is absent from the caller's
        ``variables`` dict and ``required`` is ``False``.
    """

    description: str = ""
    required: bool = True
    default: str = ""


@dataclass
class WorkflowTemplate:
    """Loaded and parsed workflow template.

    Attributes
    ----------
    name:
        Raw template name string (may contain ``{variable}`` placeholders).
    description:
        Human-readable description of the workflow.
    phases:
        List of phase dicts (each is a ``PhaseSpecModel``-compatible dict).
    defaults:
        Per-phase default values (applied as ``phase_defaults`` in the
        rendered ``WorkflowSubmit``).
    variables:
        Declared variables with metadata (see :class:`VariableSpec`).
    context:
        Optional top-level context string for ``WorkflowSubmit.context``.
    """

    name: str = "workflow"
    description: str = ""
    phases: list[dict[str, Any]] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, VariableSpec] = field(default_factory=dict)
    context: str = ""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class _MissingKeyMap(dict):
    """A dict subclass that raises ValueError for missing keys.

    Used with ``str.format_map()`` so that an unresolved ``{variable}`` in
    a template prompt immediately surfaces as a clear error, rather than
    silently passing through as a literal ``{variable}`` string.

    References
    ----------
    - Python docs: ``str.format_map`` with custom mapping.
    """

    def __missing__(self, key: str) -> str:
        raise ValueError(f"Template variable '{{{key}}}' was not provided")


def load_workflow_template(
    template_name: str,
    templates_dir: Path,
) -> WorkflowTemplate:
    """Load a phase-based YAML workflow template by name.

    Searches *templates_dir* (and its ``generic/`` subdirectory) for a file
    named ``{template_name}.yaml``.  The YAML must have the structure::

        name: "Workflow Name (may include {variable} placeholders)"
        description: "..."
        variables:
          var_name:
            description: "..."
            required: true        # default: true
            default: ""           # used when required: false
        defaults:
          timeout: 300            # applied to every phase as phase_defaults
          pattern: "single"
        phases:
          - name: "phase-1"
            pattern: "single"
            context: "Prompt text with {variable} placeholders."
          - name: "phase-2"
            pattern: "single"
            depends_on: ["phase-1"]
            context: "Another prompt referencing {variable}."

    The ``variables:`` section is optional.  When present, it documents the
    expected substitution keys and marks which are required.

    Parameters
    ----------
    template_name:
        Template identifier without the ``.yaml`` extension
        (e.g. ``"tdd"``, ``"debate"``).
    templates_dir:
        Directory to search first.  The ``generic/`` subdirectory is checked
        as a fallback.

    Returns
    -------
    WorkflowTemplate
        Parsed template ready for variable rendering.

    Raises
    ------
    FileNotFoundError
        When no matching YAML file is found.
    ImportError
        When PyYAML is not installed.
    ValueError
        When the YAML is structurally invalid (missing ``phases`` key).
    """
    if yaml is None:  # pragma: no cover
        raise ImportError("PyYAML is required: uv add pyyaml")

    # Sanitise the template name to prevent path traversal.
    safe_name = Path(template_name).name  # strips any directory component
    # Search order: generic/ first (phase-based templates), then root-level.
    # Root-level files are often old endpoint-parameter format (no 'phases' key);
    # phase-based templates that work with POST /workflows/from-template live in
    # generic/.  If both exist, generic/ takes priority for forward compatibility.
    candidates = [
        templates_dir / "generic" / f"{safe_name}.yaml",
        templates_dir / f"{safe_name}.yaml",
    ]
    path: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            path = candidate
            break
    if path is None:
        searched = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(
            f"Workflow template '{template_name}' not found. "
            f"Searched: {searched}"
        )

    with open(path, encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ValueError(
            f"Template '{template_name}' is not a valid YAML mapping."
        )

    # --- Parse variables section ---
    variables: dict[str, VariableSpec] = {}
    raw_vars = raw.get("variables") or {}
    if isinstance(raw_vars, dict):
        for var_name, var_spec in raw_vars.items():
            if isinstance(var_spec, dict):
                variables[var_name] = VariableSpec(
                    description=str(var_spec.get("description", "")),
                    required=bool(var_spec.get("required", True)),
                    default=str(var_spec.get("default", "")),
                )
            else:
                # Shorthand: "varname: description string"
                variables[var_name] = VariableSpec(
                    description=str(var_spec or ""),
                    required=True,
                    default="",
                )

    # --- Parse phases ---
    raw_phases = raw.get("phases")
    if raw_phases is None:
        raise ValueError(
            f"Template '{template_name}' is missing a 'phases' key. "
            "Phase-based templates must define a 'phases' list."
        )
    if not isinstance(raw_phases, list):
        raise ValueError(
            f"Template '{template_name}': 'phases' must be a list."
        )

    # --- Parse defaults (phase-level defaults) ---
    defaults: dict[str, Any] = {}
    raw_defaults = raw.get("defaults")
    if isinstance(raw_defaults, dict):
        defaults = raw_defaults

    return WorkflowTemplate(
        name=str(raw.get("name", template_name)),
        description=str(raw.get("description", "")),
        phases=[dict(p) for p in raw_phases if isinstance(p, dict)],
        defaults=defaults,
        variables=variables,
        context=str(raw.get("context", "")),
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_template(
    template: WorkflowTemplate,
    variables: dict[str, str],
    *,
    agent_timeout: int | None = None,
    priority: int = 0,
    reply_to: str | None = None,
) -> dict[str, Any]:
    """Render a :class:`WorkflowTemplate` into a ``WorkflowSubmit``-compatible dict.

    Variable substitution uses ``str.format_map()`` on all string values
    in the template (name, description, context, and each phase's ``name``,
    ``context``, and ``required_tags``).

    The substitution mapping is built by:
    1. Collecting defaults from :attr:`WorkflowTemplate.variables` (for non-required
       variables with a ``default`` value).
    2. Overlaying the caller-supplied *variables* dict (caller values win).

    Parameters
    ----------
    template:
        Parsed workflow template (output of :func:`load_workflow_template`).
    variables:
        Caller-supplied variable values.  Must include all required variables
        declared in ``template.variables``.
    agent_timeout:
        Optional per-phase timeout override in seconds.  When provided,
        overrides ``defaults.timeout`` from the template.
    priority:
        Task priority for all phases (default ``0``).
    reply_to:
        Agent ID that receives the RESULT of the last phase in its mailbox.
        Attached to the last phase spec.

    Returns
    -------
    dict
        A dict suitable for ``WorkflowSubmit.model_validate()``, containing:
        - ``name``: rendered workflow name
        - ``phases``: list of rendered ``PhaseSpecModel``-compatible dicts
        - ``phase_defaults``: rendered defaults dict
        - ``context``: rendered top-level context (may be empty string)

    Raises
    ------
    ValueError
        When a required variable is not provided in *variables*, or when a
        ``{variable}`` placeholder in the template is not declared and not
        provided in *variables*.
    """
    # Build the substitution map: declared defaults first, then caller values.
    sub_map: dict[str, str] = {}
    for var_name, spec in template.variables.items():
        if not spec.required and spec.default:
            sub_map[var_name] = spec.default

    # Validate required variables and overlay caller values.
    missing_required: list[str] = []
    for var_name, spec in template.variables.items():
        if var_name in variables:
            sub_map[var_name] = str(variables[var_name])
        elif spec.required and var_name not in sub_map:
            missing_required.append(var_name)

    # Also accept undeclared variables from the caller (ad-hoc substitution).
    for var_name, value in variables.items():
        if var_name not in sub_map:
            sub_map[var_name] = str(value)

    if missing_required:
        raise ValueError(
            f"Required template variables not provided: {missing_required}. "
            f"Declared variables: {list(template.variables.keys())}"
        )

    mapping = _MissingKeyMap(sub_map)

    def _render_str(s: str) -> str:
        """Substitute {variables} in a string; raise ValueError on unknown keys."""
        try:
            return s.format_map(mapping)
        except KeyError as exc:
            raise ValueError(
                f"Template contains unknown variable {exc} "
                "that was not provided in 'variables'."
            ) from exc

    # --- Render name and context ---
    rendered_name = _render_str(template.name)
    rendered_context = _render_str(template.context) if template.context else ""

    # --- Render defaults ---
    rendered_defaults: dict[str, Any] = {}
    for key, val in template.defaults.items():
        if isinstance(val, str):
            rendered_defaults[key] = _render_str(val)
        else:
            rendered_defaults[key] = val

    # Apply agent_timeout override.
    if agent_timeout is not None:
        rendered_defaults["timeout"] = agent_timeout

    # --- Render phases ---
    rendered_phases: list[dict[str, Any]] = []
    phases = copy.deepcopy(template.phases)
    for i, phase in enumerate(phases):
        rendered_phase: dict[str, Any] = {}
        for key, val in phase.items():
            if isinstance(val, str):
                rendered_phase[key] = _render_str(val)
            elif isinstance(val, list):
                rendered_phase[key] = [
                    _render_str(item) if isinstance(item, str) else item
                    for item in val
                ]
            else:
                rendered_phase[key] = val

        # Apply priority to each phase.
        if priority != 0:
            rendered_phase.setdefault("priority", priority)

        # Attach reply_to to the last phase.
        if reply_to and i == len(phases) - 1:
            rendered_phase["reply_to"] = reply_to

        rendered_phases.append(rendered_phase)

    result: dict[str, Any] = {
        "name": rendered_name,
        "phases": rendered_phases,
    }
    if rendered_context:
        result["context"] = rendered_context
    if rendered_defaults:
        result["phase_defaults"] = rendered_defaults

    return result


# ---------------------------------------------------------------------------
# Template catalogue helpers
# ---------------------------------------------------------------------------


def list_templates(templates_dir: Path) -> list[dict[str, Any]]:
    """Return a list of available template descriptors from *templates_dir*.

    Searches ``templates_dir`` and its ``generic/`` subdirectory for
    ``.yaml`` files that contain a ``phases`` key (phase-based templates).
    Non-phase-based templates (the older endpoint-specific format) are
    excluded.

    Each descriptor contains:
    - ``template``: template identifier (without ``.yaml``)
    - ``name``: raw name string from the YAML
    - ``description``: description from the YAML
    - ``variables``: list of declared variable names
    - ``required_variables``: list of required variable names
    - ``path``: relative path from templates_dir (e.g. ``"generic/tdd.yaml"``)

    Parameters
    ----------
    templates_dir:
        Root directory to search.

    Returns
    -------
    list[dict]
        Sorted alphabetically by template identifier.
    """
    if yaml is None:  # pragma: no cover
        return []

    descriptors: list[dict[str, Any]] = []
    search_dirs = [
        (templates_dir, ""),
        (templates_dir / "generic", "generic/"),
    ]
    for search_dir, prefix in search_dirs:
        if not search_dir.exists():
            continue
        for yaml_path in sorted(search_dir.glob("*.yaml")):
            try:
                with open(yaml_path, encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                if not isinstance(raw, dict):
                    continue
                # Only include phase-based templates
                if "phases" not in raw:
                    continue
                raw_vars = raw.get("variables") or {}
                declared = list(raw_vars.keys()) if isinstance(raw_vars, dict) else []
                required = [
                    v
                    for v, spec in (raw_vars.items() if isinstance(raw_vars, dict) else [])
                    if isinstance(spec, dict) and spec.get("required", True)
                ]
                descriptors.append({
                    "template": yaml_path.stem,
                    "name": str(raw.get("name", yaml_path.stem)),
                    "description": str(raw.get("description", "")),
                    "variables": declared,
                    "required_variables": required,
                    "path": f"{prefix}{yaml_path.name}",
                })
            except Exception:
                # Skip unreadable / malformed files silently.
                continue

    # Deduplicate by template name (prefer generic/ over root-level)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for desc in sorted(descriptors, key=lambda d: d["template"]):
        if desc["template"] not in seen:
            seen.add(desc["template"])
            deduped.append(desc)

    return deduped
