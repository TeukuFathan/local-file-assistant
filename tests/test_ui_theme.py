"""Font selection preserves a readable system fallback without downloads."""

import unittest

from file_assistant.ui.theme import choose_font


class FontChoiceTests(unittest.TestCase):
    def test_inter_is_preferred_and_installed_spelling_is_preserved(self):
        self.assertEqual(choose_font(["Arial", "Segoe UI", "INTER"], "System"), "INTER")

    def test_windows_sans_serif_is_used_when_inter_is_absent(self):
        self.assertEqual(choose_font(["Arial", "Segoe UI"], "System"), "Segoe UI")

    def test_system_font_is_preserved_if_no_preferred_family_exists(self):
        self.assertEqual(choose_font(["Other font"], "System Sans"), "System Sans")


if __name__ == "__main__":
    unittest.main()
