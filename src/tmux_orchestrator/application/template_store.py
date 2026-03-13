"""TemplateStore — in-memory task template registry with parameterized prompt rendering.

Implements the Template Method / Registry pattern for prompt templates.  Users
define templates once with named ``{variable}`` placeholders; at submission time
they provide a ``dict[str, str]`` of variable values, and the store renders the
final prompt using Python's standard ``str.format(**variables)``.

Design references:
- Paul Serban "Engineering a Scalable Prompt Library" (paulserban.eu, 2025) —
  registry, renderer, I/O schema separation.
- PromptLayer Template Variables (docs.promptlayer.com, 2025) — f-string substitution
  as the simple/safe choice over Jinja2 for agent dispatch prompts.
- Spring AI PromptTemplate (docs.spring.io, 2025) — Map-based placeholder rendering.
- Argo Workflows templates (iamstoxe.com, 2024) — template definition separate from
  instantiation; parameters passed at submission time.
- DESIGN.md §10.93 (v1.2.17)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskTemplate:
    """A reusable, parameterized task prompt template.

    Attributes
    ----------
    id:
        Unique machine-readable identifier (e.g. ``"code_review"``).
        Used as the primary key in the registry.
    name:
        Human-readable label (e.g. ``"Code Review"``).
    prompt_template:
        Prompt text with ``{variable}`` placeholders (Python f-string syntax).
        All placeholders listed in ``variables`` must appear in this string.
    variables:
        Ordered list of required variable names.  Every name in this list
        must be supplied in the ``variables`` dict when calling ``render()``.
    default_priority:
        Default task priority used by ``submit()`` when not overridden.
        Lower numbers are dispatched first (consistent with the task queue).
    default_tags:
        Capability tags applied to submitted tasks by default.
    default_timeout:
        Per-task timeout (seconds) used by default; ``None`` = use orchestrator
        default.
    description:
        Free-text description of what this template is for.
    """

    id: str
    name: str
    prompt_template: str
    variables: list[str]
    default_priority: int = 0
    default_tags: list[str] = field(default_factory=list)
    default_timeout: int | None = None
    description: str = ""


class TemplateStore:
    """In-memory registry for :class:`TaskTemplate` objects.

    All operations are O(1) or O(N) on the number of stored templates.
    Thread-safety: single-threaded asyncio event loop — no locks required.

    Usage
    -----
    >>> store = TemplateStore()
    >>> store.register(TaskTemplate(
    ...     id="hello", name="Hello", prompt_template="Hello, {name}!",
    ...     variables=["name"],
    ... ))
    >>> store.render("hello", {"name": "World"})
    'Hello, World!'
    """

    def __init__(self) -> None:
        self._templates: dict[str, TaskTemplate] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(self, template: TaskTemplate) -> None:
        """Store *template* under its ``id``.  Overwrites any existing entry."""
        self._templates[template.id] = template

    def get(self, template_id: str) -> TaskTemplate | None:
        """Return the template for *template_id*, or ``None`` if not found."""
        return self._templates.get(template_id)

    def list_all(self) -> list[TaskTemplate]:
        """Return all registered templates as a list (insertion order preserved)."""
        return list(self._templates.values())

    def delete(self, template_id: str) -> bool:
        """Remove *template_id* from the registry.

        Returns
        -------
        bool
            ``True`` if the template existed and was removed; ``False`` if it
            was not found.
        """
        return bool(self._templates.pop(template_id, None))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, template_id: str, variables: dict[str, str]) -> str:
        """Render *template_id* with *variables*.

        Parameters
        ----------
        template_id:
            ID of the template to render.
        variables:
            Mapping of placeholder names to values.  Extra keys are silently
            ignored (``str.format_map`` semantics via ``**variables``).

        Returns
        -------
        str
            The rendered prompt string.

        Raises
        ------
        KeyError
            If *template_id* is not registered.
        ValueError
            If any variable declared in ``template.variables`` is absent from
            *variables* (fail-fast validation before attempting substitution).
        """
        tmpl = self.get(template_id)
        if tmpl is None:
            raise KeyError(f"Template {template_id!r} not found")

        missing = set(tmpl.variables) - set(variables)
        if missing:
            # Sort for deterministic error messages in tests.
            raise ValueError(
                f"Missing required variables for template {template_id!r}: "
                + ", ".join(sorted(missing))
            )

        try:
            return tmpl.prompt_template.format(**variables)
        except KeyError as exc:
            # A placeholder in the template string is not in `variables` dict
            # (shouldn't happen after the missing-check above, but be safe).
            raise ValueError(
                f"Unresolved placeholder {exc} in template {template_id!r}"
            ) from exc
