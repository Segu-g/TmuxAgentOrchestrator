"""ScratchpadStore — in-memory scratchpad backed by filesystem write-through.

Implements the Blackboard architectural pattern (Buschmann et al., 1996) with
optional file persistence.  Every API write is atomically mirrored to a file
under `persist_dir/`; on server restart the directory is scanned and the
in-memory state is restored.

Design references:
- Buschmann et al. "Pattern-Oriented Software Architecture" (1996) — Blackboard
- ActiveState Code Recipe 579097 — atomic file write (write + rename)
- python-atomicwrites docs https://python-atomicwrites.readthedocs.io/
- DESIGN.md §10.77 (v1.2.1)

Atomic write pattern (POSIX):
    1. Write value to a hidden temp file in the same directory.
    2. `os.replace(tmp, target)` — atomic rename on the same filesystem.
    This guarantees that readers never see a partial write; the file either
    contains the old value or the new value, never a corrupt intermediate.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _validate_key(key: str) -> None:
    """Raise ValueError if *key* is not safe as a filename.

    Rejected patterns:
    - Empty string
    - Contains ``/`` (path separator — directory traversal)
    - Contains ``..`` (parent directory reference)
    - Starts with ``.`` (collides with temp-file naming convention ``.{key}.tmp``)
    """
    if not key:
        raise ValueError("Scratchpad key must not be empty.")
    if "/" in key:
        raise ValueError(f"Scratchpad key {key!r} must not contain '/'.")
    if ".." in key:
        raise ValueError(f"Scratchpad key {key!r} must not contain '..'.")
    if key.startswith("."):
        raise ValueError(f"Scratchpad key {key!r} must not start with '.'.")


class ScratchpadStore:
    """In-memory scratchpad backed by filesystem write-through.

    When *persist_dir* is provided the store maintains a flat directory of
    one JSON file per key.  Each file contains the JSON-serialised value so
    that ``cat .orchestrator/scratchpad/my_key`` returns a human-readable
    representation.

    When *persist_dir* is ``None`` the store behaves like a plain dict (all
    existing tests that construct ``_scratchpad: dict`` directly still pass).

    Dict-like interface
    -------------------
    The store implements the minimal dict protocol used by existing router
    code so it can be passed as a drop-in replacement for ``dict``:

        ``__getitem__``, ``__setitem__``, ``__delitem__``, ``__contains__``,
        ``__iter__``, ``__len__``, ``items()``, ``keys()``, ``get()``.

    Calling ``dict(store)`` or ``dict(scratchpad)`` in the router's
    ``scratchpad_list`` handler therefore works unchanged.
    """

    def __init__(self, persist_dir: Path | None = None) -> None:
        self._data: dict[str, Any] = {}
        self._dir: Path | None = persist_dir
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)
            self._restore()

    # ------------------------------------------------------------------
    # Core KV operations
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not present."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Write *value* under *key*, mirroring to disk atomically."""
        _validate_key(key)
        self._data[key] = value
        if self._dir is not None:
            self._write_file(key, value)

    def delete(self, key: str) -> None:
        """Remove *key* from the store, deleting its backing file if present.

        Raises ``KeyError`` if *key* does not exist.
        """
        del self._data[key]  # raises KeyError if absent
        if self._dir is not None:
            target = self._dir / key
            try:
                target.unlink()
            except FileNotFoundError:
                pass  # already gone — idempotent

    def list_keys(self) -> list[str]:
        """Return a sorted list of all keys."""
        return sorted(self._data.keys())

    def to_dict(self) -> dict[str, Any]:
        """Return a shallow copy of the underlying data dict."""
        return dict(self._data)

    # ------------------------------------------------------------------
    # Dict-like interface (drop-in replacement for plain dict)
    # ------------------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        self.delete(key)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def items(self):  # type: ignore[override]
        return self._data.items()

    def keys(self):  # type: ignore[override]
        return self._data.keys()

    def values(self):  # type: ignore[override]
        return self._data.values()

    def clear(self) -> None:
        """Remove all keys from the in-memory store.

        Does NOT delete backing files (use explicit ``delete()`` calls for
        that).  This matches the semantics of ``dict.clear()`` and is used
        by the test fixture to reset module-level state between test cases.
        """
        self._data.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_file(self, key: str, value: Any) -> None:
        """Atomically write *value* (JSON-serialised) to ``persist_dir/key``.

        Pattern: write to a hidden temp file (``.{key}.tmp``), then
        ``os.replace()`` for atomic rename.  The temp file is in the same
        directory as the target, guaranteeing a same-filesystem rename.

        Reference: ActiveState Recipe 579097; DESIGN.md §10.77.
        """
        assert self._dir is not None
        target = self._dir / key
        tmp = self._dir / f".{key}.tmp"
        try:
            tmp.write_text(json.dumps(value), encoding="utf-8")
            os.replace(tmp, target)
        except Exception:
            # Clean up orphaned temp file on failure (best-effort).
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    def _restore(self) -> None:
        """Load all persisted keys from ``persist_dir`` into memory.

        Files starting with ``.`` are skipped (temp files or hidden files).
        Each file is expected to contain a JSON-serialised value; non-JSON
        files are skipped with a warning (graceful degradation).
        """
        assert self._dir is not None
        loaded = 0
        for path in self._dir.iterdir():
            if path.name.startswith("."):
                continue  # skip temp files and hidden files
            try:
                text = path.read_text(encoding="utf-8")
                value = json.loads(text)
                self._data[path.name] = value
                loaded += 1
            except Exception as exc:
                logger.warning(
                    "ScratchpadStore: skipping unreadable file %s: %s", path, exc
                )
        if loaded:
            logger.info("ScratchpadStore: restored %d key(s) from %s", loaded, self._dir)
