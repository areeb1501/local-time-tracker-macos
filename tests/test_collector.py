"""Focused regression tests for collector interval change detection."""
import unittest

from timetracker.collector import title_change_key


class TitleChangeKeyTests(unittest.TestCase):
    def test_braille_spinner_frames_are_stable_in_terminal(self):
        self.assertEqual(
            title_change_key("Ghostty", "⠂ Add nested subtasks"),
            title_change_key("Ghostty", "⠐ Add nested subtasks"),
        )

    def test_star_spinner_frames_are_stable_in_terminal(self):
        self.assertEqual(
            title_change_key("Terminal", "✳ Implement optimization"),
            title_change_key("Terminal", "✢ Implement optimization"),
        )

    def test_half_circle_spinner_frames_are_stable_in_terminal(self):
        self.assertEqual(
            title_change_key("Ghostty", "◐ tt info file path linking"),
            title_change_key("Ghostty", "✳ tt info file path linking"),
        )

    def test_file_path_decodes_file_urls(self):
        from timetracker.collector import file_path
        self.assertEqual(file_path("file:///Users/a/Acme%20Dash/"), "/Users/a/Acme Dash/")
        self.assertEqual(file_path("/tmp/x.xlsx"), "/tmp/x.xlsx")
        self.assertEqual(file_path("missing value"), "")

    def test_real_terminal_title_change_is_preserved(self):
        self.assertNotEqual(
            title_change_key("Ghostty", "⠂ First task"),
            title_change_key("Ghostty", "⠐ Second task"),
        )

    def test_non_terminal_leading_symbol_is_preserved(self):
        self.assertNotEqual(
            title_change_key("Brave Browser", "✳ First page"),
            title_change_key("Brave Browser", "✢ First page"),
        )


if __name__ == "__main__":
    unittest.main()
