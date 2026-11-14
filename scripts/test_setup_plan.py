import json
import os
from pathlib import Path
import shlex
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
PLAN_SCRIPT = SCRIPTS / "setup-plan.sh"
COMMON_SCRIPT = SCRIPTS / "common.sh"


class SetupPlanTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project with spaces"
        script_dir = self.root / ".specify" / "scripts" / "bash"
        script_dir.mkdir(parents=True)
        shutil.copy2(PLAN_SCRIPT, script_dir / "setup-plan.sh")
        shutil.copy2(COMMON_SCRIPT, script_dir / "common.sh")
        template = self.root / ".specify" / "templates" / "overrides" / "plan-template.md"
        template.parent.mkdir(parents=True)
        template.write_text("# Plan template\n", encoding="utf-8")
        self.script = script_dir / "setup-plan.sh"
        self.environment = os.environ.copy()
        for key in (
            "SPECIFY_INIT_DIR",
            "SPECIFY_FEATURE_DIRECTORY",
            "SPECIFY_FEATURE",
            "SPECIFY_FEATURE_NO_PERSIST",
        ):
            self.environment.pop(key, None)
        self.environment["SPECIFY_INIT_DIR"] = str(self.root)

    def run_setup(self, *args):
        return subprocess.run(
            ["bash", str(self.script), *args],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_hostile_names_stay_data_and_round_trip(self):
        for kind, name in HOSTILE_NAMES.items():
            with self.subTest(name=kind):
                feature = f"specs/{name}"
                feature_json = self.root / ".specify" / "feature.json"
                self.environment["SPECIFY_FEATURE_DIRECTORY"] = feature
                self.environment["SPECIFY_FEATURE"] = name
                try:
                    result = self.run_setup("--json")

                    self.assertEqual(0, result.returncode, result.stderr)
                    payload = json.loads(result.stdout)
                    self.assertEqual(str(self.root / feature), payload["FEATURE_DIR"])
                    self.assertEqual(str(self.root / feature / "plan.md"), payload["IMPL_PLAN"])
                    self.assertEqual(name, payload["BRANCH"])
                    self.assertEqual(
                        feature, json.loads(feature_json.read_text())["feature_directory"]
                    )
                    self.assertEqual([], markers_under(self.root, self.root.parent))
                finally:
                    shutil.rmtree(self.root / feature, ignore_errors=True)
                    feature_json.unlink(missing_ok=True)
                    remove_markers(self.root, self.root.parent)

    def mutate(self):
        common = self.script.parent / "common.sh"
        common.write_text(mutate_common(common.read_text(encoding="utf-8")), encoding="utf-8")

    def test_guard_fails_when_quoting_is_removed(self):
        """Mutation proof: without %q quoting setup-plan.sh runs a hostile directory name."""
        self.mutate()
        self.environment["SPECIFY_FEATURE_DIRECTORY"] = (
            "specs/" + HOSTILE_NAMES["command-substitution"]
        )

        self.run_setup("--json")

        self.assertNotEqual([], markers_under(self.root, self.root.parent))

    def test_branch_guard_fails_when_quoting_is_removed(self):
        """Mutation proof: without %q quoting setup-plan.sh runs a hostile branch name."""
        self.mutate()
        self.environment["SPECIFY_FEATURE_DIRECTORY"] = "specs/013-plain"
        self.environment["SPECIFY_FEATURE"] = HOSTILE_NAMES["backticks"]

        self.run_setup("--json")

        self.assertNotEqual([], markers_under(self.root, self.root.parent))

    def test_explicit_feature_path_is_data_and_is_persisted(self):
        marker = self.root / "unexpected-command"
        feature = "specs/013 odd '$(touch " + str(marker) + ")"
        self.environment["SPECIFY_FEATURE_DIRECTORY"] = feature
        self.environment["SPECIFY_FEATURE"] = "branch with ' quotes"

        result = self.run_setup("--json")

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(str(self.root / feature), payload["FEATURE_DIR"])
        self.assertEqual(str(self.root / feature / "spec.md"), payload["FEATURE_SPEC"])
        self.assertEqual(str(self.root / feature / "plan.md"), payload["IMPL_PLAN"])
        self.assertEqual(self.environment["SPECIFY_FEATURE"], payload["BRANCH"])
        self.assertEqual("# Plan template\n", (self.root / feature / "plan.md").read_text())
        self.assertEqual(
            feature,
            json.loads((self.root / ".specify" / "feature.json").read_text())[
                "feature_directory"
            ],
        )
        self.assertFalse(marker.exists())

    def test_feature_json_is_used_and_existing_plan_is_preserved(self):
        feature = "specs/013-existing"
        feature_dir = self.root / feature
        feature_dir.mkdir(parents=True)
        plan = feature_dir / "plan.md"
        plan.write_text("Existing plan\n", encoding="utf-8")
        (self.root / ".specify" / "feature.json").write_text(
            json.dumps({"feature_directory": feature}), encoding="utf-8"
        )

        result = self.run_setup("--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Existing plan\n", plan.read_text(encoding="utf-8"))
        self.assertEqual("013-existing", json.loads(result.stdout)["BRANCH"])
        self.assertIn("skipping template copy", result.stderr)

    def test_missing_feature_context_fails_without_creating_a_plan(self):
        result = self.run_setup("--json")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Feature directory not found", result.stderr)
        self.assertEqual("", result.stdout)
        self.assertFalse((self.root / "specs").exists())

    def test_invalid_project_override_fails_clearly(self):
        self.environment["SPECIFY_INIT_DIR"] = str(self.root / "missing")

        result = self.run_setup("--json")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("SPECIFY_INIT_DIR", result.stderr)
        self.assertEqual("", result.stdout)

    def test_no_persist_and_plain_output(self):
        feature = "specs/013 feature"
        self.environment["SPECIFY_FEATURE_DIRECTORY"] = feature
        self.environment["SPECIFY_FEATURE_NO_PERSIST"] = "1"

        result = self.run_setup()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"FEATURE_DIR: {self.root / feature}", result.stdout)
        self.assertFalse((self.root / ".specify" / "feature.json").exists())

    def test_persistence_failure_goes_unnoticed_and_setup_continues(self):
        """A failed feature.json write does not stop setup.

        The write happens inside a command substitution without errexit, so the
        shell error is printed but nothing aborts. This is accepted upstream
        behaviour, not a deliberate warning; setup still produces the plan.
        """
        self.environment["SPECIFY_FEATURE_DIRECTORY"] = "specs/new-feature"
        feature_json = self.root / ".specify" / "feature.json"
        feature_json.mkdir()

        result = self.run_setup("--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("feature.json", result.stderr)
        self.assertEqual(
            str(self.root / "specs" / "new-feature"), json.loads(result.stdout)["FEATURE_DIR"]
        )
        self.assertTrue((self.root / "specs" / "new-feature" / "plan.md").is_file())
        self.assertTrue(feature_json.is_dir())

    def test_legacy_path_output_remains_compatible(self):
        feature = "specs/013 legacy"
        self.environment["SPECIFY_FEATURE_DIRECTORY"] = feature
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; get_feature_paths --no-persist', "_", str(COMMON_SCRIPT)],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        paths = dict(
            shlex.split(line)[0].split("=", 1) for line in result.stdout.splitlines()
        )
        self.assertEqual(
            {
                "REPO_ROOT": str(self.root),
                "CURRENT_BRANCH": "013 legacy",
                "FEATURE_DIR": str(self.root / feature),
                "FEATURE_SPEC": str(self.root / feature / "spec.md"),
                "IMPL_PLAN": str(self.root / feature / "plan.md"),
                "TASKS": str(self.root / feature / "tasks.md"),
                "RESEARCH": str(self.root / feature / "research.md"),
                "DATA_MODEL": str(self.root / feature / "data-model.md"),
                "QUICKSTART": str(self.root / feature / "quickstart.md"),
                "CONTRACTS_DIR": str(self.root / feature / "contracts"),
            },
            paths,
        )


if __name__ == "__main__":
    unittest.main()
