import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))


SCRIPT = Path(__file__).with_name("project_context.py")
VALID = """\
[project]
name = "Example"
description = "An example project"

[github_project]
owner = "example-org"
number = 123
"""


class ProjectContextTest(unittest.TestCase):
    def run_with_config(self, content, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.toml"
            path.write_text(content, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(path), *args],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_valid_config_is_returned_as_json(self):
        result = self.run_with_config(VALID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "project": {"name": "Example", "description": "An example project"},
                "github_project": {"owner": "example-org", "number": 123},
            },
        )

    def test_missing_and_invalid_values_fail_explicitly(self):
        for invalid, expected in (
            (VALID.replace('name = "Example"\n', ""), "project.name"),
            (VALID.replace('owner = "example-org"', 'owner = ""'), "github_project.owner"),
            (VALID.replace('owner = "example-org"', 'owner = "-bad"'), "github_project.owner"),
            (VALID.replace("number = 123", "number = 0"), "github_project.number"),
            (VALID.replace("number = 123", "number = true"), "github_project.number"),
            (VALID + "\nunexpected = 1\n", "unexpected"),
            ("[project\n", "TOML"),
        ):
            with self.subTest(expected=expected, content=invalid):
                result = self.run_with_config(invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_missing_config_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.toml"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.toml", result.stderr)
        self.assertIn("run `python3 scripts/bootstrap.py init`", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_default_config_is_found_outside_the_repository(self):
        # The default path is resolved from the script's location, not the working directory.
        with tempfile.TemporaryDirectory() as repository, tempfile.TemporaryDirectory() as elsewhere:
            scripts = Path(repository) / "scripts"
            scripts.mkdir()
            script = scripts / SCRIPT.name
            script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            (Path(repository) / "project-context.toml").write_text(VALID, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=elsewhere,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["github_project"]["number"], 123)

    def test_example_config_is_valid(self):
        example = SCRIPT.parent.parent / "project-context.example.toml"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(example)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_project_owner_must_match_repository_owner_for_scoped_tokens(self):
        matching = self.run_with_config(VALID, "--repository-owner", "EXAMPLE-ORG")
        self.assertEqual(matching.returncode, 0, matching.stderr)

        mismatched = self.run_with_config(VALID, "--repository-owner", "another-org")
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertIn("cross-owner Projects are not supported", mismatched.stderr)

    def test_the_retired_toolkit_section_is_rejected(self):
        result = self.run_with_config(VALID + "\n[toolkit]\nexport_source = true\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown sections: toolkit", result.stderr)

    def test_the_retired_toolkit_export_source_flag_is_rejected(self):
        result = self.run_with_config(VALID, "--toolkit-export-source")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments: --toolkit-export-source", result.stderr)

    def test_example_config_has_no_toolkit_section(self):
        example = SCRIPT.parent.parent / "project-context.example.toml"
        self.assertNotIn("toolkit", example.read_text(encoding="utf-8"))

    def test_unknown_or_missing_sections_are_named(self):
        misspelled = self.run_with_config(VALID + "\n[reveiw]\nrequired_approvals = 1\n")
        self.assertNotEqual(misspelled.returncode, 0)
        self.assertIn("unknown sections: reveiw", misspelled.stderr)
        self.assertIn("required sections: project, github_project", misspelled.stderr)
        self.assertIn("optional sections: review", misspelled.stderr)

        missing = self.run_with_config('[project]\nname = "Example"\ndescription = "x"\n')
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing sections: github_project", missing.stderr)
        self.assertIn("required sections: project, github_project", missing.stderr)


class ReviewSettingTest(unittest.TestCase):
    def run_with_config(self, content, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.toml"
            path.write_text(content, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(path), *args],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_review_is_optional_and_appears_in_the_json_when_set(self):
        result = self.run_with_config(VALID + "\n[review]\nrequired_approvals = 0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["review"], {"required_approvals": 0})

    def test_required_approvals_flag_prints_the_value_or_the_default(self):
        for content, expected in (
            (VALID, "1"),
            (VALID + "\n[review]\n", "1"),
            (VALID + "\n[review]\nrequired_approvals = 0\n", "0"),
            (VALID + "\n[review]\nrequired_approvals = 2\n", "2"),
        ):
            with self.subTest(content=content):
                result = self.run_with_config(content, "--required-approvals")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_invalid_review_settings_are_rejected(self):
        for invalid, expected in (
            (VALID + "\n[review]\nrequired_approvals = true\n", "review.required_approvals"),
            (VALID + "\n[review]\nrequired_approvals = -1\n", "review.required_approvals"),
            (VALID + '\n[review]\nrequired_approvals = "1"\n', "review.required_approvals"),
            (VALID + "\n[review]\nrequired_approvals = 1.5\n", "review.required_approvals"),
            (VALID + "\n[review]\nrequired_reviewers = 1\n", "review keys"),
            (VALID + "\nreview = 1\n", "review"),
        ):
            with self.subTest(content=invalid):
                result = self.run_with_config(invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_example_config_documents_the_review_setting(self):
        example = SCRIPT.parent.parent / "project-context.example.toml"
        self.assertIn("required_approvals", example.read_text(encoding="utf-8"))


class ExclusiveFlagsTest(unittest.TestCase):
    def test_mode_flags_cannot_be_combined(self):
        for flags in (
            ("--bootstrap-state", "--required-approvals"),
            ("--bootstrap-state", "--board-owner-or-skip"),
        ):
            with self.subTest(flags=flags):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), *flags],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("not allowed with", result.stderr)

    def test_repository_owner_error_does_not_claim_scoped_tokens(self):
        import project_context

        with self.assertRaises(ValueError) as raised:
            project_context.validate_repository_owner(
                {"github_project": {"owner": "a"}}, "b"
            )
        self.assertNotIn("scoped", str(raised.exception))
        self.assertIn("must match the repository owner", str(raised.exception))


class BootstrapStateTest(unittest.TestCase):
    def state(self, *paths):
        """Create the given files (or directories, with a trailing slash) and ask for the state."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "project_context.py").write_text("", encoding="utf-8")
            for name in paths:
                target = root / name
                if name.endswith("/"):
                    target.mkdir(parents=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("", encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--bootstrap-state", "--root", str(root)],
                capture_output=True,
                text=True,
                check=False,
            )

    def assert_state(self, expected, *paths):
        result = self.state(*paths)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected + "\n", paths)

    def test_a_repository_without_bootstrap_artifacts_is_uninitialized(self):
        self.assert_state("uninitialized")
        self.assert_state("uninitialized", "specs/")
        self.assert_state("uninitialized", "specs/.gitkeep", "README.md")

    def test_any_bootstrap_artifact_makes_it_initialized(self):
        # A half-initialized repository is initialized, so a deleted file cannot silence the checks.
        self.assert_state("initialized", "ROADMAP.md")
        self.assert_state("initialized", "project-context.toml")
        self.assert_state("initialized", ".specify/memory/constitution.md")
        self.assert_state("initialized", "specs/001-x/")
        self.assert_state("initialized", "specs/001-x/", "project-context.toml")

    def test_a_wrong_root_is_an_error_not_uninitialized(self):
        with tempfile.TemporaryDirectory() as directory:
            for root in (Path(directory) / "typo", Path(directory)):
                with self.subTest(root=root):
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT), "--bootstrap-state", "--root", str(root)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("not a repository root", result.stderr)
                    self.assertIn(str(root), result.stderr)

    def test_messages_use_no_slash_style_skill_names(self):
        import project_context

        for message in (project_context.NOT_INITIALIZED_MESSAGE, project_context.ROADMAP_MISSING_HINT):
            self.assertNotIn("/roadmap-write", message)
        self.assertIn("`roadmap-write` skill", project_context.ROADMAP_MISSING_HINT)

    def test_the_helper_names_the_missing_file_and_bootstrap_guide(self):
        import project_context

        self.assertIn("docs/bootstrap.md", project_context.NOT_INITIALIZED_MESSAGE)
        self.assertIn("ROADMAP.md", project_context.NOT_INITIALIZED_MESSAGE)


class BoardOwnerOrSkipTest(unittest.TestCase):
    """`--board-owner-or-skip`: the owner on stdout; notices and errors for the workflow on stderr."""

    def run_mode(self, *, files=(), app="false", pat="false", owner="example-org", config=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "project_context.py").write_text("", encoding="utf-8")
            for name in files:
                (root / name).write_text("", encoding="utf-8")
            if config is not None:
                (root / "project-context.toml").write_text(config, encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--board-owner-or-skip",
                    "--root", str(root), "--config", str(root / "project-context.toml"),
                    "--repository-owner", owner,
                ],
                capture_output=True,
                text=True,
                check=False,
                env={"PATH": "/usr/bin:/bin", "HAS_BOARD_APP": app, "HAS_BOARD_PAT": pat},
            )

    def test_without_a_credential_it_names_both_options_and_prints_no_owner(self):
        for files, config in ((), None), (("ROADMAP.md",), VALID):
            with self.subTest(files=files):
                result = self.run_mode(files=files, config=config)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("::notice::Board sync skipped", result.stderr)
                self.assertIn("PROJECT_STATUS_APP_ID", result.stderr)
                self.assertIn("PROJECT_BOARD_TOKEN", result.stderr)

    def test_before_bootstrap_it_skips_with_a_notice(self):
        result = self.run_mode(app="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("::notice::Board sync skipped", result.stderr)
        self.assertIn("project-context.toml", result.stderr)
        self.assertIn("docs/bootstrap.md", result.stderr)
        self.assertNotIn("Board sync uses", result.stderr)

    def test_once_initialized_a_missing_file_is_an_error(self):
        result = self.run_mode(app="true", files=("ROADMAP.md",))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("::error::Missing project context", result.stderr)
        self.assertIn("python3 scripts/bootstrap.py init", result.stderr)

    def test_it_prints_the_owner_and_names_the_credential_in_use(self):
        for app, pat, used, other in (
            ("true", "false", "the GitHub App", "PROJECT_BOARD_TOKEN"),
            ("false", "true", "PROJECT_BOARD_TOKEN", "GitHub App"),
            ("true", "true", "the GitHub App", "PROJECT_BOARD_TOKEN"),
        ):
            with self.subTest(app=app, pat=pat):
                result = self.run_mode(app=app, pat=pat, config=VALID)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "example-org\n")
                self.assertIn(f"::notice::Board sync uses {used}", result.stderr)
                self.assertNotIn(other, result.stderr)

    def test_an_owner_other_than_the_repository_owner_fails(self):
        result = self.run_mode(app="true", config=VALID, owner="another-org")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("must match the repository owner", result.stderr)

    def test_an_invalid_file_fails(self):
        result = self.run_mode(app="true", config="[project\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("TOML", result.stderr)
        self.assertTrue(result.stderr.startswith("::error::"), result.stderr)

    def test_an_owner_mismatch_is_an_error_annotation_and_not_a_toml_error(self):
        result = self.run_mode(app="true", config=VALID, owner="another-org")
        self.assertTrue(result.stderr.startswith("::error::github_project.owner"), result.stderr)
        self.assertNotIn("TOML", result.stderr)

    def test_the_repository_owner_is_required(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--board-owner-or-skip"],
            capture_output=True, text=True, check=False,
            env={"PATH": "/usr/bin:/bin", "HAS_BOARD_APP": "true"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--repository-owner", result.stderr)

    def test_the_mode_cannot_be_combined_with_the_others(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--board-owner-or-skip", "--required-approvals"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
