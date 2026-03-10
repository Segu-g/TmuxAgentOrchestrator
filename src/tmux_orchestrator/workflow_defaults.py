"""Workflow template defaults — parameter inheritance for YAML templates.

This module provides utilities for loading ``examples/workflows/*.yaml``
templates with a ``defaults:`` section.  The ``defaults:`` block supplies
fallback values for any field not explicitly set in the template body,
enabling DRY configuration across workflow families.

Merge semantics (GitLab-CI ``default:`` inspired):
- Scalar / list fields: ``defaults`` value is used **only** when the field is
  absent from the main template body.  If the field is present (even as
  ``null``), the main-body value takes precedence.
- Nested dict fields: recursive merge — defaults fill in missing leaf keys
  without overwriting any explicitly set value.

Design references:
- GitLab CI/CD ``default:`` keyword: job-level settings inherit from a
  top-level ``default:`` block unless overridden at the job level.
  https://docs.gitlab.com/ci/yaml/ (2025)
- HiYaPyCo deep-merge pattern: hierarchical YAML merging for Python configs.
  https://github.com/zerwes/hiyapyco (2024)
- MoldStud YAML inheritance article: base + overlay pattern for DRY configs.
  https://moldstud.com/articles/p-solving-the-yaml-inheritance-puzzle (2024)
- DESIGN.md §10.65 (v1.1.33)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import]
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


def deep_merge_defaults(base: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with *defaults* filling in missing keys from *base*.

    Merge rules:
    - If a key exists in *base*, its value is kept unchanged (even if ``None``
      or an empty list/string).
    - If a key is absent from *base* but present in *defaults*, the default
      value is copied into the result.
    - When both *base* and *defaults* have the same key **and both values are
      dicts**, the merge is applied recursively so that nested defaults fill in
      missing sub-keys without overwriting existing ones.
    - Lists are NOT merged: if a list key is present in *base*, the base list
      wins; if absent, the defaults list is used.

    Parameters
    ----------
    base:
        The primary configuration dict (typically the YAML template body after
        stripping ``workflow:`` and ``defaults:`` keys).
    defaults:
        The ``defaults:`` section from the same YAML template.

    Returns
    -------
    dict
        A new dict combining *base* with fallback values from *defaults*.

    Examples
    --------
    >>> deep_merge_defaults({"a": 1}, {"a": 99, "b": 2})
    {'a': 1, 'b': 2}
    >>> deep_merge_defaults({"nested": {"x": 1}}, {"nested": {"x": 0, "y": 2}})
    {'nested': {'x': 1, 'y': 2}}
    >>> deep_merge_defaults({}, {"tags": ["gpu"]})
    {'tags': ['gpu']}
    """
    result: dict[str, Any] = dict(base)
    for key, default_value in defaults.items():
        if key not in result:
            # Key is absent from base → copy the default
            result[key] = default_value
        elif isinstance(result[key], dict) and isinstance(default_value, dict):
            # Both values are dicts → recurse
            result[key] = deep_merge_defaults(result[key], default_value)
        # else: key is present in base (non-dict) → keep base value unchanged
    return result


def apply_workflow_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Apply the ``defaults:`` section of a workflow YAML to the main body.

    This is the primary public entry point for the defaults mechanism.  It:

    1. Extracts the ``defaults:`` dict from *data* (if present).
    2. Strips the ``workflow:`` metadata key (informational only).
    3. Strips the ``defaults:`` key from the returned dict.
    4. Deep-merges defaults into the remaining fields.

    The ``workflow:`` and ``defaults:`` keys are **not** included in the
    returned dict, so the result can be passed directly to a Pydantic
    ``model_validate()`` call.

    Parameters
    ----------
    data:
        Raw dict loaded from a workflow YAML file.

    Returns
    -------
    dict
        The merged configuration dict, ready for schema validation.

    Examples
    --------
    >>> apply_workflow_defaults({
    ...     "workflow": {"endpoint": "/workflows/tdd"},
    ...     "defaults": {"language": "python", "reply_to": None},
    ...     "feature": "a stack",
    ... })
    {'feature': 'a stack', 'language': 'python', 'reply_to': None}
    """
    if not isinstance(data, dict):
        return data  # type: ignore[return-value]

    defaults: dict[str, Any] = data.get("defaults") or {}
    # Strip metadata keys before merging
    body = {k: v for k, v in data.items() if k not in ("workflow", "defaults")}
    if not defaults:
        return body
    return deep_merge_defaults(body, defaults)


def load_workflow_template(path: Path) -> dict[str, Any]:
    """Load a workflow YAML template and apply its ``defaults:`` section.

    Convenience wrapper around :func:`apply_workflow_defaults` that reads the
    YAML file from *path* before processing.

    Parameters
    ----------
    path:
        Absolute or relative path to a ``*.yaml`` workflow template file.

    Returns
    -------
    dict
        Merged configuration dict ready for Pydantic schema validation.

    Raises
    ------
    ImportError
        If PyYAML is not installed.
    FileNotFoundError
        If *path* does not exist.
    """
    if yaml is None:  # pragma: no cover
        raise ImportError("PyYAML is required: uv add pyyaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return apply_workflow_defaults(raw or {})
