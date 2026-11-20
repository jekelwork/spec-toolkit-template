import importlib.util
import contextlib
import io
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCRIPT_PATH = Path(__file__).with_name("sync-project-status.py")
SPEC = importlib.util.spec_from_file_location("sync_project_status", SCRIPT_PATH)
sync_project_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_project_status)


class DesiredStatusTest(unittest.TestCase):
    def test_status_rules(self):
        cases = [
            ("phase with no branch", {"issue_labels": ["Story"], "state": "open"}, "Planned"),
            (
                "Backlog is not started",
                {"issue_labels": ["Story"], "state": "open", "current_status": "Backlog"},
                "Planned",
            ),
            (
                "phase branch pushed",
                {"issue_labels": ["Story"], "state": "open", "branch_exists": True},
                "In progress",
            ),
            (
                "open PR enters review even without a remote branch",
                {"issue_labels": ["Story"], "state": "open", "open_pr": True},
                "In review",
            ),
            (
                "ready PR enters review",
                {
                    "issue_labels": ["Story"],
                    "state": "open",
                    "branch_exists": True,
                    "open_pr": True,
                },
                "In review",
            ),
            (
                "closed PR without merge stays in progress while branch exists",
                {"issue_labels": ["Story"], "state": "open", "branch_exists": True},
                "In progress",
            ),
            (
                "closed issue is done",
                {
                    "issue_labels": ["Story"],
                    "state": "closed",
                    "branch_exists": True,
                    "open_pr": True,
                },
                "Done",
            ),
            (
                "not-planned issue returns to backlog",
                {
                    "issue_labels": ["Story"],
                    "state": "closed",
                    "state_reason": "not_planned",
                    "current_status": "Done",
                },
                "Backlog",
            ),
            (
                "issues without a Story label are not tracked",
                {"issue_labels": [], "state": "open", "branch_exists": True},
                None,
            ),
            ("feature is untouched", {"issue_labels": ["Epic"], "state": "open"}, None),
            (
                "planning issue is untouched",
                {"issue_labels": ["Planning"], "state": "open"},
                None,
            ),
            (
                "reopened phase leaves done",
                {
                    "issue_labels": ["Story"],
                    "state": "open",
                    "current_status": "Done",
                },
                "Planned",
            ),
        ]
        for name, arguments, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, sync_project_status.desired_status(**arguments))


class BranchStatusTest(unittest.TestCase):
    def test_only_phase_branches_are_used(self):
        self.assertEqual(
            {(373, 2)},
            sync_project_status.branch_phase_issues(
                [
                    "373-my-feature-phase-2-sync-board",
                    "410-chore-improve-tooling",
                    "main",
                ]
            ),
        )

    def test_phase_branch_matches_issue_title_before_setting_status(self):
        branches = sync_project_status.branch_phase_issues(
            ["100-phase-2-rollout"]
        )

        self.assertFalse(
            sync_project_status.has_active_phase_branch(
                {"number": 100, "title": "Roadmap: spec branch validation"}, branches
            )
        )
        self.assertTrue(
            sync_project_status.has_active_phase_branch(
                {"number": 100, "title": "Project Work - Phase 2: Rollout"}, branches
            )
        )

    def test_open_pull_request_requires_closing_reference_but_includes_drafts(self):
        pull_requests = [
            {"body": "Closes #373", "draft": False},
            {"body": "Fixes #410", "draft": True},
            {"body": "See #420", "draft": False},
            {"body": "Closes example-org/another-repo#430", "draft": False},
        ]
        self.assertEqual(
            {373, 410},
            sync_project_status.open_pr_closing_issues(
                pull_requests, "example-org/example-repo"
            ),
        )

    def test_only_same_repository_or_trusted_pull_requests_count(self):
        def pull_request(number, head_repo, association):
            return {
                "body": f"Closes #{number}",
                "head": {"repo": {"full_name": head_repo}},
                "author_association": association,
            }

        pull_requests = [
            pull_request(1, "example-org/example-repo", "NONE"),
            pull_request(2, "outsider/example-repo", "NONE"),
            pull_request(3, "outsider/example-repo", "CONTRIBUTOR"),
            pull_request(4, "member/example-repo", "MEMBER"),
            pull_request(5, "member/example-repo", "COLLABORATOR"),
            {"body": "Closes #6", "head": {"repo": None}, "author_association": "NONE"},
        ]

        self.assertEqual(
            {1, 4, 5},
            sync_project_status.open_pr_closing_issues(
                pull_requests, "example-org/example-repo"
            ),
        )


class PhaseBranchPullRequestTest(unittest.TestCase):
    def pull_request(self, ref, body="Part of #373", head_repo="example-org/example-repo",
                     association="NONE"):
        return {
            "body": body,
            "head": {"ref": ref, "repo": {"full_name": head_repo}},
            "author_association": association,
        }

    def test_phase_branch_pull_request_maps_to_its_issue_without_a_closing_reference(self):
        pull_requests = [
            self.pull_request("373-feature-phase-2-sync-board"),
            self.pull_request("410-chore-improve-tooling", body="Closes #410"),
        ]

        self.assertEqual(
            {(373, 2)},
            sync_project_status.open_pr_phase_branches(pull_requests, "example-org/example-repo"),
        )

    def test_trusted_authors_fork_phase_branch_counts(self):
        pull_requests = [
            self.pull_request(
                "373-phase-2-sync", head_repo="collaborator/example-repo",
                association="COLLABORATOR",
            )
        ]

        self.assertEqual(
            {(373, 2)},
            sync_project_status.open_pr_phase_branches(pull_requests, "example-org/example-repo"),
        )

    def test_phase_number_must_match_the_issue_title(self):
        pairs = sync_project_status.open_pr_phase_branches(
            [self.pull_request("373-phase-2-sync")], "example-org/example-repo"
        )

        self.assertFalse(
            sync_project_status.has_active_phase_branch(
                {"number": 373, "title": "Phase 1: Something else"}, pairs
            )
        )
        self.assertTrue(
            sync_project_status.has_active_phase_branch(
                {"number": 373, "title": "Phase 2: Sync"}, pairs
            )
        )

    def test_untrusted_fork_phase_branch_does_not_count(self):
        pull_requests = [self.pull_request("373-phase-2-sync", head_repo="outsider/x")]

        self.assertEqual(
            set(),
            sync_project_status.open_pr_phase_branches(pull_requests, "example-org/example-repo"),
        )


class ReconcileTest(unittest.TestCase):
    PHASE = {
        "number": 373,
        "title": "Phase 1: Implement status",
        "state": "open",
        "labels": [{"name": "Story"}],
    }
    UNLABELLED = {"number": 374, "state": "open", "labels": []}

    def reconcile(self, collections, items, apply):
        output = io.StringIO()
        with (
            patch.object(
                sync_project_status, "target_issues",
                return_value={373: self.PHASE, 374: self.UNLABELLED},
            ),
            patch.object(sync_project_status, "api_collection", side_effect=collections),
            patch.object(
                sync_project_status.project_board, "project_item",
                side_effect=lambda owner, repo, number, project_owner, project_number:
                    items[number],
            ) as project_item,
            patch.object(sync_project_status.project_board.Project, "set_status") as set_status,
            contextlib.redirect_stdout(output),
        ):
            sync_project_status.reconcile(
                "example-org", "repo", apply=apply, project_owner="example-org", project_number=42
            )
        return output.getvalue(), set_status, project_item

    def test_draft_pr_moves_phase_to_review_and_ignores_unlabelled_issues(self):
        _, set_status, project_item = self.reconcile(
            [[], [{"body": "Closes #373", "draft": True}]],
            {373: ("item-373", "In progress"), 374: ("item-374", "In progress")},
            True,
        )

        self.assertEqual([("item-373", "In review")], [call.args for call in set_status.call_args_list])
        project_item.assert_called_once_with("example-org", "repo", 373, "example-org", 42)

    def test_part_of_pull_request_from_a_phase_branch_moves_the_phase_to_review(self):
        pull_request = {
            "body": "Part of #373",
            "draft": False,
            "head": {"ref": "373-feature-phase-1-implement-status",
                     "repo": {"full_name": "example-org/repo"}},
        }
        _, set_status, _ = self.reconcile(
            [[{"name": "373-feature-phase-1-implement-status"}], [pull_request]],
            {373: ("item-373", "In progress"), 374: ("item-374", "In progress")},
            True,
        )

        self.assertEqual([("item-373", "In review")], [call.args for call in set_status.call_args_list])

    def closed_reconcile(self, state_reason, body):
        closed = {**self.PHASE, "state": "closed", "state_reason": state_reason, "body": body}
        with (
            patch.object(sync_project_status, "target_issues", return_value={373: closed}),
            patch.object(sync_project_status, "api_collection", side_effect=[[], []]),
            patch.object(
                sync_project_status.project_board, "project_item",
                return_value=("item-373", "In review"),
            ),
            patch.object(sync_project_status.project_board.Project, "set_status") as set_status,
            patch.object(sync_project_status.project_board.Project, "remove_item") as remove_item,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            sync_project_status.reconcile(
                "example-org", "repo", apply=True, project_owner="example-org", project_number=42
            )
        return set_status, remove_item

    def test_phase_closed_as_completed_keeps_its_card_and_is_set_to_done(self):
        set_status, remove_item = self.closed_reconcile("completed", "<!-- speckit-phase: x#phase-1 -->")

        self.assertEqual([("item-373", "Done")], [c.args for c in set_status.call_args_list])
        remove_item.assert_not_called()

    def test_paused_phase_keeps_its_card_as_backlog(self):
        paused = f"<!-- speckit-phase: x#phase-1 -->\n{sync_project_status.sync.PHASE_PAUSED_MARKER}"
        set_status, remove_item = self.closed_reconcile("not_planned", paused)

        self.assertEqual([("item-373", "Backlog")], [c.args for c in set_status.call_args_list])
        remove_item.assert_not_called()

    def retired_reconcile(self, apply):
        retired = {
            **self.PHASE, "state": "closed", "state_reason": "not_planned",
            "body": f"<!-- speckit-phase: specs/001-x#phase-1 -->\n\n"
                    f"{sync_project_status.sync.PHASE_RETIRED_MARKER}\n",
        }
        output = io.StringIO()
        with (
            patch.object(sync_project_status, "target_issues", return_value={373: retired}),
            patch.object(sync_project_status, "api_collection", side_effect=[[], []]),
            patch.object(
                sync_project_status.project_board, "project_item",
                return_value=("item-373", "Done"),
            ),
            patch.object(sync_project_status.project_board.Project, "set_status") as set_status,
            patch.object(sync_project_status.project_board.Project, "remove_item") as remove_item,
            contextlib.redirect_stdout(output),
        ):
            sync_project_status.reconcile(
                "example-org", "repo", apply=apply, project_owner="example-org", project_number=42
            )
        return output.getvalue(), set_status, remove_item

    def test_retired_phase_is_removed_from_the_board_not_set_to_done(self):
        _, set_status, remove_item = self.retired_reconcile(True)

        set_status.assert_not_called()
        remove_item.assert_called_once_with("item-373")

    def test_retired_phase_dry_run_previews_the_removal(self):
        output, set_status, remove_item = self.retired_reconcile(False)

        self.assertIn("[dry-run] remove #373 from project example-org/42", output)
        set_status.assert_not_called()
        remove_item.assert_not_called()

    def test_phase_branch_dry_run_reports_only_the_phase(self):
        output, set_status, _ = self.reconcile(
            [[{"name": "373-feature-phase-1-implement-status"}], []],
            {373: ("phase-item", "Backlog"), 374: ("task-item", "Backlog")},
            False,
        )

        self.assertIn("[dry-run] #373: Backlog -> In progress", output)
        self.assertNotIn("#374", output)
        set_status.assert_not_called()

    def test_target_issues_for_one_issue_makes_no_parent_or_sub_issue_calls(self):
        with patch.object(sync_project_status, "get_issue", return_value=self.PHASE) as get_issue:
            issues = sync_project_status.target_issues("o", "r", 373)

        self.assertEqual({373: self.PHASE}, issues)
        get_issue.assert_called_once_with("o", "r", 373)


class ProjectConfigurationTest(unittest.TestCase):
    def parse(self, *arguments):
        with patch.object(sys, "argv", ["sync-project-status.py", "--all", *arguments]):
            return sync_project_status.parse_arguments()

    def test_explicit_project_configuration(self):
        with patch.object(sync_project_status.project_context, "load_context") as load:
            args = self.parse("--project-owner", "example-org", "--project-number", "42")
        load.assert_not_called()
        self.assertEqual("example-org", args.project_owner)
        self.assertEqual(42, args.project_number)
        self.assertFalse(args.apply)

    def test_context_defaults_and_independent_overrides(self):
        with patch.object(
            sync_project_status.project_context, "load_context",
            return_value={"github_project": {"owner": "fixture-org", "number": 91}},
        ):
            for arguments, owner, number in (
                ((), "fixture-org", 91),
                (("--project-owner", "other-org"), "other-org", 91),
                (("--project-number", "42"), "fixture-org", 42),
            ):
                with self.subTest(arguments=arguments):
                    args = self.parse(*arguments)
                    self.assertEqual((owner, number), (args.project_owner, args.project_number))

    def test_missing_or_invalid_context_fails_without_github_calls(self):
        for error in (FileNotFoundError("missing context"), ValueError("invalid context")):
            with (
                patch.object(sync_project_status.project_context, "load_context", side_effect=error),
                patch.object(sync_project_status.sync, "gh") as gh,
                contextlib.redirect_stderr(io.StringIO()) as output,
            ):
                with self.assertRaises(SystemExit) as stopped:
                    self.parse()
                self.assertEqual(2, stopped.exception.code)
                self.assertIn("Invalid project context TOML", output.getvalue())
                gh.assert_not_called()

    def test_invalid_project_overrides_are_rejected(self):
        for arguments in (
            ("--project-owner", " ", "--project-number", "42"),
            ("--project-owner", "-invalid", "--project-number", "42"),
            ("--project-owner", "example-org", "--project-number", "0"),
            ("--project-owner", "example-org", "--project-number", "-1"),
            ("--project-owner", "example-org", "--project-number", "invalid"),
        ):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    self.parse(*arguments)
                self.assertEqual(2, stopped.exception.code)


class WorkflowProjectConfigurationTest(unittest.TestCase):
    def test_app_token_is_scoped_to_projects_and_issue_reads(self):
        action = (SCRIPT_PATH.parent.parent / ".github/actions/board-token/action.yml").read_text()
        token_step = action.split("- name: Create optional board token", 1)[1]
        self.assertIn("permission-issues: read", token_step)
        self.assertIn("permission-organization-projects: write", token_step)
        self.assertNotIn("permission-contents", token_step)
        self.assertNotIn("permission-pull-requests", token_step)
        self.assertIn("owner: ${{ steps.project-config.outputs.owner }}", token_step)
        self.assertIn("repositories: ${{ steps.repo.outputs.name }}", token_step)

    def test_status_workflow_has_a_separate_mutation_queue(self):
        workflow = (SCRIPT_PATH.parent.parent / ".github/workflows/sync-project-status.yml").read_text()
        self.assertIn("group: project-status", workflow)
        self.assertNotIn("group: issue-sync", workflow)

    def test_workflow_validates_shared_context_before_token_creation(self):
        action = (SCRIPT_PATH.parent.parent / ".github/actions/board-token/action.yml").read_text()
        validation = action.split("- name: Read configured Project owner\n", 1)[1]
        validation, _ = validation.split("- name: Create optional board token", 1)
        self.assertIn("python3 scripts/project_context.py", validation)
        self.assertIn("--repository-owner", validation)

    def test_every_workflow_invocation_uses_shared_context_defaults(self):
        workflow = (SCRIPT_PATH.parent.parent / ".github/workflows/sync-project-status.yml").read_text()
        self.assertNotIn("PROJECT_BOARD_OWNER", workflow)
        self.assertNotIn("PROJECT_BOARD_NUMBER", workflow)
        invocations = [
            line for line in workflow.splitlines()
            if line.strip().startswith("python3 scripts/sync-project-status.py ")
        ]
        self.assertEqual(5, len(invocations))
        for invocation in invocations:
            self.assertNotIn("--project-owner", invocation)
            self.assertNotIn("--project-number", invocation)


if __name__ == "__main__":
    unittest.main()
