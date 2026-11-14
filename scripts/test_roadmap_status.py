import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_roadmap_dependencies as dependencies
import roadmap_issues
import roadmap_status
from fake_github import FakeGitHub, OWNER, REPO


PROJECT_CONTEXT = """\
[project]
name = "Example"
description = "An example project"

[github_project]
owner = "example-org"
number = 123
"""


def roadmap_text(status="planned"):
    return (
        "## Planned Specs\n\n"
        f"### 900 — Requested  [status: {status}]\n"
        "- **Depends on:** 901\n- **Notes:** Check merged schema evidence.\n\n"
        "### 901 — Prerequisite  [status: planned]\n"
        "- **Depends on:** none\n- **Notes:** Keep milestone qualifications.\n"
    )


def epic(number=10, state="closed", reason="completed", marker=None):
    return {
        "number": number, "state": state, "state_reason": reason,
        "labels": ["Epic"],
        "body": marker or roadmap_issues.roadmap_marker("901"),
    }


class RoadmapStatusTest(unittest.TestCase):
    def setUp(self):
        self.entries = dependencies.parse_roadmap(roadmap_text(), {})

    def test_unlabelled_spoof_cannot_complete_a_prerequisite(self):
        for labels in ([], ["epic"], ["Epic copy"]):
            spoof = {**epic(), "labels": labels}
            with self.subTest(labels=labels), contextlib.redirect_stderr(io.StringIO()) as errors:
                epics = roadmap_status.resolve_epics(self.entries, [spoof])
                self.assertEqual(roadmap_status.evaluate_status(self.entries, "900", epics)["result"], "blocked")
                self.assertIn("Warning:", errors.getvalue())

    def test_unlabelled_duplicate_does_not_hide_labelled_epic(self):
        marker = roadmap_issues.roadmap_marker("901")
        with contextlib.redirect_stderr(io.StringIO()):
            real = epic(marker=marker)
            spoof = {**epic(11, marker=marker), "labels": []}
            self.assertEqual(roadmap_status.resolve_epics(self.entries, [real, spoof])["901"], real)

    def test_invalid_epic_label_metadata_fails_closed(self):
        for labels in (None, "Epic", [{}], [1]):
            with self.subTest(labels=labels), self.assertRaises(dependencies.InvalidData):
                roadmap_status.resolve_epics(self.entries, [{**epic(), "labels": labels}])

    def test_completion_comes_from_github_not_plan_status(self):
        for state, reason, satisfied in (
            ("closed", "completed", True),
            ("closed", "not_planned", False),
            ("open", None, False),
        ):
            with self.subTest(state=state, reason=reason):
                result = roadmap_status.evaluate_status(
                    self.entries, "900", {"901": epic(state=state, reason=reason)}
                )
                self.assertEqual(result["dependencies"][0]["satisfied"], satisfied)
                self.assertEqual(result["result"], "ready" if satisfied else "blocked")
                self.assertEqual(result["dependencies"][0]["notes"], "Keep milestone qualifications.")
                baseline = dependencies.evaluate(self.entries, "900")
                self.assertEqual(set(result), set(baseline))
                self.assertEqual(set(result["entry"]), set(baseline["entry"]))
                self.assertEqual(set(result["dependencies"][0]), set(baseline["dependencies"][0]))

    def test_missing_epic_is_unsatisfied_and_keeps_identity(self):
        result = roadmap_status.evaluate_status(self.entries, "900", {})
        self.assertEqual(result["result"], "blocked")
        prerequisite = result["dependencies"][0]
        self.assertEqual((prerequisite["id"], prerequisite["title"]), ("901", "Prerequisite"))
        self.assertIn("missing", prerequisite["condition"])

    def test_feature_path_marker_alone_is_not_an_epic(self):
        issues = [epic(marker="<!-- speckit-feature: specs/901-prerequisite -->")]
        self.assertIsNone(roadmap_status.resolve_epics(self.entries, issues)["901"])

    def test_invalid_closed_state_does_not_imply_completion(self):
        with self.assertRaises(dependencies.InvalidData):
            roadmap_status.resolve_epics(self.entries, [epic(reason=None)])

    def test_rows_include_order_state_url_and_blockers(self):
        rows = roadmap_status.status_rows(
            self.entries, {"901": epic()}, "example", "repository", {"901": "Done"}
        )
        self.assertEqual([row["id"] for row in rows], ["900", "901"])
        self.assertEqual(rows[1]["epic"], 10)
        self.assertEqual(rows[1]["epic_state"], "closed completed")
        self.assertEqual(rows[1]["board_status"], "Done")
        self.assertEqual(rows[1]["url"], "https://github.com/example/repository/issues/10")
        self.assertEqual(rows[0]["open_blockers"], [])
        self.assertEqual(
            roadmap_status.status_rows(self.entries, {}, "example", "repository")[0]["open_blockers"],
            ["901"],
        )

    def run_command(self, argv, issues=(), failure=None, board_failure=None, text=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            path.write_text(text if text is not None else roadmap_text(), encoding="utf-8")
            context = Path(directory) / "project-context.toml"
            context.write_text(PROJECT_CONTEXT, encoding="utf-8")
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(roadmap_status.sync.project_context, "DEFAULT_CONFIG", context),
                patch.object(roadmap_status.sync, "origin_repo", return_value=("example", "repository")) as origin,
                patch.object(roadmap_status.sync, "load_issues", return_value=list(issues), side_effect=failure) as load_issues,
                patch.object(roadmap_status.project_board, "project_item_details", side_effect=board_failure),
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
            ):
                self.origin_repo_mock, self.load_issues_mock = origin, load_issues
                code = roadmap_status.main(["--roadmap", str(path), *argv])
            return code, output.getvalue(), errors.getvalue()

    def test_cli_dependency_exit_codes_and_api_errors(self):
        for issues, expected in (([], 1), ([epic()], 0)):
            code, output, _ = self.run_command(["--id", "900", "--json"], issues)
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(output)["result"], "ready" if expected == 0 else "blocked")
        code, output, _ = self.run_command(
            ["--id", "900", "--json"], failure=SystemExit("API permission denied")
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["result"], "error")
        self.assertIn("API permission denied", output)

    def test_optional_board_failure_is_visible_not_required_data_failure(self):
        code, output, errors = self.run_command(
            ["--json"], [epic()], board_failure=SystemExit("Project access denied")
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)), 2)
        self.assertIn("Project access denied", errors)
        self.assertIn("Warning:", errors)

    def test_invalid_ids_and_non_plan_statuses_fail_clearly(self):
        code, output, _ = self.run_command(["--id", "1", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["result"], "error")
        with self.assertRaises(dependencies.InvalidData):
            dependencies.parse_roadmap(roadmap_text("implemented"), {}, statuses=roadmap_issues.PLAN_STATUSES)

    def test_duplicate_managed_epics_return_cli_error_json(self):
        code, output, _ = self.run_command(
            ["--id", "900", "--json"], [epic(), epic(11)]
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["result"], "error")
        self.assertIn("901", json.loads(output)["diagnostics"][0])

    def test_feature_path_marker_does_not_satisfy_cli_dependency(self):
        issue = epic(marker="<!-- speckit-feature: specs/901-prerequisite -->")
        code, output, _ = self.run_command(["--id", "900", "--json"], [issue])
        self.assertFalse(json.loads(output)["dependencies"][0]["satisfied"])
        self.assertIn("epic missing", json.dumps(json.loads(output)))

    def test_non_plan_status_returns_cli_error_before_github_reads(self):
        with patch.object(roadmap_status, "resolve_epics") as resolve:
            code, output, _ = self.run_command(
                ["--id", "900", "--json"], text=roadmap_text("implemented")
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["result"], "error")
        resolve.assert_not_called()
        self.origin_repo_mock.assert_not_called()
        self.load_issues_mock.assert_not_called()

    def test_duplicate_planning_markers_do_not_affect_completion(self):
        planning = roadmap_issues.planning_marker("901", "spec-plan")
        code, output, _ = self.run_command(
            ["--id", "900", "--json"],
            [epic(), epic(11, marker=planning), epic(12, marker=planning)],
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["result"], "ready")

    def test_unrelated_bad_epic_does_not_block_dependency_mode(self):
        text = roadmap_text() + (
            "\n### 902 — Unrelated  [status: planned]\n- **Depends on:** none\n"
        )
        unrelated = epic(11, reason=None, marker=roadmap_issues.roadmap_marker("902"))
        code, output, _ = self.run_command(
            ["--id", "900", "--json"], [epic(), unrelated], text=text
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["result"], "ready")
        code, output, _ = self.run_command(
            ["--skip-project", "--json"], [epic(), unrelated], text=text
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["result"], "error")

    def test_invalid_optional_board_configuration_warns_without_failing_listing(self):
        for error in (
            ValueError("Invalid Project configuration"),
            FileNotFoundError("Missing Project configuration"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(
                    roadmap_status.sync.project_context, "load_context", side_effect=error,
                ):
                    code, output, errors = self.run_command(["--json"], [epic()])
                self.assertEqual(code, 0)
                self.assertEqual(len(json.loads(output)), 2)
                self.assertIn("Warning:", errors)
                self.assertIn(str(error), errors)

    def test_table_output_has_required_columns(self):
        code, output, _ = self.run_command(["--skip-project"], [epic()])
        self.assertEqual(code, 0)
        self.assertEqual(
            output.splitlines()[0],
            "ID\tTitle\tPlan status\tEpic\tEpic state\tBoard Status\tURL\tOpen blockers",
        )
        self.assertEqual(len(output.splitlines()), 3)

    def test_real_issue_loader_is_read_only(self):
        fake = FakeGitHub()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ROADMAP.md"
            path.write_text(roadmap_text(), encoding="utf-8")
            with (
                patch.object(roadmap_status.sync, "origin_repo", return_value=(OWNER, REPO)),
                patch.object(roadmap_status.sync, "gh", fake),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = roadmap_status.main(["--roadmap", str(path), "--skip-project", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.issues, {})
        self.assertEqual(fake.mutations(), [])

    def test_dependency_mode_never_reads_optional_board(self):
        with patch.object(roadmap_status, "load_board_statuses") as board:
            self.run_command(["--id", "900", "--json"], [epic()])
        board.assert_not_called()

    def test_unknown_id_retains_requested_identity(self):
        code, output, _ = self.run_command(["--id", "999", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["entry"]["id"], "999")

    def test_every_plan_status_is_accepted(self):
        for status in sorted(roadmap_issues.PLAN_STATUSES):
            with self.subTest(status=status):
                entries = dependencies.parse_roadmap(
                    roadmap_text(status), {}, statuses=roadmap_issues.PLAN_STATUSES
                )
                self.assertEqual(status, entries["900"]["status"])

    def test_progress_maintenance_hooks_are_removed_but_other_hooks_remain(self):
        from test_roadmap_dependency_workflow import ROOT, read_hook_entries
        events = read_hook_entries(ROOT / ".specify/extensions.yml")
        hooks = events["after_implement"]
        commands = [hook["command"] for hook in hooks]
        self.assertNotIn("speckit.roadmap.sync", commands)
        self.assertNotIn("speckit.roadmap.write", commands)
        self.assertNotIn("speckit.roadmap.brief", commands)
        self.assertNotIn("speckit.roadmap.debrief", commands)
        self.assertIn("code-review-and-quality", commands)
        self.assertIn(
            "roadmap-dependency.check",
            [hook["command"] for hook in events["before_implement"]],
        )


class UninitializedRepositoryTest(unittest.TestCase):
    def run_main(self, argv, *directories):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir(exist_ok=True)
            (root / "scripts" / "project_context.py").write_text("", encoding="utf-8")
            for name in directories:
                (root / name).mkdir(parents=True)
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(roadmap_status, "ROOT", root),
                patch.object(roadmap_status.sync, "origin_repo", side_effect=AssertionError("no GitHub access")),
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
            ):
                code = roadmap_status.main(["--roadmap", str(root / "ROADMAP.md"), *argv])
        return code, output.getvalue(), errors.getvalue()

    def test_the_progress_table_prints_one_clear_line_and_passes(self):
        code, output, errors = self.run_main([])
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, "Not initialized yet: no ROADMAP.md. See docs/bootstrap.md.\n")

    def test_json_output_stays_parseable(self):
        code, output, errors = self.run_main(["--json"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), [])
        self.assertIn("Not initialized yet", errors)

    def test_a_dependency_lookup_is_still_an_error(self):
        # The ID cannot exist, so a gate or guard reading this must not see "ready".
        code, output, _ = self.run_main(["--id", "001", "--json"])
        self.assertEqual(code, 2)
        data = json.loads(output)
        self.assertEqual(data["result"], "error")
        self.assertIn("docs/bootstrap.md", data["diagnostics"][0])

    def test_an_initialized_repository_without_a_roadmap_is_an_error(self):
        code, output, _ = self.run_main(["--json"], "specs/001-x")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["result"], "error")
        self.assertIn("ROADMAP.md is missing", json.loads(output)["diagnostics"][0])

    def test_the_state_is_read_where_roadmap_points(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            initialized, other = Path(first), Path(second)
            for root in (initialized, other):
                (root / "scripts").mkdir()
                (root / "scripts" / "project_context.py").write_text("", encoding="utf-8")
            (initialized / "specs" / "001-x").mkdir(parents=True)
            output = io.StringIO()
            with (
                patch.object(roadmap_status, "ROOT", initialized),
                patch.object(roadmap_status.sync, "origin_repo", side_effect=AssertionError("no GitHub access")),
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()),
            ):
                code = roadmap_status.main(["--roadmap", str(other / "ROADMAP.md")])
        self.assertEqual(code, 0)
        self.assertIn("Not initialized yet", output.getvalue())

    def test_a_mistyped_roadmap_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = roadmap_status.main(["--roadmap", str(Path(directory) / "typo" / "ROADMAP.md"), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("not a repository root", output.getvalue())


if __name__ == "__main__":
    unittest.main()
