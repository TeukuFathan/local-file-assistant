"""Font selection preserves a readable system fallback without downloads."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from file_assistant.ui.theme import RoundedPanel, choose_font


class FontChoiceTests(unittest.TestCase):
    def test_inter_is_preferred_and_installed_spelling_is_preserved(self):
        self.assertEqual(choose_font(["Arial", "Segoe UI", "INTER"], "System"), "INTER")

    def test_windows_sans_serif_is_used_when_inter_is_absent(self):
        self.assertEqual(choose_font(["Arial", "Segoe UI"], "System"), "Segoe UI")

    def test_system_font_is_preserved_if_no_preferred_family_exists(self):
        self.assertEqual(choose_font(["Other font"], "System Sans"), "System Sans")


class PanelLayoutTests(unittest.TestCase):
    def test_initially_clipped_content_still_requests_enough_height(self):
        """A canvas starting at one pixel must reveal its controls on resize."""
        panel = RoundedPanel.__new__(RoundedPanel)
        panel.padding = 18
        panel.content_id, panel.shape = "content", "shape"
        panel.content = Mock()
        panel.content.winfo_reqheight.return_value = 90
        panel.cget = Mock(return_value="1")
        panel.configure, panel.itemconfigure, panel.coords = Mock(), Mock(), Mock()
        panel._resize(SimpleNamespace(width=500, height=1))
        panel.configure.assert_called_once_with(height=126)


if __name__ == "__main__":
    unittest.main()
