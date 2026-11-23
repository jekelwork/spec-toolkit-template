import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import roadmap_sync_summary  # noqa: E402

OUTPUT = """Roadmap issue sync (dry run; 3 entries)
Roadmap Epic exists: yes
[dry-run] create Feature: 900 Planned Feature
[dry-run] create SPEC: 900 Create and approve spec and plan
[dry-run] close #12 as not planned (deferred)
[dry-run] reopen deferred issue #13
[dry-run] add label Story to #14
[dry-run] update #15 (Phase 1)
[dry-run] make planning issue for 900 a sub-issue of its epic
[dry-run] add #16 to project example-org/123
[dry-run] set #16 Roadmap order to 2
"""


def summarize(output=OUTPUT, exit_code=0, board_skipped=False, code_changed=False, auto_apply=""):
    return roadmap_sync_summary.summarize(output, exit_code, board_skipped, code_changed, auto_apply)


class SummaryTest(unittest.TestCase):
    def test_the_summary_is_plain_text_without_marker_or_markup(self):
        text = summarize()
        self.assertNotIn("<!--", text)
        self.assertNotIn("<details", text)
        self.assertNotIn("```", text)

    def test_counts_changes_by_kind_and_lists_the_lines(self):
        text = summarize()
        for expected in (
            "created: 2",
            "updated: 1",
            "closed: 1",
            "reopened: 1",
            "labels: 1",
            "board: 2",
            "links and other: 1",
            "[dry-run] create Feature: 900 Planned Feature",
            "[dry-run] set #16 Roadmap order to 2",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_board_lines_are_recognised_by_their_shape(self):
        output = (
            "[dry-run] add #1 to project o/1\n"
            "[dry-run] set #1 Roadmap order to 3\n"
            "[dry-run] create Feature: Project Atlas\n"
            "[dry-run] update #2 (project plan)\n"
        )
        text = summarize(output)
        self.assertIn("board: 2", text)
        self.assertIn("created: 1", text)
        self.assertIn("updated: 1", text)

    def test_no_planned_changes(self):
        text = summarize("Roadmap issue sync (dry run; 1 entry)\n")
        self.assertIn("No issue changes are planned.", text)
        self.assertNotIn("created:", text)

    def test_says_when_board_changes_were_not_previewed(self):
        self.assertIn("Board changes were not previewed", summarize(board_skipped=True))
        self.assertNotIn("Board changes were not previewed", summarize())

    def test_a_failed_dry_run_is_reported(self):
        text = summarize("Multiple issues match epic 900", 1)
        self.assertIn("The dry run failed (exit code 1)", text)

    def test_a_pull_request_that_changes_the_sync_code_says_so_first(self):
        text = summarize(code_changed=True)
        first = text.splitlines()[0]
        self.assertIn("changes files other than ROADMAP.md and specs/", first)
        self.assertIn("base branch's code", text)
        self.assertIn("may behave differently", text)
        self.assertNotIn("base branch's code", summarize())

    def test_the_apply_sentence_matches_what_a_merge_will_do(self):
        applies = "Merging to the default branch applies these changes"
        cases = (
            (OUTPUT, "", applies),
            (OUTPUT, "true", applies),
            (OUTPUT, "False", "Automatic apply is off"),
            (OUTPUT, " false ", "has an invalid value"),
            (OUTPUT.replace("exists: yes", "exists: no"), "", "first roadmap sync is a manual, reviewed step"),
            (OUTPUT, "maybe", "has an invalid value"),
            ("[dry-run] create Feature: x\n", "", "could not be determined"),
        )
        for output, value, expected in cases:
            with self.subTest(value=value, expected=expected):
                text = summarize(output, auto_apply=value)
                self.assertIn(expected, text)
                if expected != applies:
                    self.assertNotIn(applies, text)

    def test_a_bootstrap_pull_request_is_covered_by_the_no_epic_sentence(self):
        # An uninitialised base branch prints its notice and the state line "no".
        output = "Not initialized yet: no ROADMAP.md. See docs/bootstrap.md.\nRoadmap Epic exists: no\n"
        text = summarize(output)
        self.assertIn("No roadmap Epic exists yet", text)
        self.assertNotIn("could not be determined", text)

    def test_long_output_is_truncated_at_a_line_end_below_the_comment_limit(self):
        output = "".join(f"[dry-run] create Feature: {n} {'x' * 80}\n" for n in range(2000))
        text = summarize(output)
        self.assertLess(len(text), roadmap_sync_summary.MAX_LENGTH)
        self.assertIn("truncated", text)
        self.assertIn("created: 2000", text)
        shown = [line for line in text.splitlines() if line.startswith("[dry-run]")]
        self.assertTrue(all(line.endswith("x" * 80) for line in shown))

    def test_markup_in_titles_is_neutralised(self):
        text = summarize("[dry-run] create </details> <!-- roadmap-sync-preview --> ```\n")
        self.assertNotIn("<", text)
        self.assertNotIn("```", text)


if __name__ == "__main__":
    unittest.main()
