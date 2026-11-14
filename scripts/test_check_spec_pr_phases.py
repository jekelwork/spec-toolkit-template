import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).with_name("check_spec_pr_phases.py")
SPEC = importlib.util.spec_from_file_location("check_spec_pr_phases", SCRIPT_PATH)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

FEATURE = "specs/005-boards"


class Args:
    allow_label = check.ALLOW_LABEL
    base_ref = "origin/main"


class CheckSpecPrPhasesTest(unittest.TestCase):
    def test_flags_new_tasks_with_plan_in_same_pr(self):
        changes = [("added", f"{FEATURE}/plan.md"), ("added", f"{FEATURE}/tasks.md")]

        self.assertEqual([FEATURE], check.find_combined_features(changes))

    def test_allows_spec_and_plan_without_tasks(self):
        changes = [("added", f"{FEATURE}/spec.md"), ("added", f"{FEATURE}/plan.md")]

        self.assertEqual([], check.find_combined_features(changes))

    def test_allows_tasks_only_pr(self):
        changes = [("added", f"{FEATURE}/tasks.md")]

        self.assertEqual([], check.find_combined_features(changes))

    def test_allows_editing_existing_tasks_with_plan(self):
        changes = [("modified", f"{FEATURE}/plan.md"), ("modified", f"{FEATURE}/tasks.md")]

        self.assertEqual([], check.find_combined_features(changes))

    def test_features_are_checked_independently(self):
        changes = [
            ("added", "specs/005-boards/tasks.md"),
            ("modified", "specs/006-issues/plan.md"),
        ]

        self.assertEqual([], check.find_combined_features(changes))

    def run_pr_files(self, stdin, labels="", missing=None):
        with patch.object(check, "missing_on_ref", return_value=missing or []):
            with patch("sys.stdin", io.StringIO(stdin)), patch.dict(os.environ, {"LABELS": labels}):
                with patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
                    return check.run_pr_files(Args())

    def test_pr_files_fails_for_combined_pr(self):
        stdin = f"added\t{FEATURE}/plan.md\nadded\t{FEATURE}/tasks.md\n"

        self.assertEqual(1, self.run_pr_files(stdin))

    def test_allow_label_overrides(self):
        stdin = f"added\t{FEATURE}/plan.md\nadded\t{FEATURE}/tasks.md\n"

        self.assertEqual(0, self.run_pr_files(stdin, labels=f"docs,{check.ALLOW_LABEL}"))

    def test_tasks_only_pr_fails_when_plan_not_on_base(self):
        stdin = f"added\t{FEATURE}/tasks.md\n"

        self.assertEqual(1, self.run_pr_files(stdin, missing=["plan.md"]))

    def test_tasks_only_pr_passes_when_plan_on_base(self):
        stdin = f"added\t{FEATURE}/tasks.md\n"

        self.assertEqual(0, self.run_pr_files(stdin, missing=[]))

    def test_allow_label_skips_unmerged_plan_check(self):
        stdin = f"added\t{FEATURE}/tasks.md\n"

        self.assertEqual(
            0, self.run_pr_files(stdin, labels=check.ALLOW_LABEL, missing=["spec.md", "plan.md"])
        )

    def test_editing_existing_tasks_skips_base_check(self):
        stdin = f"modified\t{FEATURE}/tasks.md\n"

        self.assertEqual(0, self.run_pr_files(stdin, missing=["plan.md"]))

    @patch.object(check.subprocess, "run")
    def test_plan_merged_reports_missing_files(self, run):
        run.return_value.returncode = 1
        args = type("A", (), {"feature": FEATURE, "ref": "origin/main"})()

        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(1, check.run_plan_merged(args))

    @patch.object(check.subprocess, "run")
    def test_plan_merged_message_names_the_principle_not_its_number(self, run):
        # An adopter's constitution numbers its principles freely; refer to them by name.
        run.return_value.returncode = 1
        args = type("A", (), {"feature": FEATURE, "ref": "origin/main"})()

        with patch("sys.stderr", io.StringIO()) as stderr:
            check.run_plan_merged(args)

        self.assertNotRegex(stderr.getvalue(), r"constitution [IVX]+\b")
        self.assertIn("Git workflow principle", stderr.getvalue())

    @patch.object(check.subprocess, "run")
    def test_plan_merged_passes_when_files_exist(self, run):
        run.return_value.returncode = 0
        args = type("A", (), {"feature": FEATURE, "ref": "origin/main"})()

        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(0, check.run_plan_merged(args))


if __name__ == "__main__":
    unittest.main()
