"""Tests for app-local config persistence using disposable temporary paths."""

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
