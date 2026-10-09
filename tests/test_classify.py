"""Unassigned long-block reminder: meetings, thresholds, skips, coalescing."""
import unittest

from timetracker import classify


def visit(**kw):
    base = {
        "app": "Google Chrome", "domain": "meet.google.com",
        "url": "https://meet.google.com/piq-gcvt-dte",
        "titles": ["Meet - Dev ops help - Microphone recording - Google Chrome"],
        "start_ts": 1000, "end_ts": 1600, "active_seconds": 600,
        "event_ids": [1],
    }
    base.update(kw)
    return base


class MeetingLabelTests(unittest.TestCase):
    def test_strips_recording_and_browser_chrome(self):
        self.assertEqual(
            classify.meeting_label(
                ["Meet - Dev ops help - Microphone recording - High memory usage - 817 MB - Google Chrome"],
                "meet.google.com"),
            "Dev ops help")

    def test_skips_meet_codes(self):
        self.assertEqual(
            classify.meeting_label(["Meet - piq-gcvt-dte - Google Chrome"], "meet.google.com"),
            "Google Meet")

    def test_is_meeting_from_domain_and_wispr(self):
        self.assertTrue(classify.is_meeting(visit()))
        self.assertTrue(classify.is_companion(visit(
            app="Wispr Flow", domain="", titles=["Meeting Recorder"])))
        self.assertFalse(classify.is_meeting(visit(
            app="Wispr Flow", domain="", titles=["Meeting Recorder"])))
        self.assertFalse(classify.is_meeting(visit(
            app="Google Chrome", domain="github.com", titles=["some repo"])))


class PromptBlockTests(unittest.TestCase):
    def test_one_minute_default_keeps_short_tab_switches_out(self):
        short = visit(start_ts=1, end_ts=46, active_seconds=45, event_ids=[1],
                      domain="github.com", titles=["some repo"])
        kept = visit(start_ts=100, end_ts=190, active_seconds=90, event_ids=[2],
                     domain="github.com", titles=["some repo"])
        out = classify.unassigned_prompt_blocks(
            [short, kept], {1: 45, 2: 90}, [], now=10_000)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["seconds"], 90)

    def test_skips_short_noise(self):
        v = visit(start_ts=1, end_ts=20, active_seconds=19, event_ids=[1])
        out = classify.unassigned_prompt_blocks(
            [v], {1: 19}, [], now=10_000, min_seconds=300)
        self.assertEqual(out, [])

    def test_includes_long_unassigned_meeting(self):
        v = visit(event_ids=[1, 2])
        out = classify.unassigned_prompt_blocks(
            [v], {1: 400, 2: 200}, [], now=10_000, min_seconds=300)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["meeting"])
        self.assertEqual(out[0]["label"], "Dev ops help")
        self.assertEqual(out[0]["seconds"], 600)

    def test_ignores_time_already_on_a_project(self):
        v = visit(event_ids=[1])
        out = classify.unassigned_prompt_blocks(
            [v], {1: 0}, [], now=10_000, min_seconds=300)
        self.assertEqual(out, [])

    def test_skip_hides_overlapping_same_key(self):
        v = visit()
        skips = [{"visit_key": "meet.google.com", "start_ts": 900, "end_ts": 1700}]
        out = classify.unassigned_prompt_blocks(
            [v], {1: 600}, skips, now=10_000, min_seconds=300)
        self.assertEqual(out, [])

    def test_does_not_prompt_while_visit_is_still_open(self):
        v = visit(end_ts=9950)
        out = classify.unassigned_prompt_blocks(
            [v], {1: 600}, [], now=10_000, min_seconds=300, open_grace=90)
        self.assertEqual(out, [])

    def test_coalesces_meet_and_recorder(self):
        meet = visit(event_ids=[1], start_ts=1000, end_ts=1600, active_seconds=600)
        wispr = visit(app="Wispr Flow", domain="", titles=["Meeting Recorder"],
                      event_ids=[2], start_ts=1610, end_ts=1900, active_seconds=290)
        out = classify.unassigned_prompt_blocks(
            [meet, wispr], {1: 600, 2: 290}, [], now=10_000, min_seconds=300)
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["event_ids"]), [1, 2])
        self.assertIn("meet.google.com", out[0]["keys"])
        self.assertIn("Wispr Flow", out[0]["keys"])
        self.assertEqual(out[0]["seconds"], 890)

    def test_short_recorder_fragment_merges_into_the_meeting(self):
        meet = visit(event_ids=[1], start_ts=1000, end_ts=1600, active_seconds=600)
        wispr = visit(app="Wispr Flow", domain="", titles=["Meeting Recorder"],
                      event_ids=[2], start_ts=1605, end_ts=1725, active_seconds=120)
        out = classify.unassigned_prompt_blocks(
            [meet, wispr], {1: 600, 2: 120}, [], now=10_000, min_seconds=300)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["seconds"], 720)
        self.assertEqual(sorted(out[0]["event_ids"]), [1, 2])

    def test_orphan_recorder_is_not_prompted(self):
        wispr = visit(app="Wispr Flow", domain="", titles=["Meeting Recorder"],
                      event_ids=[2], start_ts=1000, end_ts=1900, active_seconds=900)
        out = classify.unassigned_prompt_blocks(
            [wispr], {2: 900}, [], now=10_000, min_seconds=300)
        self.assertEqual(out, [])



class PathRuleTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE rules (id INTEGER PRIMARY KEY, kind TEXT, category TEXT,"
            " project_id INTEGER, field TEXT, match_type TEXT, pattern TEXT,"
            " priority INTEGER, enabled INTEGER DEFAULT 1);"
            "INSERT INTO projects VALUES (1, 'Acme Portal'), (2, 'Orbit'), (3, 'Job');"
            "INSERT INTO rules (kind, project_id, field, match_type, pattern, priority) VALUES"
            " ('project', 1, 'path', 'contains', '/Downloads/Acme Portal', 20),"
            " ('project', 3, 'title', 'contains', 'job', 100);")
        self.rules = classify.load_rules(self.conn)[1]

    def row(self, title="", path=None, url="", app="Ghostty"):
        return {"app": app, "bundle_id": "", "window_title": title, "url": url,
                "domain": "", "path": path, "is_afk": 0}

    def test_path_rule_links_any_file_in_the_folder(self):
        r = self.row("✳ ideas slicer", "/Users/a/Downloads/Acme Portal/x.xlsx")
        self.assertEqual(classify.rule_project(r, self.rules)[0], 1)

    def test_path_rule_outranks_title_keyword(self):
        r = self.row("job board notes", "/Users/a/Downloads/Acme Portal")
        self.assertEqual(classify.rule_project(r, self.rules)[0], 1)

    def test_folder_named_like_project_links_without_a_rule(self):
        r = self.row("notes.md", "/Users/a/code/Orbit/src/notes.md")
        self.assertEqual(classify.rule_project(r, self.rules)[0], 2)
        # whole folder names only: "Orbiter" is not "Orbit"
        r = self.row("notes.md", "/Users/a/code/Orbiter/notes.md")
        self.assertEqual(classify.rule_project(r, self.rules)[0], None)

    def test_file_url_counts_as_a_path_and_rows_without_path_work(self):
        r = self.row(url="file:///Users/a/Downloads/Acme%20Portal/a.pdf")
        self.assertEqual(classify.rule_project(r, self.rules)[0], 1)
        old = {k: v for k, v in self.row("plain").items() if k != "path"}
        self.assertEqual(classify.rule_project(old, self.rules), (None, None))

    def test_cache_separates_same_title_in_different_folders(self):
        cache = {}
        a = self.row("✳ same", "/Users/a/Downloads/Acme Portal")
        b = self.row("✳ same", "/Users/a/elsewhere")
        self.assertEqual(classify.rule_project(a, self.rules, cache)[0], 1)
        self.assertEqual(classify.rule_project(b, self.rules, cache)[0], None)


if __name__ == "__main__":
    unittest.main()
