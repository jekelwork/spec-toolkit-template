import ast
import contextlib
import copy
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import bootstrap_github  # noqa: E402

REPO = "acme/widgets"
CONTEXT = """\
[project]
name = "widgets"
description = "Widgets"

[github_project]
owner = "acme"
number = 3

[review]
required_approvals = {approvals}
"""


class FakeGitHub:
    """Stands in for gh: keeps repository state, records every call, applies writes to the state."""

    def __init__(self, owner_type="Organization", board=True, status_options=None):
        self.calls = []
        self.writes = []
        self.labels = {}
        self.owner_type = owner_type
        self.secrets = []
        self.variables = []
        self.rulesets = []
        self.forbid_ruleset_writes = False
        self.fork_approval = "all_external_contributors"
        self.fork_approval_error = None
        self.private = False
        self.org_rulesets = [{"id": 99, "name": bootstrap_github.RULESET_NAME,
                              "source_type": "Organization", "source": "acme", "rules": []}]
        self.org_secrets = []
        self.org_variables = []
        self.org_error = False
        self.fail_mutation = None
        self.cwds = {}
        self.next_option = 0
        self.board = None
        if board:
            options = status_options
            if options is None:
                options = ["Todo", "In Progress", "Done"]
            self.board = {
                "id": "PVT_1",
                "url": "https://github.com/orgs/acme/projects/3",
                "repositories": [],
                "fields": [
                    {"id": "F_TITLE", "name": "Title", "dataType": "TITLE"},
                    {
                        "id": "F_STATUS",
                        "name": "Status",
                        "dataType": "SINGLE_SELECT",
                        "options": [self.option(name) for name in options],
                    },
                ],
            }

    def option(self, name, color="GRAY", description=""):
        self.next_option += 1
        return {"id": f"opt{self.next_option}", "name": name, "color": color,
                "description": description}

    def find_label(self, name):
        return next((key for key in self.labels if key.casefold() == name.casefold()), None)

    @staticmethod
    def body(argv):
        if "--input" in argv:
            return json.loads(Path(argv[argv.index("--input") + 1]).read_text())
        return None

    @staticmethod
    def is_write(argv, body):
        if argv[:3] in (["gh", "label", "create"], ["gh", "label", "edit"]):
            return True
        if argv[:3] == ["gh", "api", "graphql"]:
            return "mutation" in body["query"]
        return "-X" in argv

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        body = self.body(argv)  # read before the temp file disappears
        self.calls.append(argv)
        self.cwds[tuple(argv[:3])] = cwd
        if self.is_write(argv, body):
            self.writes.append(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            return json.dumps({"nameWithOwner": REPO})
        if argv[:3] == ["gh", "label", "list"]:
            return json.dumps([{"name": n, "color": c, "description": d}
                               for n, (c, d) in self.labels.items()])
        if argv[:3] == ["gh", "label", "create"]:
            if self.find_label(argv[3]):
                raise bootstrap.CommandError("label already exists (HTTP 422)")
            self.labels[argv[3]] = (argv[argv.index("--color") + 1],
                                    argv[argv.index("--description") + 1])
            return ""
        if argv[:3] == ["gh", "label", "edit"]:
            key = self.find_label(argv[3])
            del self.labels[key]
            name = argv[argv.index("--name") + 1] if "--name" in argv else key
            self.labels[name] = (argv[argv.index("--color") + 1],
                                 argv[argv.index("--description") + 1])
            return ""
        if argv[:3] == ["gh", "secret", "list"]:
            return json.dumps([{"name": name} for name in self.secrets])
        if argv[:3] == ["gh", "variable", "list"]:
            return json.dumps([{"name": name} for name in self.variables])
        if argv[:3] == ["gh", "api", "graphql"]:
            return self.graphql(body["query"], body["variables"])
        if argv[:2] == ["gh", "api"]:
            return self.rest(argv, body)
        raise AssertionError(f"unexpected command {argv}")

    def graphql(self, query, variables):
        if self.fail_mutation and self.fail_mutation in query:
            raise bootstrap.CommandError(
                "Your token has not been granted the required scopes to execute this query.")
        if "createProjectV2Field" in query:
            data = variables["input"]
            field = {"id": f"F_{data['name']}", "name": data["name"], "dataType": data["dataType"]}
            if data["dataType"] == "SINGLE_SELECT":
                field["options"] = [self.option(o["name"], o["color"], o["description"])
                                    for o in data["singleSelectOptions"]]
            self.board["fields"].append(field)
            return "{}"
        if "updateProjectV2Field" in query:
            data = variables["input"]
            names = [o["name"].casefold() for o in data["singleSelectOptions"]]
            if len(names) != len(set(names)):
                raise bootstrap.CommandError("Validation failed: option names must be unique")
            field = next(f for f in self.board["fields"] if f["id"] == data["fieldId"])
            field["options"] = [
                {"id": o.get("id") or self.option(o["name"])["id"], "name": o["name"],
                 "color": o["color"], "description": o["description"]}
                for o in data["singleSelectOptions"]
            ]
            return "{}"
        if "linkProjectV2ToRepository" in query:
            self.board["repositories"].append({"nameWithOwner": REPO})
            return "{}"
        if "repository(owner" in query:
            return json.dumps({"data": {"repository": {"id": "R_1"}}})
        if variables["login"] != "acme":
            return json.dumps({"data": {"repositoryOwner": None}})
        if self.board is None:
            raise bootstrap.CommandError("Could not resolve to a ProjectV2 with the number 3.")
        project = {
            "id": self.board["id"], "url": self.board["url"],
            "repositories": {"nodes": self.board["repositories"]},
            "fields": {"nodes": self.board["fields"]},
        }
        return json.dumps({"data": {"repositoryOwner": {
            "__typename": self.owner_type, "projectV2": project}}})

    READ_ONLY = {"node_id": "RRS_x", "_links": {}, "source_type": "Repository", "source": REPO,
                 "current_user_can_bypass": "never", "created_at": "t0", "updated_at": "t1"}
    PRIVATE_ERROR = "gh: Fork PR approval is not allowed for private repositories. (Validation Failed)"
    WRITABLE = {"name", "target", "enforcement", "conditions", "rules", "bypass_actors"}

    def rest(self, argv, body):
        path = next(arg for arg in argv[2:] if arg.startswith("repos/"))
        route = path.split("?")[0]
        method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
        if route.endswith("/actions/organization-secrets"):
            return self.org_listing("secrets", self.org_secrets)
        if route.endswith("/actions/organization-variables"):
            return self.org_listing("variables", self.org_variables)
        if route == f"repos/{REPO}":
            return "true" if self.private else "false"
        if route.endswith("/actions/permissions/fork-pr-contributor-approval"):
            if self.private:
                raise bootstrap.CommandError(self.PRIVATE_ERROR)
            if self.fork_approval_error and (method != "GET" or self.fork_approval_error[1]):
                raise bootstrap.CommandError(self.fork_approval_error[0])
            if method == "GET":
                return json.dumps({"approval_policy": self.fork_approval})
            self.fork_approval = body["approval_policy"]
            return ""
        if method != "GET" and self.forbid_ruleset_writes:
            raise bootstrap.CommandError("gh: Resource not accessible by personal access token (HTTP 403)")
        if method == "GET" and route.endswith("/rulesets"):
            listed = list(self.rulesets)
            if "includes_parents=false" not in path:
                listed += self.org_rulesets
            return json.dumps([{"id": r["id"], "name": r["name"],
                                "source_type": r.get("source_type", "Repository")} for r in listed])
        if method == "GET":
            found = next(r for r in self.rulesets + self.org_rulesets if route.endswith(f"/{r['id']}"))
            return json.dumps({**self.READ_ONLY, **found})
        unknown = set(body) - self.WRITABLE
        if unknown:
            raise bootstrap.CommandError(f"Invalid request: unknown properties {sorted(unknown)} (HTTP 422)")
        if method == "POST":
            self.rulesets.append({"id": 11, **body})
        else:
            index = next(i for i, r in enumerate(self.rulesets) if route.endswith(f"/{r['id']}"))
            self.rulesets[index] = {"id": self.rulesets[index]["id"], **body}
        return "{}"

    def org_listing(self, key, names):
        if self.org_error:
            raise bootstrap.CommandError("gh: Not Found (HTTP 404)")
        return json.dumps({"total_count": len(names), key: [{"name": n} for n in names]})


class GitHubTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.write_context(1)

    def write_context(self, approvals):
        (self.root / "project-context.toml").write_text(CONTEXT.format(approvals=approvals))

    def github(self, run, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(["github", *args], root=self.root, run=run, is_tty=False)
        return code, out.getvalue(), err.getvalue()

    def managed(self, run):
        return next(r for r in run.rulesets if r["name"] == "Default branch protection")

    def rule(self, run, kind):
        return next(r for r in self.managed(run)["rules"] if r["type"] == kind)


class ModesTest(GitHubTestCase):
    def test_preview_lists_every_change_by_area_and_writes_nothing(self):
        run = FakeGitHub()
        code, out, _ = self.github(run)
        self.assertEqual(code, 0)
        self.assertEqual(run.writes, [])
        for line in (
            "Labels: create Epic",
            "Labels: create combined-spec-pr-approved",
            "Board: rename Status option Todo → Backlog",
            "Board: rename Status option In Progress → In progress",
            "Board: add Status option Planned",
            "Board: add Status option In review",
            "Board: create field Roadmap order",
            "Board: link to acme/widgets",
            "Ruleset: create Default branch protection (required approvals 1)",
        ):
            self.assertIn(line, out)
        self.assertLess(out.index("\nLabels:"), out.index("\nBoard: "))
        self.assertLess(out.index("\nBoard: "), out.index("\nRuleset:"))
        self.assertIn("--apply", out)

    def test_check_exits_1_on_drift_and_changes_nothing(self):
        run = FakeGitHub()
        code, out, _ = self.github(run, "--check")
        self.assertEqual(code, 1)
        self.assertEqual(run.writes, [])
        self.assertIn("Labels: create Epic", out)

    def test_apply_then_check_is_clean_and_a_second_apply_plans_nothing(self):
        run = FakeGitHub()
        code, _, _ = self.github(run, "--apply")
        self.assertEqual(code, 0)
        writes = len(run.writes)
        self.assertGreater(writes, 0)
        code, out, _ = self.github(run, "--check")
        self.assertEqual(code, 0)
        code, out, _ = self.github(run, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(run.writes), writes)
        self.assertIn("Nothing to change", out)
        self.assertNotIn("create", out)

    def test_apply_makes_exactly_the_needed_calls(self):
        run = FakeGitHub()
        self.github(run, "--apply")
        kinds = [call[:3] for call in run.writes if call[:2] == ["gh", "label"]]
        self.assertEqual(len(kinds), 7)
        self.assertEqual(sum(call[:3] == ["gh", "api", "graphql"] for call in run.writes), 3)
        posts = [call for call in run.writes if "-X" in call]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][posts[0].index("-X") + 1], "POST")

    def test_header_names_repository_and_board_and_repo_view_runs_in_the_root(self):
        run = FakeGitHub()
        _, out, _ = self.github(run)
        self.assertEqual(
            out.splitlines()[0],
            "Repository: acme/widgets · Board: https://github.com/orgs/acme/projects/3",
        )
        self.assertEqual(run.cwds[("gh", "repo", "view")], self.root)

    def test_partial_apply_names_the_applied_and_failed_areas(self):
        run = FakeGitHub()
        run.fail_mutation = "updateProjectV2Field"
        code, _, err = self.github(run, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("Applied completely: Labels", err)
        self.assertIn("failed: Board", err)
        self.assertEqual(len(run.labels), 7)
        self.assertEqual(run.rulesets, [])

    def test_missing_context_file_points_to_init(self):
        (self.root / "project-context.toml").unlink()
        code, _, err = self.github(FakeGitHub())
        self.assertEqual(code, 2)
        self.assertIn("bootstrap.py init", err)

    def test_apply_and_check_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.github(FakeGitHub(), "--apply", "--check")


class LabelsTest(GitHubTestCase):
    def test_only_differing_labels_are_updated(self):
        run = FakeGitHub()
        for name, color, description in bootstrap_github.LABELS:
            run.labels[name] = (color.lower(), description)  # color case does not matter
        run.labels["Epic"] = ("000000", "Epic")
        del run.labels["abandoned"]
        _, out, _ = self.github(run)
        self.assertIn("Labels: update Epic", out)
        self.assertIn("Labels: create abandoned", out)
        self.assertEqual(out.count("Labels:"), 2)

    def test_override_labels_are_marked_human_only(self):
        by_name = {name: description for name, _, description in bootstrap_github.LABELS}
        for name in ("combined-spec-pr-approved", "prerequisites-acknowledged"):
            self.assertIn("human", by_name[name].lower())


class LabelCaseTest(GitHubTestCase):
    def test_case_variants_are_renamed_not_recreated(self):
        run = FakeGitHub()
        run.labels["epic"] = ("6A0DAD", "Roadmap entry - parent for Phase issues")
        _, out, _ = self.github(run, "--apply")
        self.assertIn("Labels: rename epic → Epic", out)
        self.assertIn("Epic", run.labels)
        self.assertNotIn("epic", run.labels)
        code, _, _ = self.github(run, "--check")
        self.assertEqual(code, 0)


class BoardTest(GitHubTestCase):
    def test_fresh_board_options_are_renamed_not_duplicated(self):
        run = FakeGitHub()
        ids = {o["name"]: o["id"] for o in run.board["fields"][1]["options"]}
        self.github(run, "--apply")
        after = run.board["fields"][1]["options"]
        self.assertEqual([o["name"] for o in after],
                         ["Backlog", "Planned", "In progress", "In review", "Done"])
        self.assertEqual(
            [o["id"] for o in after if o["name"] != "Planned" and o["name"] != "In review"],
            [ids["Todo"], ids["In Progress"], ids["Done"]],
        )
        self.assertTrue(all(o["color"] for o in after))

    def test_existing_backlog_leaves_todo_as_an_unmanaged_note_not_drift(self):
        run = FakeGitHub(status_options=["Todo", "Backlog", "Planned"])
        ids = {o["name"]: o["id"] for o in run.board["fields"][1]["options"]}
        _, out, _ = self.github(run, "--apply")
        after = run.board["fields"][1]["options"]
        self.assertEqual([o["name"] for o in after],
                         ["Backlog", "Planned", "In progress", "In review", "Done", "Todo"])
        self.assertEqual(after[0]["id"], ids["Backlog"])
        self.assertEqual(after[-1]["id"], ids["Todo"])
        self.assertIn("Todo", out.split("Note")[1])
        code, out, _ = self.github(run, "--check")
        self.assertEqual(code, 0)
        self.assertIn("Todo", out)

    def test_names_match_case_insensitively_and_no_change_means_no_reorder(self):
        run = FakeGitHub(status_options=["done", "Planned", "Backlog", "In progress", "In review"])
        _, out, _ = self.github(run)
        self.assertIn("Board: rename Status option done → Done", out)
        run = FakeGitHub(status_options=["Planned", "Backlog", "In progress", "In review", "Done"])
        run.board["repositories"].append({"nameWithOwner": REPO})
        run.board["fields"].append({"id": "F_O", "name": "Roadmap order", "dataType": "NUMBER"})
        code, _, _ = self.github(run, "--check")
        self.assertEqual(code, 1)  # only the ruleset and labels differ, not the board
        _, out, _ = self.github(run)
        self.assertEqual([l for l in out.splitlines() if l.startswith("Board: ")], [])

    def test_duplicate_case_variants_stop_with_a_clear_error(self):
        run = FakeGitHub(status_options=["In Progress", "In progress", "Done"])
        code, _, err = self.github(run)
        self.assertEqual(code, 2)
        self.assertIn("In Progress", err)
        self.assertIn("delete or rename", err)

    def test_exact_name_is_preferred_over_a_case_variant(self):
        run = FakeGitHub(status_options=["Todo", "In progress", "Done"])
        ids = {o["name"]: o["id"] for o in run.board["fields"][1]["options"]}
        self.github(run, "--apply")
        kept = {o["name"]: o["id"] for o in run.board["fields"][1]["options"]}
        self.assertEqual(kept["In progress"], ids["In progress"])

    def test_roadmap_order_of_another_type_is_an_error(self):
        run = FakeGitHub()
        run.board["fields"].append({"id": "F_O", "name": "Roadmap order", "dataType": "TEXT"})
        code, _, err = self.github(run)
        self.assertEqual(code, 2)
        self.assertIn("NUMBER", err)
        self.assertIn("Roadmap order", err)

    def test_board_scope_error_gets_the_auth_refresh_hint(self):
        run = FakeGitHub()
        run.fail_mutation = "updateProjectV2Field"
        code, _, err = self.github(run, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("gh auth refresh -s project", err)

    def test_complete_board_plans_nothing(self):
        run = FakeGitHub(status_options=["Backlog", "Planned", "In progress", "In review", "Done"])
        run.board["repositories"].append({"nameWithOwner": "Acme/Widgets"})
        run.board["fields"].append({"id": "F_ORDER", "name": "Roadmap order", "dataType": "NUMBER"})
        _, out, _ = self.github(run)
        self.assertEqual([l for l in out.splitlines() if l.startswith("Board: ")], [])

    def test_missing_board_suggests_create_board(self):
        code, _, err = self.github(FakeGitHub(board=False))
        self.assertEqual(code, 2)
        self.assertIn('gh project create --owner acme --title "widgets"', err)
        self.assertIn("github_project.number", err)
        self.assertNotIn("--create-board", err)

    def test_user_and_organization_owned_boards_both_work(self):
        for owner_type in ("User", "Organization"):
            run = FakeGitHub(owner_type=owner_type)
            code, out, _ = self.github(run)
            self.assertEqual(code, 0, owner_type)
            self.assertIn("Board: add Status option Planned", out)

    def test_manual_workflow_step_is_printed_with_the_board_url(self):
        _, out, _ = self.github(FakeGitHub())
        self.assertIn("Manual step", out)
        self.assertIn("https://github.com/orgs/acme/projects/3", out)
        self.assertIn("In review", out)

    def test_manual_step_is_not_drift(self):
        run = FakeGitHub()
        self.github(run, "--apply")
        code, out, _ = self.github(run, "--check")
        self.assertEqual(code, 0)
        self.assertIn("Manual step", out)


class RulesetTest(GitHubTestCase):
    def test_new_ruleset_has_the_managed_rules(self):
        run = FakeGitHub()
        self.github(run, "--apply")
        ruleset = self.managed(run)
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["target"], "branch")
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        self.assertEqual({r["type"] for r in ruleset["rules"]},
                         {"deletion", "non_fast_forward", "pull_request", "required_status_checks"})
        pull_request = self.rule(run, "pull_request")["parameters"]
        self.assertEqual(pull_request["required_approving_review_count"], 1)
        self.assertTrue(pull_request["require_last_push_approval"])
        self.assertFalse(pull_request["require_extra_approval_for_unattributed_changes"])
        self.assertEqual(pull_request["allowed_merge_methods"], ["merge", "squash", "rebase"])
        checks = self.rule(run, "required_status_checks")["parameters"]
        self.assertFalse(checks["strict_required_status_checks_policy"])
        self.assertEqual(
            [(c["context"], c["integration_id"]) for c in checks["required_status_checks"]],
            [(name, 15368) for name in bootstrap_github.REQUIRED_CHECKS],
        )

    def test_approval_counts_zero_one_and_two(self):
        for approvals, last_push in ((0, False), (1, True), (2, True)):
            self.write_context(approvals)
            run = FakeGitHub()
            self.github(run, "--apply")
            parameters = self.rule(run, "pull_request")["parameters"]
            self.assertEqual(parameters["required_approving_review_count"], approvals)
            self.assertEqual(parameters["require_last_push_approval"], last_push)

    def existing(self, run, approvals=1):
        self.write_context(approvals)
        self.github(run, "--apply")
        ruleset = self.managed(run)
        ruleset["rules"].insert(2, {"type": "copilot_code_review",
                                    "parameters": {"review_on_push": True}})
        pull_request = self.rule(run, "pull_request")["parameters"]
        pull_request["dismiss_stale_reviews_on_push"] = True
        pull_request["require_extra_approval_for_unattributed_changes"] = True
        ruleset["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole",
                                     "bypass_mode": "always"}]
        run.calls.clear()
        run.writes.clear()

    def test_existing_ruleset_changes_only_managed_values(self):
        run = FakeGitHub()
        self.existing(run)
        self.write_context(2)
        _, out, _ = self.github(run, "--apply")
        self.assertIn("Ruleset: required approvals 1 → 2", out)
        rules = self.managed(run)["rules"]
        self.assertIn("copilot_code_review", [r["type"] for r in rules])
        parameters = self.rule(run, "pull_request")["parameters"]
        self.assertEqual(parameters["required_approving_review_count"], 2)
        self.assertTrue(parameters["dismiss_stale_reviews_on_push"])
        self.assertTrue(parameters["require_extra_approval_for_unattributed_changes"])
        self.assertEqual(self.managed(run)["bypass_actors"][0]["actor_id"], 5)
        puts = [call for call in run.writes if "-X" in call]
        self.assertEqual([call[call.index("-X") + 1] for call in puts], ["PUT"])
        self.assertTrue(puts[0][puts[0].index("-X") + 2].endswith("/rulesets/11"))

    def test_lowering_to_zero_keeps_the_pull_request_rule(self):
        run = FakeGitHub()
        self.existing(run)
        self.write_context(0)
        _, out, _ = self.github(run, "--apply", "--allow-weaken")
        self.assertIn("required approvals 1 → 0", out)
        self.assertIn("last-push approval true → false", out)
        parameters = self.rule(run, "pull_request")["parameters"]
        self.assertEqual(parameters["required_approving_review_count"], 0)
        self.assertFalse(parameters["require_last_push_approval"])

    def test_two_approvals_turn_on_stale_review_dismissal(self):
        run = FakeGitHub()
        self.write_context(2)
        self.github(run, "--apply")
        parameters = self.rule(run, "pull_request")["parameters"]
        self.assertTrue(parameters["dismiss_stale_reviews_on_push"])

    def test_existing_ruleset_gets_stale_dismissal_when_two_are_required(self):
        run = FakeGitHub()
        self.existing(run)
        self.rule(run, "pull_request")["parameters"]["dismiss_stale_reviews_on_push"] = False
        self.write_context(2)
        _, out, _ = self.github(run, "--apply")
        self.assertIn("Ruleset: dismiss stale reviews on push false → true", out)
        self.assertTrue(self.rule(run, "pull_request")["parameters"]["dismiss_stale_reviews_on_push"])

    def test_up_to_one_approval_keeps_the_existing_stale_dismissal_value(self):
        for existing in (True, False):
            run = FakeGitHub()
            self.existing(run)
            self.rule(run, "pull_request")["parameters"]["dismiss_stale_reviews_on_push"] = existing
            self.write_context(1)
            code, out, _ = self.github(run, "--check")
            self.assertEqual(code, 0, out)
            self.assertNotIn("dismiss", out)

    def test_a_missing_stale_dismissal_key_is_not_drift_below_two_approvals(self):
        for approvals in (0, 1):
            run = FakeGitHub()
            self.existing(run, approvals=approvals)
            del self.rule(run, "pull_request")["parameters"]["dismiss_stale_reviews_on_push"]
            code, out, _ = self.github(run, "--check")
            self.assertEqual(code, 0, out)
            self.assertNotIn("dismiss", out)
            self.assertNotIn("false → false", out)

    def test_an_existing_ruleset_with_bypass_actors_prints_a_note(self):
        run = FakeGitHub()
        self.existing(run)
        code, out, _ = self.github(run, "--check")
        self.assertEqual(code, 0, out)
        self.assertIn("Note:", out)
        self.assertIn("bypass_actors", out)

    def test_a_ruleset_without_bypass_actors_prints_no_bypass_note(self):
        run = FakeGitHub()
        self.existing(run)
        self.managed(run)["bypass_actors"] = []
        _, out, _ = self.github(run, "--check")
        self.assertNotIn("bypass_actors", out)

    def test_new_ruleset_with_one_approval_does_not_dismiss(self):
        run = FakeGitHub()
        self.github(run, "--apply")
        parameters = self.rule(run, "pull_request")["parameters"]
        self.assertFalse(parameters["dismiss_stale_reviews_on_push"])

    def test_lowering_approvals_is_refused_without_allow_weaken(self):
        run = FakeGitHub()
        self.existing(run, approvals=2)
        self.write_context(1)
        code, out, _ = self.github(run, "--apply")
        self.assertEqual(code, 0)
        self.assertIn(
            "Warning: required approvals 2 → 1 (rerun with --allow-weaken to apply)", out)
        self.assertEqual([call for call in run.writes if "-X" in call], [])
        parameters = self.rule(run, "pull_request")["parameters"]
        self.assertEqual(parameters["required_approving_review_count"], 2)

    def test_refused_weakening_is_drift_under_check(self):
        run = FakeGitHub()
        self.existing(run, approvals=2)
        self.write_context(1)
        code, out, _ = self.github(run, "--check")
        self.assertEqual(code, 1)
        self.assertIn("--allow-weaken", out)
        self.assertNotIn("Nothing to change", out)

    def test_lowering_approvals_is_applied_with_allow_weaken(self):
        run = FakeGitHub()
        self.existing(run, approvals=2)
        self.write_context(1)
        self.github(run, "--apply", "--allow-weaken")
        parameters = self.rule(run, "pull_request")["parameters"]
        self.assertEqual(parameters["required_approving_review_count"], 1)

    def test_other_changes_still_apply_next_to_a_refused_weakening(self):
        run = FakeGitHub()
        self.existing(run, approvals=2)
        ruleset = self.managed(run)
        ruleset["rules"] = [r for r in ruleset["rules"] if r["type"] != "deletion"]
        self.write_context(1)
        self.github(run, "--apply")
        self.assertIn("deletion", [r["type"] for r in self.managed(run)["rules"]])
        parameters = self.rule(run, "pull_request")["parameters"]
        self.assertEqual(parameters["required_approving_review_count"], 2)

    def test_turning_off_last_push_approval_is_refused_without_allow_weaken(self):
        run = FakeGitHub()
        self.existing(run)
        self.write_context(0)
        self.rule(run, "pull_request")["parameters"]["required_approving_review_count"] = 0
        _, out, _ = self.github(run, "--apply")
        self.assertIn("Warning: last-push approval true → false", out)
        self.assertTrue(self.rule(run, "pull_request")["parameters"]["require_last_push_approval"])

    def test_missing_managed_pieces_are_added_to_an_existing_ruleset(self):
        run = FakeGitHub()
        self.existing(run)
        ruleset = self.managed(run)
        ruleset["enforcement"] = "disabled"
        ruleset["rules"] = [r for r in ruleset["rules"] if r["type"] != "deletion"]
        checks = self.rule(run, "required_status_checks")["parameters"]
        checks["required_status_checks"] = checks["required_status_checks"][:2] + [
            {"context": "Extra check"}]
        _, out, _ = self.github(run, "--apply")
        self.assertIn("Ruleset: enforcement disabled → active", out)
        self.assertIn("Ruleset: add rule deletion", out)
        self.assertIn("Ruleset: add required check Roadmap prerequisites", out)
        contexts = [c["context"] for c in
                    self.rule(run, "required_status_checks")["parameters"]["required_status_checks"]]
        self.assertIn("Extra check", contexts)
        self.assertEqual(len(contexts), 5)
        code, _, _ = self.github(run, "--check")
        self.assertEqual(code, 0)

    def test_organization_ruleset_with_the_same_name_is_never_matched(self):
        run = FakeGitHub()
        _, out, _ = self.github(run)
        self.assertIn("Ruleset: create", out)
        listing = next(c for c in run.calls if c[:2] == ["gh", "api"] and c[2].endswith(
            "includes_parents=false") or c[:2] == ["gh", "api"] and "includes_parents=false" in c[2])
        self.assertIn("/rulesets?", listing[2])
        self.github(run, "--apply")
        self.assertEqual(len(run.rulesets), 1)

    def test_404_on_read_says_repository_or_ruleset_not_found(self):
        run = FakeGitHub()
        original = run.rest

        def missing(argv, body):
            if "/rulesets" in argv[-1]:
                raise bootstrap.CommandError("gh: Not Found (HTTP 404)")
            return original(argv, body)

        run.rest = missing
        code, _, err = self.github(run)
        self.assertEqual(code, 2)
        self.assertIn("repository or ruleset not found", err)
        self.assertIn("gh repo view", err)
        self.assertNotIn("repository admin", err)

    def test_403_asks_for_an_admin(self):
        run = FakeGitHub()
        run.forbid_ruleset_writes = True
        code, _, err = self.github(run, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("ask a repository admin to run `bootstrap.py github --apply`", err)
        self.assertIn("plan that supports rulesets", err)

    def test_404_is_treated_like_403(self):
        run = FakeGitHub()
        original = run.rest

        def deny(argv, body):
            if "-X" in argv:
                raise bootstrap.CommandError("gh: Not Found (HTTP 404)")
            return original(argv, body)

        run.rest = deny
        code, _, err = self.github(run, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("repository admin", err)

    PLAN_ERROR = ("gh: Upgrade to GitHub Pro or make this repository public to enable this "
                  "feature. (HTTP 403)")

    def test_a_plan_without_rulesets_gets_a_plan_message_on_write(self):
        run = FakeGitHub()
        original = run.rest

        def plan(argv, body):
            if "-X" in argv:
                raise bootstrap.CommandError(self.PLAN_ERROR)
            return original(argv, body)

        run.rest = plan
        code, _, err = self.github(run, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("public repository or a GitHub plan", err)
        self.assertNotIn("ask a repository admin", err)

    def test_a_plan_without_rulesets_gets_a_plan_message_on_read(self):
        run = FakeGitHub()

        def plan(argv, body):
            raise bootstrap.CommandError(self.PLAN_ERROR)

        run.rest = plan
        code, _, err = self.github(run)
        self.assertEqual(code, 2)
        self.assertIn("public repository or a GitHub plan", err)
        self.assertNotIn("ask a repository admin", err)


class ForkApprovalTest(GitHubTestCase):
    ROUTE = "actions/permissions/fork-pr-contributor-approval"

    def run_with(self, policy):
        run = FakeGitHub()
        run.fork_approval = policy
        return run

    def put(self, run):
        return [c for c in run.writes if any(self.ROUTE in a for a in c)]

    def test_first_time_policy_is_planned_and_not_written_in_preview(self):
        run = self.run_with("first_time_contributors")

        code, out, _ = self.github(run)

        self.assertEqual(0, code)
        self.assertIn(
            "Actions: fork PR approval first_time_contributors → all_external_contributors", out
        )
        self.assertEqual([], self.put(run))

    def test_apply_writes_the_policy_then_check_is_clean(self):
        run = self.run_with("first_time_contributors")

        self.github(run, "--apply")

        self.assertEqual("all_external_contributors", run.fork_approval)
        self.assertEqual(1, len(self.put(run)))
        self.assertIn("PUT", self.put(run)[0])
        code, _, _ = self.github(run, "--check")
        self.assertEqual(0, code)

    def test_check_reports_drift(self):
        run = self.run_with("first_time_contributors_new_to_github")

        code, _, _ = self.github(run, "--check")

        self.assertEqual(1, code)
        self.assertEqual([], self.put(run))

    def test_an_already_strict_policy_is_left_alone(self):
        run = self.run_with("all_external_contributors")

        _, out, _ = self.github(run, "--apply")

        self.assertEqual([], self.put(run))
        self.assertNotIn("fork PR approval", out)

    def test_an_unknown_value_is_never_downgraded_and_prints_a_note(self):
        run = self.run_with("everyone_including_bots")

        code, out, _ = self.github(run, "--apply")

        self.assertEqual(0, code)
        self.assertEqual([], self.put(run))
        self.assertEqual("everyone_including_bots", run.fork_approval)
        self.assertIn("Note: fork PR approval policy 'everyone_including_bots'", out)

    def test_a_private_repository_is_skipped_with_a_note_and_never_asked(self):
        run = self.run_with("first_time_contributors")
        run.private = True
        self.github(run, "--apply")

        code, out, err = self.github(run, "--check")

        self.assertEqual(0, code, err)
        self.assertIn("Note: fork PR approval applies to public repositories only", out)
        self.assertNotIn("Actions:", out)
        self.assertEqual([], [c for c in run.calls if any(self.ROUTE in a for a in c)])

    def test_a_422_for_a_private_repository_is_a_note_not_an_error(self):
        run = self.run_with("first_time_contributors")
        # Visibility changed between the repository lookup and the policy read.
        run.fork_approval_error = (FakeGitHub.PRIVATE_ERROR, True)

        code, out, err = self.github(run, "--apply")

        self.assertEqual(0, code, err)
        self.assertIn("Note: fork PR approval applies to public repositories only", out)
        self.assertEqual([], self.put(run))

    def test_a_denied_write_names_admin_rights(self):
        run = self.run_with("first_time_contributors")
        run.fork_approval_error = ("gh: Resource not accessible (HTTP 403)", False)

        code, _, err = self.github(run, "--apply")

        self.assertEqual(2, code)
        self.assertIn("repository admin", err)

    def test_a_denied_read_names_admin_rights(self):
        run = self.run_with("first_time_contributors")
        run.fork_approval_error = ("gh: Not Found (HTTP 404)", True)

        code, _, err = self.github(run)

        self.assertEqual(2, code)
        self.assertIn("fork pull request approval policy", err)
        self.assertIn("repository admin", err)


class CredentialsTest(GitHubTestCase):
    def report(self, secrets, variables=()):
        run = FakeGitHub()
        run.secrets = list(secrets)
        run.variables = list(variables)
        code, out, err = self.github(run)
        return run, code, out, err

    def test_both_app_secrets_count_as_configured(self):
        _, _, out, _ = self.report(["PROJECT_STATUS_APP_ID", "PROJECT_BOARD_APP_PRIVATE_KEY"])
        self.assertIn("Board credential: GitHub App configured", out)

    def test_token_counts_as_configured(self):
        _, _, out, _ = self.report(["PROJECT_BOARD_TOKEN"])
        self.assertIn("Board credential: PROJECT_BOARD_TOKEN configured", out)

    def test_one_app_secret_warns(self):
        _, _, out, _ = self.report(["PROJECT_STATUS_APP_ID"])
        self.assertIn("Warning: only PROJECT_STATUS_APP_ID is set", out)
        self.assertIn("docs/bootstrap.md#board-credentials", out)

    def test_organization_secrets_count_and_a_failure_is_noted(self):
        run = FakeGitHub()
        run.org_secrets = ["PROJECT_BOARD_TOKEN"]
        _, out, _ = self.github(run)
        self.assertIn("Board credential: PROJECT_BOARD_TOKEN configured", out)
        run = FakeGitHub()
        run.org_error = True
        _, out, _ = self.github(run)
        self.assertEqual(out.count("organization-level secrets and variables were not checked"), 1)

    def test_nothing_configured_points_to_the_guide(self):
        _, _, out, _ = self.report([])
        self.assertIn("Board credential: not configured", out)
        self.assertIn("docs/bootstrap.md#board-credentials", out)

    def test_auto_apply_variable_is_reported(self):
        _, _, out, _ = self.report([], ["PHASE_SYNC_AUTO_APPLY"])
        self.assertIn("PHASE_SYNC_AUTO_APPLY: set", out)
        _, _, out, _ = self.report([])
        self.assertIn("PHASE_SYNC_AUTO_APPLY: not set", out)

    def test_missing_credentials_are_not_drift_and_values_are_never_read(self):
        run = FakeGitHub()
        self.github(run, "--apply")
        code, out, _ = self.github(run, "--check")
        self.assertEqual(code, 0)
        self.assertIn("not configured", out)
        for call in run.calls:
            if call[:2] == ["gh", "secret"]:
                self.assertEqual(call[2], "list")
                self.assertIn("name", call[call.index("--json") + 1])


if __name__ == "__main__":
    unittest.main()


SCRIPTS = Path(__file__).resolve().parent
WORKFLOWS = SCRIPTS.parent / ".github" / "workflows"


class ConstantsTest(unittest.TestCase):
    def test_required_checks_are_the_job_names_of_the_gating_workflows(self):
        names = set()
        for workflow in ("spec-pr-phases", "pr-conventions", "toolkit", "roadmap-gate"):
            text = (WORKFLOWS / f"{workflow}.yml").read_text(encoding="utf-8")
            names |= set(re.findall(r"^    name: (.+)$", text, re.MULTILINE))
        self.assertEqual(set(bootstrap_github.REQUIRED_CHECKS), names)

    @staticmethod
    def strings(node):
        return {
            n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }

    def labels_used_by(self, script):
        tree = ast.parse((SCRIPTS / script).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.endswith("_LABELS") for t in node.targets
            ):
                found |= self.strings(node.value)
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name == "update_labels":
                    for keyword in node.keywords:
                        if keyword.arg == "add":
                            found |= self.strings(keyword.value)
                elif name in ("create_issue", "create_if_missing") and len(node.args) > 3:
                    found |= self.strings(node.args[3])
        return found

    def test_labels_cover_the_labels_the_sync_scripts_apply(self):
        used = self.labels_used_by("sync-roadmap-issues.py") | self.labels_used_by(
            "sync-task-issues.py")
        # The extraction must find the labels it is meant to guard.
        self.assertTrue({"Epic", "Story", "Planning", "needs-info", "abandoned"} <= used, used)
        self.assertEqual(used - {name for name, _, _ in bootstrap_github.LABELS}, set())
