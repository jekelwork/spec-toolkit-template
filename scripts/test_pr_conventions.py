import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import pr_conventions

SCRIPT = Path(__file__).with_name("pr_conventions.py")


class PrConventionsTest(unittest.TestCase):
    def test_rejects_legacy_task_branch_work_id(self):
        with self.assertRaises(pr_conventions.ConventionError):
            pr_conventions.parse_branch("22-t060-hold-security-review")

    def test_three_digit_task_branch_is_only_a_spec_branch(self):
        # "123-t001-foo" is no longer an issue branch; it parses as a Spec Kit
        # branch and is rejected unless it changes files under specs/123-*/.
        metadata = pr_conventions.parse_branch("123-t001-foo")

        self.assertEqual("spec", metadata.branch_type)
        with self.assertRaisesRegex(pr_conventions.ConventionError, "must change a file"):
            pr_conventions.validate_spec_branch_changes(metadata, ["src/app.py"])

    def test_parses_non_spec_maintenance_branch(self):
        metadata = pr_conventions.parse_branch(
            "107-chore-enforce-issue-backed-branches"
        )

        self.assertEqual("issue", metadata.branch_type)
        self.assertEqual("chore", metadata.work_id)

    def test_parses_spec_kit_generated_branch(self):
        metadata = pr_conventions.parse_branch("002-example-pipeline")

        self.assertEqual("spec", metadata.branch_type)
        self.assertEqual(2, metadata.feature_number)
        self.assertEqual("example-pipeline", metadata.slug)

    def test_parses_zero_padded_spec_feature_branch(self):
        metadata = pr_conventions.parse_branch("005-example-workspace")

        self.assertEqual("spec", metadata.branch_type)
        self.assertEqual(5, metadata.feature_number)
        self.assertEqual("005", metadata.feature_id)

    def test_parses_unpadded_spec_feature_branch_at_100(self):
        metadata = pr_conventions.parse_branch("100-roadmap-improvements")

        self.assertEqual("spec", metadata.branch_type)
        self.assertEqual(100, metadata.feature_number)
        self.assertEqual("100", metadata.feature_id)

    def test_spec_branch_rejects_changes_outside_its_numbered_directory(self):
        metadata = pr_conventions.parse_branch("422-feat-accept-ids")

        with self.assertRaisesRegex(
            pr_conventions.ConventionError, "must change a file under"
        ):
            pr_conventions.validate_spec_branch_changes(
                metadata,
                ["README.md", "specs/004-example-state/spec.md"],
            )

    def test_spec_branch_rejects_file_without_a_directory(self):
        metadata = pr_conventions.parse_branch("100-roadmap-improvements")

        with self.assertRaisesRegex(
            pr_conventions.ConventionError, "must change a file under"
        ):
            pr_conventions.validate_spec_branch_changes(
                metadata, ["specs/100-roadmap-improvements"]
            )

    def test_spec_branch_rejects_path_with_parent_directory_segment(self):
        metadata = pr_conventions.parse_branch("100-roadmap-improvements")

        with self.assertRaisesRegex(
            pr_conventions.ConventionError, "must change a file under"
        ):
            pr_conventions.validate_spec_branch_changes(
                metadata, ["specs/../specs/100-roadmap-improvements/spec.md"]
            )

    def test_spec_branch_accepts_changes_in_its_numbered_directory(self):
        metadata = pr_conventions.parse_branch("100-roadmap-improvements")

        pr_conventions.validate_spec_branch_changes(
            metadata, ["specs/100-roadmap-improvements/spec.md"]
        )

    def test_spec_branch_accepts_zero_padded_directory(self):
        metadata = pr_conventions.parse_branch("005-example-workspace")

        pr_conventions.validate_spec_branch_changes(
            metadata, ["specs/005-example-workspace/spec.md"]
        )

    def test_spec_branch_preserves_over_padded_feature_id(self):
        metadata = pr_conventions.parse_branch("0005-example-workspace")

        pr_conventions.validate_spec_branch_changes(
            metadata, ["specs/0005-example-workspace/spec.md"]
        )
        with self.assertRaises(pr_conventions.ConventionError):
            pr_conventions.validate_spec_branch_changes(
                metadata, ["specs/005-example-workspace/spec.md"]
            )

    def test_rejects_spec_branch_with_fewer_than_three_digits(self):
        with self.assertRaises(pr_conventions.ConventionError):
            pr_conventions.parse_branch("07-enforce-branches")

    def test_parses_phase_issue_branch(self):
        metadata = pr_conventions.parse_branch(
            "81-phase-2-foundational-blocking-prerequisites"
        )

        self.assertEqual("phase", metadata.branch_type)
        self.assertEqual(81, metadata.issue_number)
        self.assertEqual(2, metadata.phase_number)
        self.assertEqual("foundational-blocking-prerequisites", metadata.slug)

    def test_parses_github_suggested_phase_branch(self):
        metadata = pr_conventions.parse_branch(
            "82-first-feature---phase-3-user-story-1---"
            "sign-up-for-an-account-priority-p1-mvp"
        )

        self.assertEqual("phase", metadata.branch_type)
        self.assertEqual(82, metadata.issue_number)
        self.assertEqual(3, metadata.phase_number)
        self.assertEqual(
            "user-story-1---sign-up-for-an-account-priority-p1-mvp",
            metadata.slug,
        )

    def test_rejects_branch_without_numeric_issue_prefix(self):
        with self.assertRaisesRegex(
            pr_conventions.ConventionError, "Branch must match"
        ):
            pr_conventions.parse_branch("feature-add-sign-in")

    def test_rejects_branch_with_unrecognised_work_id(self):
        with self.assertRaises(pr_conventions.ConventionError):
            pr_conventions.parse_branch("22-task-hold-security-review")

    def test_rejects_non_kebab_case_slug(self):
        with self.assertRaises(pr_conventions.ConventionError):
            pr_conventions.parse_branch("22-chore-Hold_Security_Review")

    def test_validates_issue_title_and_state(self):
        metadata = pr_conventions.parse_branch("22-chore-hold-security-review")

        pr_conventions.validate_issue(
            metadata, "CHORE: Hold the design-level security review", "OPEN"
        )

    def test_accepts_maintenance_issue_prefix_regardless_of_case(self):
        for work_id in ("chore", "ci", "docs", "fix", "refactor", "spec", "test"):
            metadata = pr_conventions.parse_branch(f"107-{work_id}-update-tooling")
            for prefix in (work_id, work_id.upper(), work_id.capitalize()):
                with self.subTest(work_id=work_id, prefix=prefix):
                    pr_conventions.validate_issue(
                        metadata, f"{prefix}: Update tooling", "OPEN"
                    )

    def test_rejects_mismatched_or_malformed_maintenance_issue_prefix(self):
        metadata = pr_conventions.parse_branch("107-chore-update-tooling")
        for title in ("fix: Update tooling", "chore Update tooling", "chore:", "chore: "):
            with self.subTest(title=title):
                with self.assertRaises(pr_conventions.ConventionError):
                    pr_conventions.validate_issue(metadata, title, "OPEN")

    def test_validates_uppercase_maintenance_issue_prefix(self):
        metadata = pr_conventions.parse_branch(
            "107-chore-enforce-issue-backed-branches"
        )

        pr_conventions.validate_issue(
            metadata,
            "CHORE: Enforce issue-backed branches",
            "OPEN",
        )

    def test_validates_phase_issue_title(self):
        metadata = pr_conventions.parse_branch(
            "81-phase-2-foundational-blocking-prerequisites"
        )

        pr_conventions.validate_issue(
            metadata,
            "User Sign-In - Phase 2: Foundational (Blocking Prerequisites)",
            "OPEN",
        )

    def test_validates_github_suggested_phase_branch_title(self):
        metadata = pr_conventions.parse_branch(
            "82-first-feature---phase-3-user-story-1---"
            "sign-up-for-an-account-priority-p1-mvp"
        )

        pr_conventions.validate_issue(
            metadata,
            "User Sign-In - Phase 3: User Story 1 - Sign up for an account "
            "(Priority: P1) 🎯 MVP",
            "OPEN",
        )

    def test_rejects_mismatched_phase_issue_title(self):
        metadata = pr_conventions.parse_branch("81-phase-2-foundational-prerequisites")

        with self.assertRaisesRegex(
            pr_conventions.ConventionError, "must contain 'Phase 2:'"
        ):
            pr_conventions.validate_issue(
                metadata,
                "User Sign-In - Phase 3: Foundational",
                "OPEN",
            )

    def test_rejects_closed_issue(self):
        metadata = pr_conventions.parse_branch("22-chore-hold-security-review")

        with self.assertRaisesRegex(pr_conventions.ConventionError, "must be open"):
            pr_conventions.validate_issue(
                metadata,
                "CHORE: Hold the design-level security review",
                "CLOSED",
            )

    def test_appends_closing_reference(self):
        body, changed = pr_conventions.ensure_closing_reference(
            "## Summary\n\nAdds validation.\n", 107
        )

        self.assertTrue(changed)
        self.assertEqual(
            "## Summary\n\nAdds validation.\n\nCloses #107\n",
            body,
        )

    def test_does_not_duplicate_supported_closing_reference(self):
        original = "## Summary\n\nResolves #107\n"

        body, changed = pr_conventions.ensure_closing_reference(original, 107)

        self.assertFalse(changed)
        self.assertEqual(original, body)

    def test_does_not_duplicate_github_closing_keywords(self):
        for keyword in (
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ):
            with self.subTest(keyword=keyword):
                original = f"{keyword.title()} #107\n"

                body, changed = pr_conventions.ensure_closing_reference(
                    original, 107
                )

                self.assertFalse(changed)
                self.assertEqual(original, body)

    def test_does_not_duplicate_colon_style_closing_reference(self):
        original = "Closes: #107\n"

        body, changed = pr_conventions.ensure_closing_reference(original, 107)

        self.assertFalse(changed)
        self.assertEqual(original, body)

    def test_does_not_duplicate_same_repository_closing_reference(self):
        original = "Closes example-org/repository#107\n"

        body, changed = pr_conventions.ensure_closing_reference(
            original, 107, "example-org/repository"
        )

        self.assertFalse(changed)
        self.assertEqual(original, body)

    def test_ignores_closing_reference_to_another_repository(self):
        body, changed = pr_conventions.ensure_closing_reference(
            "Closes another/repository#107", 107
        )

        self.assertTrue(changed)
        self.assertEqual(
            "Closes another/repository#107\n\nCloses #107\n",
            body,
        )

    def test_does_not_confuse_issue_number_prefixes(self):
        body, changed = pr_conventions.ensure_closing_reference("Closes #1070", 107)

        self.assertTrue(changed)
        self.assertEqual("Closes #1070\n\nCloses #107\n", body)


SHA = "a" * 40
ZERO_SHA = "0" * 40


class CheckPushedBranchesTest(unittest.TestCase):
    def test_accepts_valid_pushed_branches(self):
        errors = pr_conventions.check_pushed_branches(
            [
                f"refs/heads/x {SHA} refs/heads/107-chore-enforce-branches {ZERO_SHA}",
                f"refs/heads/y {SHA} refs/heads/002-example-pipeline {SHA}",
                f"refs/heads/z {SHA} refs/heads/81-phase-2-foundational-setup {SHA}",
            ]
        )

        self.assertEqual([], errors)

    def test_rejects_invalid_remote_branch_name(self):
        errors = pr_conventions.check_pushed_branches(
            [f"refs/heads/my-feature {SHA} refs/heads/my-feature {ZERO_SHA}\n"]
        )

        self.assertEqual(1, len(errors))
        self.assertIn("'my-feature'", errors[0])

    def test_checks_remote_name_not_local_name(self):
        errors = pr_conventions.check_pushed_branches(
            [f"refs/heads/107-chore-ok {SHA} refs/heads/wip {ZERO_SHA}"]
        )

        self.assertEqual(1, len(errors))
        self.assertIn("'wip'", errors[0])

    def test_skips_deletions_tags_main_and_blank_lines(self):
        errors = pr_conventions.check_pushed_branches(
            [
                f"(delete) {ZERO_SHA} refs/heads/old-branch {SHA}",
                f"refs/tags/v1 {SHA} refs/tags/v1 {ZERO_SHA}",
                f"refs/heads/main {SHA} refs/heads/main {SHA}",
                "",
            ]
        )

        self.assertEqual([], errors)

    def test_remote_name_decides_main_exemption(self):
        errors = pr_conventions.check_pushed_branches(
            [f"refs/heads/main {SHA} refs/heads/wip {ZERO_SHA}"]
        )

        self.assertEqual(1, len(errors))
        self.assertIn("'wip'", errors[0])

        self.assertEqual(
            [],
            pr_conventions.check_pushed_branches(
                [f"refs/heads/wip {SHA} refs/heads/main {SHA}"]
            ),
        )

    def test_rejects_malformed_line(self):
        with self.assertRaises(pr_conventions.ConventionError):
            pr_conventions.check_pushed_branches(["refs/heads/x"])


class CheckPushCommandTest(unittest.TestCase):
    def run_check_push(self, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "check-push"],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exits_non_zero_and_names_invalid_branch(self):
        result = self.run_check_push(
            f"refs/heads/x {SHA} refs/heads/my-feature {ZERO_SHA}\n"
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("'my-feature'", result.stderr)

    def test_exits_zero_for_valid_or_empty_input(self):
        valid = f"refs/heads/x {SHA} refs/heads/107-chore-ok {ZERO_SHA}\n"

        self.assertEqual(0, self.run_check_push(valid).returncode)
        self.assertEqual(0, self.run_check_push("").returncode)


class ValidateSpecBranchCommandTest(unittest.TestCase):
    def run_branch_command(
        self, command: str, branch: str, changed_paths: list[str]
    ) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            changed_paths_file = Path(directory) / "changed-paths.txt"
            changed_paths_file.write_text(
                "\n".join(changed_paths), encoding="utf-8"
            )
            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    command,
                    branch,
                    "--changed-paths-file",
                    str(changed_paths_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_matching_spec_directory_disambiguates_issue_like_spec_slug(self):
        result = self.run_branch_command(
            "parse-pr-branch",
            "100-spec-create-feature",
            ["specs/100-create-feature/spec.md"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("spec", json.loads(result.stdout)["branch_type"])

    def test_matching_spec_directory_disambiguates_phase_like_spec_slug(self):
        result = self.run_branch_command(
            "parse-pr-branch",
            "100-phase-2-rollout",
            ["specs/100-rollout/spec.md"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("spec", json.loads(result.stdout)["branch_type"])

    def test_issue_branch_without_matching_spec_directory_stays_issue(self):
        result = self.run_branch_command(
            "parse-pr-branch",
            "422-chore-update-branch-check",
            ["README.md"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("issue", json.loads(result.stdout)["branch_type"])

    def test_phase_branch_without_matching_spec_directory_stays_phase(self):
        result = self.run_branch_command(
            "parse-pr-branch",
            "373-my-feature-phase-2-sync-board",
            ["README.md"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        metadata = json.loads(result.stdout)
        self.assertEqual("phase", metadata["branch_type"])
        self.assertEqual(373, metadata["issue_number"])
        self.assertEqual(2, metadata["phase_number"])

    def test_accepts_matching_changed_spec_directory(self):
        result = self.run_branch_command(
            "validate-spec-branch",
            "005-example-workspace",
            ["specs/005-example-workspace/spec.md"],
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_rejects_changes_without_matching_spec_directory(self):
        result = self.run_branch_command(
            "validate-spec-branch",
            "422-feat-accept-ids",
            ["README.md", "specs/004-example-state/spec.md"],
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Spec Kit feature 422", result.stderr)

    def test_rejects_non_spec_branch(self):
        result = self.run_branch_command(
            "validate-spec-branch", "422-chore-update-branch-check", []
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Only Spec Kit branches", result.stderr)


if __name__ == "__main__":
    unittest.main()


class PhaseReferenceTest(unittest.TestCase):
    def test_appends_a_non_closing_reference(self):
        body, changed = pr_conventions.ensure_phase_reference("## Summary\n\nPhase work.\n", 11)

        self.assertTrue(changed)
        self.assertEqual("## Summary\n\nPhase work.\n\nPart of #11\n", body)
        self.assertFalse(pr_conventions.has_closing_reference(body, 11))

    def test_keeps_an_existing_part_of_reference(self):
        original = "Part of #11\n"

        self.assertEqual((original, False), pr_conventions.ensure_phase_reference(original, 11))

    def test_keeps_other_non_closing_references(self):
        for original in ("Refs #11\n", "Related to #11\n", "Relates to #11\n"):
            with self.subTest(original=original):
                self.assertEqual(
                    (original, False), pr_conventions.ensure_phase_reference(original, 11)
                )

    def test_leaves_an_author_written_closing_keyword_alone(self):
        # The Phase sync reopens a Phase with unchecked tasks, so it is not rewritten.
        original = "Closes #11\n"

        self.assertEqual((original, False), pr_conventions.ensure_phase_reference(original, 11))

    def test_a_reference_to_another_issue_does_not_count(self):
        body, changed = pr_conventions.ensure_phase_reference("Part of #110\n", 11)

        self.assertTrue(changed)
        self.assertTrue(body.endswith("Part of #11\n"))

    def test_update_body_phase_flag_writes_part_of(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "body.md"
            output = Path(directory) / "out.md"
            source.write_text("Phase work.\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "update-body", "11", "--phase",
                 "--body-file", str(source), "--output-file", str(output)],
                capture_output=True, text=True, check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("updated", result.stdout.strip())
            self.assertIn("Part of #11", output.read_text(encoding="utf-8"))
            self.assertNotIn("Closes", output.read_text(encoding="utf-8"))

    def test_the_workflow_passes_phase_for_phase_branches_only(self):
        text = (SCRIPT.parent.parent / ".github/workflows/pr-conventions.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("steps.branch.outputs.branch-type == 'phase'", text)
        self.assertIn("--phase", text)
