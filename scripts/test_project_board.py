import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_board  # noqa: E402
from fake_github import FakeGitHub, OWNER, REPO  # noqa: E402


def gh_project_calls(fake):
    """Any `gh project` call: gh checks scopes client-side and wants read:org, which a PAT lacks."""
    return [call for call in fake.calls if call[:1] == ("project",)]


class ProjectBoardTest(unittest.TestCase):
    def use(self, fake):
        patcher = patch.object(project_board.sync, "gh", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def item(self, fake, number, owner="example-org", project_number=123):
        return project_board.project_item(OWNER, REPO, number, owner, project_number)


class ProjectItemTest(ProjectBoardTest):
    def test_returns_item_id_and_status(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="In review")

        self.assertEqual(("PVTI_4", "In review"), self.item(fake, 4))

    def test_status_is_none_when_unset(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", in_project=True)

        self.assertEqual(("PVTI_4", None), self.item(fake, 4))

    def test_details_include_status_and_roadmap_order(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="In review")
        fake.project_items[4]["order"] = 12

        self.assertEqual(
            ("PVTI_4", "In review", 12),
            project_board.project_item_details(OWNER, REPO, 4, "example-org", 123),
        )

    def test_none_when_issue_is_not_in_the_project(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body")

        self.assertIsNone(self.item(fake, 4))

    def test_other_project_number_or_owner_is_ignored(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Done")

        self.assertIsNone(self.item(fake, 4, project_number=20))
        self.assertIsNone(self.item(fake, 4, owner="someone-else"))

    def test_owner_comparison_ignores_case(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Done")

        self.assertEqual(("PVTI_4", "Done"), self.item(fake, 4, owner="Example-ORG"))

    def test_reading_makes_no_mutation(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Done")

        self.item(fake, 4)

        self.assertEqual([], fake.mutations())


class AddIssueToProjectTest(ProjectBoardTest):
    def test_adds_the_issue_and_returns_its_item_id(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body")

        item_id = project_board.add_issue_to_project(
            OWNER, REPO, 4, project_board.Project("example-org", 123)
        )

        self.assertEqual("PVTI_4", item_id)
        self.assertEqual(("PVTI_4", None), self.item(fake, 4))
        self.assertEqual([], gh_project_calls(fake))

    def test_a_fresh_add_needs_no_lookup_and_no_sleep(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body")
        sleeps = []

        with (
            patch.object(project_board, "_sleep", sleeps.append),
            patch.object(project_board, "project_item") as lookup,
        ):
            item_id = project_board.add_issue_to_project(
                OWNER, REPO, 4, project_board.Project("example-org", 123)
            )

        self.assertEqual("PVTI_4", item_id)
        lookup.assert_not_called()
        self.assertEqual([], sleeps)

    def test_returns_the_existing_item_when_github_already_added_the_content(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")

        item_id = project_board.add_issue_to_project(
            OWNER, REPO, 4, project_board.Project("example-org", 123)
        )

        self.assertEqual("PVTI_4", item_id)

    def test_other_add_errors_still_fail_with_their_message(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body")
        original = fake._dispatch

        def dispatch(args):
            if any("addProjectV2ItemById" in a for a in args):
                return fake._result(
                    1, stderr="gh: Resource not accessible", args=args, check=fake._check
                )
            return original(args)

        with patch.object(fake, "_dispatch", dispatch):
            with self.assertRaises(SystemExit) as stopped:
                project_board.add_issue_to_project(
                    OWNER, REPO, 4, project_board.Project("example-org", 123)
                )

        self.assertEqual(
            "Cannot add issue #4 to project example-org/123: gh: Resource not accessible",
            str(stopped.exception),
        )

    def lagging_lookup(self, fake, misses):
        """Make the item lookup answer 'not in the project' for the first `misses` reads."""
        real = project_board.project_item
        reads = []

        def lookup(*args):
            reads.append(args)
            return None if len(reads) <= misses else real(*args)

        return patch.object(project_board, "project_item", lookup), reads

    def test_retries_the_lookup_when_the_auto_added_item_is_not_visible_yet(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")
        lag, reads = self.lagging_lookup(fake, misses=2)
        sleeps = []

        with lag, patch.object(project_board, "_sleep", sleeps.append):
            item_id = project_board.add_issue_to_project(
                OWNER, REPO, 4, project_board.Project("example-org", 123)
            )

        self.assertEqual("PVTI_4", item_id)
        self.assertEqual(3, len(reads))
        self.assertEqual(2, len(sleeps))

    def test_item_never_visible_stops_with_a_rerun_message(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")
        lag, reads = self.lagging_lookup(fake, misses=99)

        with lag, patch.object(project_board, "_sleep", lambda seconds: None):
            with self.assertRaises(SystemExit) as stopped:
                project_board.add_issue_to_project(
                    OWNER, REPO, 4, project_board.Project("example-org", 123)
                )

        self.assertEqual(
            "GitHub reports issue #4 is already in project example-org/123, but it is not "
            "visible yet; rerun the sync",
            str(stopped.exception),
        )
        self.assertEqual(3, len(reads))

    def test_works_for_a_user_owned_board_without_gh_project_owner_lookup(self):
        fake = self.use(FakeGitHub(project_owner="some-user", project_owner_kind="user"))
        fake.add_issue(4, "body")

        item_id = project_board.add_issue_to_project(
            OWNER, REPO, 4, project_board.Project("some-user", 123)
        )

        self.assertEqual("PVTI_4", item_id)
        self.assertEqual([], gh_project_calls(fake))

    def test_existing_backlog_card_is_moved_to_planned(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")

        project_board.ensure_planned(
            OWNER,
            REPO,
            project_board.Project("example-org", 123),
            {"number": 4, "state": "open"},
            True,
            lambda message: None,
        )

        self.assertEqual(("PVTI_4", "Planned"), self.item(fake, 4))


class FakeAutoAddTest(ProjectBoardTest):
    # These tests check the fake's own timing model (its internal queue), not production code.
    def test_auto_add_keeps_an_item_that_is_already_on_the_board(self):
        fake = self.use(FakeGitHub())
        fake.auto_add_sub_issues = True
        fake.add_issue(1, "epic", status="Planned")
        fake.add_issue(4, "child", status="In progress")
        fake.project_items[4]["order"] = 7.0

        fake(
            "api", "-X", "POST", f"repos/{OWNER}/{REPO}/issues/1/sub_issues",
            "-F", f"sub_issue_id={fake.issues[4]['id']}",
        )

        self.assertIn(4, fake.pending_auto_adds)

        self.assertEqual(("PVTI_4", "In progress"), self.item(fake, 4))
        self.assertEqual(set(), fake.pending_auto_adds)
        self.assertEqual(7.0, fake.project_items[4]["order"])

    def test_auto_add_queues_existing_children_when_the_parent_is_added(self):
        fake = self.use(FakeGitHub())
        fake.auto_add_sub_issues = True
        fake.add_issue(1, "epic")
        fake.add_issue(4, "child")
        fake(
            "api", "-X", "POST", f"repos/{OWNER}/{REPO}/issues/1/sub_issues",
            "-F", f"sub_issue_id={fake.issues[4]['id']}",
        )

        fake(
            "api", "graphql",
            "-f", "query=mutation { addProjectV2ItemById }",
            "-f", "projectId=PVT_project",
            "-f", f"contentId={fake.issues[1]['node_id']}",
        )

        self.assertIn(4, fake.pending_auto_adds)

        self.assertIsNone(self.item(fake, 4))  # the first read still sees nothing
        self.assertEqual(set(), fake.pending_auto_adds)
        self.assertIn(4, fake.project_items)
        self.assertIsNotNone(self.item(fake, 4))


class EnsureRemovedTest(ProjectBoardTest):
    ISSUE = {"number": 4}

    def remove(self, apply):
        messages = []
        project = project_board.Project("example-org", 123)
        changed = project_board.ensure_removed(OWNER, REPO, project, self.ISSUE, apply, messages.append)
        return changed, messages

    def test_removes_the_item_with_a_graphql_mutation_and_no_gh_project(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Done")

        changed, _ = self.remove(True)

        self.assertTrue(changed)
        self.assertIsNone(self.item(fake, 4))
        self.assertEqual([], gh_project_calls(fake))
        mutation = next(c for c in fake.calls if any("deleteProjectV2Item" in a for a in c))
        query = next(a for a in mutation if a.startswith("query="))
        self.assertNotIn("PVTI_4", query)

    def test_is_a_no_op_when_the_issue_is_not_on_the_board(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body")

        self.assertEqual((False, []), self.remove(True))
        self.assertEqual([], fake.mutations())

    def test_dry_run_prints_and_does_not_mutate(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Done")

        changed, messages = self.remove(False)

        self.assertTrue(changed)
        self.assertEqual(["remove #4 from project example-org/123"], messages)
        self.assertEqual([], fake.mutations())


class ProjectStatusTest(ProjectBoardTest):
    def test_resolves_user_and_organization_in_separate_queries(self):
        fake = self.use(FakeGitHub())

        project_board.Project("example-org", 123).status_ids()

        queries = [next(a for a in call if a.startswith("query=")) for call in fake.calls]
        self.assertEqual(2, len(queries))
        self.assertTrue(all(("user(login" in q) != ("organization(login" in q) for q in queries))

    def test_resolves_a_user_owned_project(self):
        fake = self.use(FakeGitHub(project_owner="some-user", project_owner_kind="user"))
        fake.add_issue(4, "body", status="Backlog")

        project_board.Project("some-user", 123).set_status("PVTI_4", "Done")

        self.assertEqual(("PVTI_4", "Done"), self.item(fake, 4, owner="some-user"))
        self.assertEqual([], gh_project_calls(fake))

    def lookup_responses(self, fake, user, organization):
        """Make the user and organization lookups answer (returncode, stdout, stderr)."""
        original = fake._dispatch
        answers = {"user(login": user, "organization(login": organization}

        def dispatch(args):
            for marker, (code, out, err) in answers.items():
                if any(marker in a for a in args):
                    return fake._result(code, stdout=out, stderr=err, check=False)
            return original(args)

        patcher = patch.object(fake, "_dispatch", dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)

    NOT_FOUND = (
        1,
        '{"data":{"organization":null},'
        '"errors":[{"type":"NOT_FOUND","path":["organization"]}]}',
        "gh: Could not resolve to an Organization",
    )

    def stopped_message(self):
        with self.assertRaises(SystemExit) as stopped:
            project_board.Project("example-org", 123).status_ids()
        return str(stopped.exception)

    def test_non_not_found_graphql_error_is_reported(self):
        fake = self.use(FakeGitHub())
        body = (
            '{"data":{"user":null},'
            '"errors":[{"type":"INSUFFICIENT_SCOPES","message":"needs read:project"}]}'
        )
        self.lookup_responses(fake, (1, body, ""), self.NOT_FOUND)

        self.assertIn("needs read:project", self.stopped_message())

    def test_gh_not_logged_in_is_reported(self):
        fake = self.use(FakeGitHub())
        failure = (1, "", "To get started with GitHub CLI, please run:  gh auth login")
        self.lookup_responses(fake, failure, failure)

        self.assertIn("gh auth login", self.stopped_message())

    def test_http_error_body_is_reported(self):
        fake = self.use(FakeGitHub())
        body = '{"message":"Bad credentials","status":"401"}'
        self.lookup_responses(fake, (1, body, "gh: Bad credentials (HTTP 401)"), (1, body, ""))

        self.assertIn("Bad credentials", self.stopped_message())

    def test_non_object_body_does_not_crash(self):
        fake = self.use(FakeGitHub())
        self.lookup_responses(fake, (1, "[1]", "boom"), (1, "null", "boom"))

        self.assertIn("boom", self.stopped_message())

    def test_user_without_that_project_falls_through_to_organization(self):
        fake = self.use(FakeGitHub())
        no_project = (
            '{"data":{"user":{"projectV2":null}},'
            '"errors":[{"type":"NOT_FOUND","path":["user","projectV2"]}]}'
        )
        original = fake._dispatch

        def dispatch(args):
            if any("user(login" in a for a in args):
                return fake._result(1, stdout=no_project, check=False)
            return original(args)

        with patch.object(fake, "_dispatch", dispatch):
            project_id, _ = project_board.find_project("example-org", 123)

        self.assertEqual("PVT_project", project_id)

    def test_missing_issue_stops_with_a_clear_error(self):
        self.use(FakeGitHub())

        with self.assertRaises(SystemExit) as stopped:
            project_board.add_issue_to_project(
                OWNER, REPO, 4, project_board.Project("example-org", 123)
            )

        self.assertIn("Issue #4 not found", str(stopped.exception))

    def test_invisible_repository_is_not_reported_as_a_missing_issue(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body")
        original = fake._dispatch

        def dispatch(args):
            if any("issue(number: $number) { id }" in a for a in args):
                body = (
                    '{"data":{"repository":null},'
                    '"errors":[{"type":"NOT_FOUND","path":["repository"]}]}'
                )
                return fake._result(1, stdout=body, check=False)
            return original(args)

        with patch.object(fake, "_dispatch", dispatch):
            with self.assertRaises(SystemExit) as stopped:
                project_board.add_issue_to_project(
                    OWNER, REPO, 4, project_board.Project("example-org", 123)
                )

        message = str(stopped.exception)
        # A wrong or renamed name, or a local run with the developer's own gh login, gives the
        # same answer.
        self.assertIn(
            f"Repository {OWNER}/{REPO} was not found or is not visible to the token used "
            "for the board",
            message,
        )
        self.assertIn("repo", message)
        self.assertNotIn("Issue #4 not found", message)

    def test_issue_lookup_error_reports_its_cause(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body")
        original = fake._dispatch

        def dispatch(args):
            if any("issue(number: $number) { id }" in a for a in args):
                body = '{"message":"Bad credentials","status":"401"}'
                return fake._result(1, stdout=body, check=False)
            return original(args)

        with patch.object(fake, "_dispatch", dispatch):
            with self.assertRaises(SystemExit) as stopped:
                project_board.add_issue_to_project(
                    OWNER, REPO, 4, project_board.Project("example-org", 123)
                )

        self.assertIn("Bad credentials", str(stopped.exception))

    def test_adding_several_issues_looks_the_project_up_once(self):
        fake = self.use(FakeGitHub())
        for number in (4, 5, 6):
            fake.add_issue(number, "body")
        project = project_board.Project("example-org", 123)

        for number in (4, 5, 6):
            project_board.add_issue_to_project(OWNER, REPO, number, project)

        lookups = [c for c in fake.calls if any("organization(login" in a for a in c)]
        self.assertEqual(1, len(lookups))

    def test_missing_project_stops_with_a_clear_error(self):
        self.use(FakeGitHub())

        with self.assertRaises(SystemExit) as stopped:
            project_board.Project("example-org", 999).status_ids()

        self.assertIn(
            "No project 999 found for user or organization 'example-org'", str(stopped.exception)
        )

    def test_sets_status_by_option_name(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")

        project_board.Project("example-org", 123).set_status("PVTI_4", "In progress")

        self.assertEqual(("PVTI_4", "In progress"), self.item(fake, 4))

    def test_reads_project_ids_once(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")
        project = project_board.Project("example-org", 123)

        project.set_status("PVTI_4", "In progress")
        project.set_status("PVTI_4", "Done")
        project.number_field()

        lookups = [call for call in fake.calls if any("organization(login" in a for a in call)]
        self.assertEqual(1, len(lookups))
        self.assertEqual([], gh_project_calls(fake))

    def test_status_write_uses_graphql_with_variables_and_no_gh_project(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")

        project_board.Project("example-org", 123).set_status("PVTI_4", "Done")

        self.assertEqual([], gh_project_calls(fake))
        mutation = next(c for c in fake.calls if any("singleSelectOptionId" in a for a in c))
        query = next(a for a in mutation if a.startswith("query="))
        self.assertNotIn("PVTI_4", query)
        self.assertNotIn("opt-Done", query)
        self.assertIn("optionId=opt-Done", mutation)

    def test_all_digit_option_id_is_sent_as_a_string(self):
        fake = self.use(FakeGitHub())
        fake.option_ids["Done"] = "98236657"
        fake.add_issue(4, "body", status="Backlog")

        project_board.Project("example-org", 123).set_status("PVTI_4", "Done")

        mutation = next(c for c in fake.calls if any("singleSelectOptionId" in a for a in c))
        index = mutation.index("optionId=98236657")
        self.assertEqual("-f", mutation[index - 1])
        self.assertEqual(("PVTI_4", "Done"), self.item(fake, 4))

    def test_roadmap_order_write_uses_no_gh_project(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")

        project_board.Project("example-org", 123).set_number("PVTI_4", 7)

        self.assertEqual([], gh_project_calls(fake))
        self.assertEqual(7.0, fake.project_items[4]["order"])

    def test_failed_status_write_reports_its_cause(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")
        project = project_board.Project("example-org", 123)
        project.status_ids()
        original = fake._dispatch

        def dispatch(args):
            if any("singleSelectOptionId" in a for a in args):
                return fake._result(1, stdout='{"message":"Bad credentials"}', check=False)
            return original(args)

        with patch.object(fake, "_dispatch", dispatch):
            with self.assertRaises(SystemExit) as stopped:
                project.set_status("PVTI_4", "Done")

        self.assertIn("Bad credentials", str(stopped.exception))

    def test_unknown_status_option_stops_without_editing(self):
        fake = self.use(FakeGitHub())
        fake.add_issue(4, "body", status="Backlog")

        with self.assertRaises(SystemExit) as stopped:
            project_board.Project("example-org", 123).set_status("PVTI_4", "Epic")

        self.assertIn("has no Status option 'Epic'", str(stopped.exception))
        self.assertEqual([], fake.mutations())

    def test_project_without_status_field_stops(self):
        fake = self.use(FakeGitHub())
        original = fake._dispatch

        def without_status_field(args):
            result = original(args)
            if any("organization(login" in a for a in args):
                body = json.loads(result.stdout)
                body["data"]["organization"]["projectV2"]["fields"]["nodes"] = []
                return fake._result(stdout=json.dumps(body))
            return result

        with patch.object(fake, "_dispatch", without_status_field):
            with self.assertRaises(SystemExit) as stopped:
                project_board.Project("example-org", 123).status_ids()

        self.assertIn("has no Status field", str(stopped.exception))


if __name__ == "__main__":
    unittest.main()
