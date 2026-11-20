"""Tests for app-local config persistence using disposable temporary paths."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from file_assistant.config import AppConfig, load_config, save_config


class ConfigTests(unittest.TestCase):
    """Exercise persistence and fail-closed handling of invalid state."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.state = self.base / "app-state" / "config.json"
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def test_save_and_load_selected_workspace(self):
        save_config(AppConfig(self.workspace), self.state)
        self.assertEqual(load_config(self.state), AppConfig(self.workspace))
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["workspace"], str(self.workspace))
        self.assertEqual(list(self.state.parent.glob("*.tmp")), [])

    def test_missing_or_corrupt_config_has_no_workspace(self):
        self.assertEqual(load_config(self.state), AppConfig())
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{broken", encoding="utf-8")
        self.assertEqual(load_config(self.state), AppConfig())

    def test_invalid_persisted_root_does_not_widen_scope(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"workspace": str(self.base / "missing")}), encoding="utf-8")
        self.assertEqual(load_config(self.state), AppConfig())

    def test_saving_relative_missing_or_file_root_is_rejected(self):
        cases = (
            (Path("relative"), ValueError),
            (self.base / "missing", FileNotFoundError),
            (self.state, NotADirectoryError),
        )
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.touch()
        for root, error in cases:
            with self.subTest(root=root), self.assertRaises(error):
                save_config(AppConfig(root), self.state)

    def test_replacing_root_is_persisted_explicitly(self):
        another = self.base / "another"
        another.mkdir()
        save_config(AppConfig(self.workspace), self.state)
        save_config(AppConfig(another), self.state)
        self.assertEqual(load_config(self.state).workspace, another)


    # Workspace selection must share T2 policy and protect application state.
    def test_save_rejects_state_overlap_without_changing_config(self):
        """Reject state itself, its ancestor, and its child as managed roots."""
        save_config(AppConfig(self.workspace), self.state)
        original = self.state.read_bytes()
        child = self.state.parent / "child"
        child.mkdir()
        for root in (self.state.parent, self.base, child):
            with self.subTest(root=root):
                with self.assertRaises(ValueError):
                    save_config(AppConfig(root), self.state)
                self.assertEqual(self.state.read_bytes(), original)

    def test_load_rejects_state_overlap(self):
        """Persisted configuration cannot grant access to app-owned state."""
        self.state.parent.mkdir()
        for root in (self.state.parent, self.base):
            with self.subTest(root=root):
                self.state.write_text(json.dumps({"workspace": str(root)}))
                self.assertEqual(load_config(self.state), AppConfig())

    def test_save_rejects_overlap_before_creating_state(self):
        """A rejected root must not create a config directory inside the root."""
        with self.assertRaises(ValueError):
            save_config(AppConfig(self.base), self.state)
        self.assertFalse(self.state.parent.exists())

    def test_selection_rejects_redirect_in_workspace_ancestor(self):
        """Both loading and saving enforce the shared ancestor-link policy."""
        save_config(AppConfig(self.workspace), self.state)
        with patch.object(Path, "is_symlink", new=lambda path: path == self.base):
            with self.assertRaises(ValueError):
                save_config(AppConfig(self.workspace), self.state)
            self.assertEqual(load_config(self.state), AppConfig())

    def test_selection_uses_canonical_root(self):
        """Persist and load the same canonical root returned by T2."""
        child = self.workspace / "child"
        child.mkdir()
        alias = child / ".."
        save_config(AppConfig(alias), self.state)
        self.assertEqual(load_config(self.state).workspace, self.workspace.resolve())
        stored = json.loads(self.state.read_text())["workspace"]
        self.assertEqual(stored, str(self.workspace.resolve()))

    def test_selection_rejects_windows_alias_name(self):
        """Invalid trailing-dot names are refused before state is written."""
        alias = self.base / "workspace."
        with self.assertRaises(ValueError):
            save_config(AppConfig(alias), self.state)
        self.state.parent.mkdir(exist_ok=True)
        self.state.write_text(json.dumps({"workspace": str(alias)}))
        self.assertEqual(load_config(self.state), AppConfig())


if __name__ == "__main__":
    unittest.main()
