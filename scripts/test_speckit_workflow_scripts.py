import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from speckit_hostile_names import (  # noqa: E402
    HOSTILE_NAMES,
    markers_under,
    mutate_common,
    remove_markers,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".specify" / "scripts" / "bash"
SCRIPT_NAMES = (
    "setup-tasks.sh",
    "check-prerequisites.sh",
    "create-new-feature.sh",
)


class ScriptSandboxCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project with spaces"
        self.script_dir = self.root / ".specify" / "scripts" / "bash"
        self.script_dir.mkdir(parents=True)
        for name in (*SCRIPT_NAMES, "common.sh"):
            shutil.copy2(SCRIPTS / name, self.script_dir / name)
        templates = self.root / ".specify" / "templates" / "overrides"
        templates.mkdir(parents=True)
        (templates / "tasks-template.md").write_text("# Tasks\n\n", encoding="utf-8")
        (templates / "spec-template.md").write_text("# Specification\n", encoding="utf-8")
        self.environment = os.environ.copy()
        for key in (
            "SPECIFY_INIT_DIR",
            "SPECIFY_FEATURE_DIRECTORY",
            "SPECIFY_FEATURE",
            "SPECIFY_FEATURE_NO_PERSIST",
        ):
            self.environment.pop(key, None)
        self.environment["SPECIFY_INIT_DIR"] = str(self.root)

    def run_script(self, name, *args):
        return subprocess.run(
            ["bash", str(self.script_dir / name), *args],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def feature(self, relative="specs/013 odd '$(touch SHOULD_NOT_EXIST)"):
        self.environment["SPECIFY_FEATURE_DIRECTORY"] = relative
        directory = self.root / relative
        directory.mkdir(parents=True)
        (directory / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (directory / "spec.md").write_text("# Spec\n", encoding="utf-8")
        return directory


class SpecKitWorkflowScriptsTest(ScriptSandboxCase):
    def test_tasks_setup_returns_paths_documents_and_exact_template(self):
        directory = self.feature()
        for name in ("research.md", "data-model.md", "quickstart.md"):
            (directory / name).touch()
        (directory / "contracts").mkdir()
        (directory / "contracts" / "api.md").touch()

        result = self.run_script("setup-tasks.sh", "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(str(directory), payload["FEATURE_DIR"])
        self.assertEqual(
            ["research.md", "data-model.md", "contracts/", "quickstart.md"],
            payload["AVAILABLE_DOCS"],
        )
        self.assertEqual("# Tasks\n\n", payload["TASKS_TEMPLATE_CONTENT"])
        self.assertEqual(
            str(self.root / ".specify/templates/overrides/tasks-template.md"),
            payload["TASKS_TEMPLATE"],
        )
        self.assertEqual(
            self.environment["SPECIFY_FEATURE_DIRECTORY"],
            json.loads((self.root / ".specify/feature.json").read_text())["feature_directory"],
        )
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_tasks_setup_reports_missing_plan_and_spec(self):
        directory = self.feature("specs/013-example")
        for name in ("plan.md", "spec.md"):
            with self.subTest(missing=name):
                path = directory / name
                content = path.read_text()
                path.unlink()
                result = self.run_script("setup-tasks.sh", "--json")
                self.assertNotEqual(0, result.returncode)
                self.assertIn(f"{name} not found", result.stderr)
                self.assertEqual("", result.stdout)
                path.write_text(content)

    def test_tasks_setup_respects_no_persist_and_reports_missing_template(self):
        self.feature("specs/013-example")
        self.environment["SPECIFY_FEATURE_NO_PERSIST"] = "1"
        (self.root / ".specify/templates/overrides/tasks-template.md").unlink()

        result = self.run_script("setup-tasks.sh", "--json")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Could not resolve required tasks-template", result.stderr)
        self.assertFalse((self.root / ".specify/feature.json").exists())

    def test_tasks_setup_plain_output_preserves_pinned_feature_when_no_persist(self):
        directory = self.feature("specs/013-example")
        feature_json = self.root / ".specify/feature.json"
        feature_json.write_text(json.dumps({"feature_directory": "specs/pinned"}))
        self.environment["SPECIFY_FEATURE_NO_PERSIST"] = "1"

        result = self.run_script("setup-tasks.sh")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"FEATURE_DIR: {directory}", result.stdout)
        self.assertEqual("specs/pinned", json.loads(feature_json.read_text())["feature_directory"])

    def test_prerequisite_paths_only_does_not_persist(self):
        directory = self.feature()

        result = self.run_script("check-prerequisites.sh", "--paths-only", "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(str(directory), payload["FEATURE_DIR"])
        self.assertEqual(str(directory / "tasks.md"), payload["TASKS"])
        self.assertFalse((self.root / ".specify/feature.json").exists())
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_prerequisites_validate_and_include_tasks_and_template(self):
        directory = self.feature("specs/013-example")
        missing = self.run_script(
            "check-prerequisites.sh", "--json", "--require-spec", "--require-tasks"
        )
        self.assertNotEqual(0, missing.returncode)
        self.assertIn("tasks.md not found", missing.stderr)

        (directory / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
        result = self.run_script(
            "check-prerequisites.sh",
            "--json",
            "--require-spec",
            "--require-tasks",
            "--include-tasks",
            "--template",
            "tasks-template",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(["tasks.md"], payload["AVAILABLE_DOCS"])
        self.assertEqual("# Tasks\n\n", payload["TEMPLATE_CONTENT"])
        self.assertTrue((self.root / ".specify/feature.json").is_file())

    def test_prerequisites_report_resolution_errors(self):
        self.environment["SPECIFY_INIT_DIR"] = str(self.root / "missing")

        result = self.run_script("check-prerequisites.sh", "--json", "--paths-only")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("SPECIFY_INIT_DIR", result.stderr)
        self.assertIn("Failed to resolve feature paths", result.stderr)

    def test_feature_creation_accepts_mixed_option_order_without_side_effects(self):
        result = self.run_script(
            "create-new-feature.sh",
            "A new",
            "--number",
            "25",
            "--json",
            "database feature",
            "--short-name",
            "safe database",
            "--dry-run",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("025-safe-database", payload["BRANCH_NAME"])
        self.assertEqual("025", payload["FEATURE_NUM"])
        self.assertTrue(payload["DRY_RUN"])
        self.assertFalse((self.root / "specs").exists())
        self.assertFalse((self.root / ".specify/feature.json").exists())

    def test_feature_creation_requires_option_values(self):
        for option in ("--number", "--short-name"):
            for args in ((option,), (option, "--json")):
                with self.subTest(args=args):
                    result = self.run_script("create-new-feature.sh", *args)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(f"{option} requires a value", result.stderr)

    def test_feature_creation_help_does_not_require_a_description(self):
        result = self.run_script("create-new-feature.sh", "--help")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--short-name", result.stdout)
        self.assertFalse((self.root / "specs").exists())

    def test_feature_creation_creates_spec_and_persists_feature(self):
        result = self.run_script(
            "create-new-feature.sh", "--json", "--number", "13", "Create database"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("013-create-database", payload["BRANCH_NAME"])
        spec = Path(payload["SPEC_FILE"])
        self.assertEqual("# Specification\n", spec.read_text())
        self.assertEqual(
            "specs/013-create-database",
            json.loads((self.root / ".specify/feature.json").read_text())["feature_directory"],
        )


# Every caller of get_feature_paths that evals its output, with the arguments
# that select the mode under test. The tasks.md the validating mode needs is
# created by HostileNamesStayDataTest.hostile_feature.
CALLERS = {
    "setup-tasks --json": ("setup-tasks.sh", "--json"),
    "check-prerequisites --json": (
        "check-prerequisites.sh",
        "--json",
        "--require-spec",
        "--require-tasks",
    ),
    "check-prerequisites --paths-only --json": (
        "check-prerequisites.sh",
        "--paths-only",
        "--json",
    ),
}
# Callers that persist .specify/feature.json when persistence is left on.
PERSISTING = ("setup-tasks --json", "check-prerequisites --json")


class HostileNamesStayDataTest(ScriptSandboxCase):
    """Behaviour guard: hostile names are never executed and round-trip exactly."""

    def hostile_feature(self, name):
        directory = self.feature(f"specs/{name}")
        (directory / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
        return directory

    def run_caller(self, label):
        result = self.run_script(*CALLERS[label])
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def clean(self, directory):
        shutil.rmtree(directory, ignore_errors=True)
        (self.root / ".specify" / "feature.json").unlink(missing_ok=True)
        remove_markers(self.root, self.root.parent)
        self.environment.pop("SPECIFY_FEATURE", None)

    def assert_no_markers(self):
        self.assertEqual([], markers_under(self.root, self.root.parent))

    def test_hostile_feature_directory_is_data_and_round_trips(self):
        for label in CALLERS:
            for kind, name in HOSTILE_NAMES.items():
                with self.subTest(script=label, feature=kind):
                    directory = self.hostile_feature(name)
                    try:
                        payload = self.run_caller(label)
                        self.assertEqual(str(directory), payload["FEATURE_DIR"])
                        if "BRANCH" in payload:
                            self.assertEqual(name, payload["BRANCH"])
                        feature_json = self.root / ".specify" / "feature.json"
                        if label in PERSISTING:
                            self.assertEqual(
                                f"specs/{name}",
                                json.loads(feature_json.read_text())["feature_directory"],
                            )
                        else:
                            self.assertFalse(feature_json.exists())
                        self.assert_no_markers()
                    finally:
                        self.clean(directory)

    def test_hostile_branch_is_data_and_round_trips(self):
        self.environment["SPECIFY_FEATURE_NO_PERSIST"] = "1"
        for kind, name in HOSTILE_NAMES.items():
            with self.subTest(branch=kind):
                directory = self.hostile_feature("013-plain")
                self.environment["SPECIFY_FEATURE"] = name
                try:
                    payload = self.run_caller("check-prerequisites --paths-only --json")
                    self.assertEqual(name, payload["BRANCH"])
                    self.assertEqual(str(directory), payload["FEATURE_DIR"])
                    self.assert_no_markers()
                finally:
                    self.clean(directory)

    def mutate(self):
        common = self.script_dir / "common.sh"
        common.write_text(mutate_common(common.read_text(encoding="utf-8")), encoding="utf-8")

    def test_guard_fails_when_quoting_is_removed_for_each_caller(self):
        """Mutation proof: without %q quoting each eval caller runs the payload."""
        self.mutate()
        for label, args in CALLERS.items():
            with self.subTest(script=label):
                directory = self.hostile_feature(HOSTILE_NAMES["command-substitution"])
                try:
                    self.run_script(*args)
                    self.assertNotEqual([], markers_under(self.root, self.root.parent))
                finally:
                    self.clean(directory)

    def test_branch_guard_fails_when_quoting_is_removed(self):
        """Mutation proof for a hostile branch with a plain feature directory."""
        self.mutate()
        self.environment["SPECIFY_FEATURE"] = HOSTILE_NAMES["backticks"]
        directory = self.hostile_feature("013-plain")
        try:
            self.run_script(*CALLERS["check-prerequisites --paths-only --json"])
            self.assertNotEqual([], markers_under(self.root, self.root.parent))
        finally:
            self.clean(directory)


# Each entry: (arguments, expected outcome). "reject" must exit non-zero with an
# Error or Usage message and no output; "accept" must exit 0 with a sanitized name.
HOSTILE_ARGUMENTS = {
    "short-name-substitution": (("--short-name", "x$(touch PWNED) `touch PWNED`", "A feature"), "accept"),
    "description-semicolon": (("--short-name", "plain", "do it; touch PWNED"), "accept"),
    "empty-description": (("--short-name", "plain", ""), "reject"),
    "empty-short-name": (("--short-name", "", "A feature"), "accept"),
    "option-like-value": (("--short-name", "--json", "A feature"), "reject"),
    "option-like-description": (("--short-name", "plain", "--something"), "accept"),
    "hostile-number": (("--number", "$(touch PWNED)", "A feature"), "reject"),
    "hostile-number-backticks": (("--number", "`touch PWNED`", "A feature"), "reject"),
    **{
        f"description-{kind}": (("--short-name", "plain", name), "accept")
        for kind, name in HOSTILE_NAMES.items()
    },
}


class HostileArgumentsStayDataTest(ScriptSandboxCase):
    """create-new-feature.sh reads arguments with indirect expansion; keep them data."""

    def test_hostile_arguments_are_data_and_branch_name_is_sanitized(self):
        for kind, (args, outcome) in HOSTILE_ARGUMENTS.items():
            with self.subTest(arguments=kind):
                try:
                    result = self.run_script(
                        "create-new-feature.sh", "--json", "--dry-run", *args
                    )

                    self.assertEqual([], markers_under(self.root, self.root.parent))
                    self.assertFalse((self.root / "specs").exists())
                    if outcome == "accept":
                        self.assertEqual(0, result.returncode, result.stderr)
                        branch = json.loads(result.stdout)["BRANCH_NAME"]
                        self.assertRegex(branch, r"^[0-9]+-[a-z0-9-]*$")
                    else:
                        self.assertNotEqual(0, result.returncode)
                        self.assertEqual("", result.stdout)
                        self.assertRegex(result.stderr, r"Error|Usage")
                finally:
                    remove_markers(self.root, self.root.parent)


if __name__ == "__main__":
    unittest.main()
