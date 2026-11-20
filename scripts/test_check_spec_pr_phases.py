import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
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


class RealGitTest(unittest.TestCase):
    """The plan-merged check against real objects: a clone of a local bare `origin`."""

    # Git variables that would point git at another repository or object store.
    INHERITED_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                         "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR")

    def setUp(self):
        # Isolate from the user's and system git config (signing, hooks, default branch) and
        # from an inherited repository location; the production code inherits os.environ.
        environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull,
                                              "GIT_CONFIG_NOSYSTEM": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        for name in self.INHERITED_GIT_ENV:
            os.environ.pop(name, None)  # patch.dict restores them on cleanup
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.remote, self.work = base / "origin.git", base / "work"
        self.git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.git(base, "clone", "-q", str(self.remote), str(self.work))
        self.git(self.work, "checkout", "-q", "-b", "main")
        self.git(self.work, "config", "user.name", "Test")
        self.git(self.work, "config", "user.email", "test@example.com")
        self.git(self.work, "config", "commit.gpgsign", "false")
        previous = os.getcwd()
        os.chdir(self.work)  # the check runs git in the current directory
        self.addCleanup(os.chdir, previous)

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout

    def publish(self, *paths):
        """Commit the files and push them to origin/main."""
        for path in paths:
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n")
        self.git(self.work, "add", "--", *paths)
        self.git(self.work, "commit", "-q", "-m", "files")
        self.git(self.work, "push", "-q", "origin", "main")

    def run_plan_merged(self, feature, ref="origin/main"):
        args = type("A", (), {"feature": feature, "ref": ref})()
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = check.run_plan_merged(args)
        return code, out.getvalue(), err.getvalue()

    def test_missing_on_ref_is_empty_when_both_design_files_are_on_the_ref(self):
        self.publish(f"{FEATURE}/spec.md", f"{FEATURE}/plan.md")

        self.assertEqual([], check.missing_on_ref(FEATURE, "origin/main"))

    def test_missing_on_ref_names_each_file_that_is_not_there(self):
        self.publish(f"{FEATURE}/spec.md")

        self.assertEqual(["plan.md"], check.missing_on_ref(FEATURE, "origin/main"))
        self.assertEqual(["spec.md", "plan.md"], check.missing_on_ref("specs/other", "origin/main"))

    def test_a_file_only_in_the_working_tree_is_not_on_the_ref(self):
        self.publish(f"{FEATURE}/spec.md", f"{FEATURE}/plan.md")
        (self.work / "specs/006-new").mkdir(parents=True)
        (self.work / "specs/006-new/spec.md").write_text("x\n")

        self.assertEqual(["spec.md", "plan.md"], check.missing_on_ref("specs/006-new", "origin/main"))

    def test_plan_merged_passes_when_both_files_are_on_origin_main(self):
        self.publish(f"{FEATURE}/spec.md", f"{FEATURE}/plan.md")

        code, out, err = self.run_plan_merged(FEATURE)

        self.assertEqual(0, code, err)
        self.assertIn("spec.md and plan.md are on origin/main", out)

    def test_plan_merged_names_the_missing_file(self):
        self.publish(f"{FEATURE}/spec.md")

        code, out, err = self.run_plan_merged(FEATURE)

        self.assertEqual(1, code)
        self.assertIn(f"{FEATURE}: plan.md not found on origin/main", err)
        self.assertNotIn("spec.md not found", err)

    def test_plan_merged_accepts_a_feature_path_with_a_trailing_slash(self):
        self.publish(f"{FEATURE}/spec.md", f"{FEATURE}/plan.md")

        code, out, err = self.run_plan_merged(f"{FEATURE}/")

        self.assertEqual(0, code, err)
        self.assertIn(f"{FEATURE}: spec.md and plan.md are on", out)

    def test_plan_merged_fetches_so_a_file_pushed_by_someone_else_counts(self):
        other = self.work.parent / "other"
        self.git(self.work.parent, "clone", "-q", str(self.remote), str(other))
        self.git(other, "config", "user.name", "Other")
        self.git(other, "config", "user.email", "other@example.com")
        self.git(other, "config", "commit.gpgsign", "false")
        self.publish(".keep")  # origin/main exists locally, without the feature
        self.git(other, "pull", "-q", "origin", "main")
        for name in ("spec.md", "plan.md"):
            (other / FEATURE).mkdir(parents=True, exist_ok=True)
            (other / FEATURE / name).write_text("x\n")
        self.git(other, "add", "--", FEATURE)
        self.git(other, "commit", "-q", "-m", "feature")
        self.git(other, "push", "-q", "origin", "HEAD:main")

        code, _, err = self.run_plan_merged(FEATURE)

        self.assertEqual(0, code, err)


if __name__ == "__main__":
    unittest.main()
