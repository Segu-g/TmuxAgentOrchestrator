"""Unit tests for ScratchpadStore — in-memory + file persistence.

Tests cover:
- In-memory mode (no persist_dir) — pure dict behaviour
- File persistence: set → file created, get → reads memory, delete → file deleted
- Atomic write: tmp file cleaned up on success
- Restore on startup: pre-existing files loaded into memory
- Key validation: '/' in key raises ValueError
- Dict-like interface: store["key"] = value, store["key"], "key" in store
- Integration: web/app.py create_app() uses ScratchpadStore

Reference: DESIGN.md §10.77 (v1.2.1)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tmux_orchestrator.application.scratchpad_store import ScratchpadStore, _validate_key


# ---------------------------------------------------------------------------
# _validate_key
# ---------------------------------------------------------------------------


class TestValidateKey:
    def test_valid_simple_key(self):
        _validate_key("result")  # no exception

    def test_valid_key_with_dash(self):
        _validate_key("my-key")  # no exception

    def test_valid_key_with_underscore(self):
        _validate_key("my_key_123")  # no exception

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_key("")

    def test_slash_in_key_raises(self):
        with pytest.raises(ValueError, match="'/'"):
            _validate_key("foo/bar")

    def test_double_dot_in_key_raises(self):
        with pytest.raises(ValueError, match="'\\.\\.'"):
            _validate_key("..evil")

    def test_key_starting_with_dot_raises(self):
        with pytest.raises(ValueError, match="start with '\\.'"):
            _validate_key(".hidden")

    def test_absolute_path_rejected(self):
        with pytest.raises(ValueError, match="'/'"):
            _validate_key("/absolute/path")


# ---------------------------------------------------------------------------
# In-memory mode (persist_dir=None)
# ---------------------------------------------------------------------------


class TestScratchpadStoreInMemory:
    def test_set_and_get(self):
        store = ScratchpadStore()
        store.set("key1", "value1")
        assert store.get("key1") == "value1"

    def test_get_default(self):
        store = ScratchpadStore()
        assert store.get("missing") is None
        assert store.get("missing", 42) == 42

    def test_delete(self):
        store = ScratchpadStore()
        store.set("k", "v")
        store.delete("k")
        assert store.get("k") is None

    def test_delete_missing_raises_key_error(self):
        store = ScratchpadStore()
        with pytest.raises(KeyError):
            store.delete("nonexistent")

    def test_list_keys(self):
        store = ScratchpadStore()
        store.set("b", 2)
        store.set("a", 1)
        assert store.list_keys() == ["a", "b"]

    def test_to_dict(self):
        store = ScratchpadStore()
        store.set("x", 10)
        store.set("y", 20)
        d = store.to_dict()
        assert d == {"x": 10, "y": 20}

    def test_no_files_created_without_persist_dir(self, tmp_path):
        store = ScratchpadStore()  # no persist_dir
        store.set("k", "v")
        # No files should be created anywhere (can't easily check global fs, but
        # at least verify the store doesn't have a _dir set)
        assert store._dir is None

    # Dict-like interface
    def test_setitem_getitem(self):
        store = ScratchpadStore()
        store["hello"] = "world"
        assert store["hello"] == "world"

    def test_getitem_missing_raises_key_error(self):
        store = ScratchpadStore()
        with pytest.raises(KeyError):
            _ = store["missing"]

    def test_delitem(self):
        store = ScratchpadStore()
        store["k"] = "v"
        del store["k"]
        assert "k" not in store

    def test_contains(self):
        store = ScratchpadStore()
        store["x"] = 1
        assert "x" in store
        assert "y" not in store

    def test_iter(self):
        store = ScratchpadStore()
        store["a"] = 1
        store["b"] = 2
        assert set(store) == {"a", "b"}

    def test_len(self):
        store = ScratchpadStore()
        assert len(store) == 0
        store["k"] = "v"
        assert len(store) == 1

    def test_items(self):
        store = ScratchpadStore()
        store["x"] = 10
        assert list(store.items()) == [("x", 10)]

    def test_keys(self):
        store = ScratchpadStore()
        store["a"] = 1
        assert "a" in store.keys()

    def test_values(self):
        store = ScratchpadStore()
        store["a"] = 99
        assert 99 in store.values()

    def test_dict_conversion(self):
        """dict(store) must work — used in scratchpad_list router."""
        store = ScratchpadStore()
        store["p"] = "q"
        assert dict(store) == {"p": "q"}

    def test_set_validates_key(self):
        store = ScratchpadStore()
        with pytest.raises(ValueError):
            store.set("bad/key", "v")

    def test_setitem_validates_key(self):
        store = ScratchpadStore()
        with pytest.raises(ValueError):
            store["bad/key"] = "v"


# ---------------------------------------------------------------------------
# File persistence mode
# ---------------------------------------------------------------------------


class TestScratchpadStoreFilePersistence:
    def test_directory_created_on_init(self, tmp_path):
        persist_dir = tmp_path / "scratchpad"
        assert not persist_dir.exists()
        ScratchpadStore(persist_dir=persist_dir)
        assert persist_dir.is_dir()

    def test_set_creates_file(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("my_key", "hello")
        file = tmp_path / "sp" / "my_key"
        assert file.exists()
        assert json.loads(file.read_text()) == "hello"

    def test_set_file_contains_json(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("nums", [1, 2, 3])
        file = tmp_path / "sp" / "nums"
        assert json.loads(file.read_text()) == [1, 2, 3]

    def test_set_file_overwrite(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("k", "first")
        store.set("k", "second")
        file = tmp_path / "sp" / "k"
        assert json.loads(file.read_text()) == "second"

    def test_delete_removes_file(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("k", "v")
        file = tmp_path / "sp" / "k"
        assert file.exists()
        store.delete("k")
        assert not file.exists()

    def test_delete_missing_file_is_idempotent(self, tmp_path):
        """delete() must not crash if the backing file was already removed."""
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store._data["orphaned"] = "v"  # inject directly, bypassing file creation
        # file does not exist — delete() should not raise FileNotFoundError
        store.delete("orphaned")
        assert "orphaned" not in store

    def test_no_tmp_file_after_write(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("k", "v")
        tmp_file = tmp_path / "sp" / ".k.tmp"
        assert not tmp_file.exists()

    def test_atomic_write_replaces_cleanly(self, tmp_path):
        """Verify os.replace is used (tmp file naming convention)."""
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("key", "first")
        store.set("key", "second")
        # Only the final file should exist; no orphaned .key.tmp
        files = list((tmp_path / "sp").iterdir())
        assert len(files) == 1
        assert files[0].name == "key"

    def test_restore_on_init_loads_existing_files(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        (sp / "existing_key").write_text(json.dumps("restored_value"), encoding="utf-8")
        (sp / "num_key").write_text(json.dumps(42), encoding="utf-8")

        store = ScratchpadStore(persist_dir=sp)
        assert store.get("existing_key") == "restored_value"
        assert store.get("num_key") == 42

    def test_restore_skips_dot_files(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        (sp / ".hidden").write_text(json.dumps("skip_me"), encoding="utf-8")
        (sp / ".k.tmp").write_text(json.dumps("partial"), encoding="utf-8")

        store = ScratchpadStore(persist_dir=sp)
        assert ".hidden" not in store
        assert ".k.tmp" not in store

    def test_restore_skips_invalid_json_files(self, tmp_path):
        sp = tmp_path / "sp"
        sp.mkdir()
        (sp / "bad_file").write_text("NOT JSON {{{{", encoding="utf-8")
        (sp / "good_key").write_text(json.dumps("ok"), encoding="utf-8")

        store = ScratchpadStore(persist_dir=sp)  # should not raise
        assert store.get("good_key") == "ok"
        assert "bad_file" not in store

    def test_persist_survives_new_store_instance(self, tmp_path):
        """Second ScratchpadStore on the same directory sees first store's data."""
        sp = tmp_path / "sp"
        store1 = ScratchpadStore(persist_dir=sp)
        store1.set("key", "written_by_store1")

        store2 = ScratchpadStore(persist_dir=sp)
        assert store2.get("key") == "written_by_store1"

    def test_dict_json_value_persisted(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("obj", {"a": 1, "b": [2, 3]})
        file = tmp_path / "sp" / "obj"
        assert json.loads(file.read_text()) == {"a": 1, "b": [2, 3]}

    def test_none_value_persisted(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("nullkey", None)
        file = tmp_path / "sp" / "nullkey"
        assert json.loads(file.read_text()) is None

    def test_bool_value_persisted(self, tmp_path):
        store = ScratchpadStore(persist_dir=tmp_path / "sp")
        store.set("flag", True)
        file = tmp_path / "sp" / "flag"
        assert json.loads(file.read_text()) is True


# ---------------------------------------------------------------------------
# Integration: web/app.py create_app() initialises ScratchpadStore
# ---------------------------------------------------------------------------


class TestWebAppUsesScratchpadStore:
    def test_scratchpad_is_store_at_module_level(self):
        """The module-level _scratchpad is a ScratchpadStore (not a plain dict)."""
        import tmux_orchestrator.web.app as web_app
        from tmux_orchestrator.application.scratchpad_store import ScratchpadStore

        assert isinstance(web_app._scratchpad, ScratchpadStore)

    def test_scratchpad_persist_dir_matches_config(self, tmp_path):
        """After create_app() with a config, the ScratchpadStore's persist_dir
        must equal config.scratchpad_dir."""
        import tmux_orchestrator.web.app as web_app
        from tmux_orchestrator.application.scratchpad_store import ScratchpadStore
        from tmux_orchestrator.web.ws import WebSocketHub

        original = web_app._scratchpad
        try:
            sp_dir = tmp_path / "mysp"
            mock_orch = MagicMock()
            mock_orch.config = MagicMock()
            mock_orch.config.scratchpad_dir = str(sp_dir)
            mock_orch.config.mailbox_dir = str(tmp_path / "mailbox")
            mock_orch.config.session_name = "test"
            mock_bus = MagicMock()
            hub = WebSocketHub(mock_bus)

            web_app.create_app(mock_orch, hub)
            assert isinstance(web_app._scratchpad, ScratchpadStore)
            assert web_app._scratchpad._dir == sp_dir
        finally:
            web_app._scratchpad = original
