import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).with_name("sync-task-issues.py")
SPEC = importlib.util.spec_from_file_location("sync_task_issues", SCRIPT_PATH)
sync_task_issues = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_task_issues)


PARSE_FIXTURE_TASKS = """# Tasks: Example Feature

## Format: `[ID] [P?] [Story] Description`

- [ ] Not a task: no T### identifier

## Phase 1: Setup

**Purpose**: Prepare the project.

- [x] T001 Create the project skeleton
- [X] T002 [P] Configure formatting

## Phase 2: Foundational

- [ ] T060 Added late but belongs to Phase 2 (depends on T001)
- [ ] T003 Write the first test

## Phase 4: Polish (numbers may have gaps)

- [ ] T004 Final review
"""


class OriginRepoTest(unittest.TestCase):
    def origin(self, url):
        done = type("Done", (), {"stdout": url + "\n"})()
        with patch.object(sync_task_issues.subprocess, "run", return_value=done):
            return sync_task_issues.origin_repo()

    def test_the_remote_forms_name_owner_and_repository(self):
        for url in ("https://github.com/acme/widgets", "https://github.com/acme/widgets.git",
                    "https://github.com/acme/widgets/", "https://github.com/acme/widgets.git/",
                    "git@github.com:acme/widgets.git"):
            with self.subTest(url=url):
                self.assertEqual(("acme", "widgets"), self.origin(url))

    def test_a_remote_that_is_not_github_ends_the_process(self):
        with self.assertRaises(SystemExit):
            self.origin("https://example.com/acme/widgets.git")


class SyncTaskIssuesTest(unittest.TestCase):
    def test_parses_feature_phases_and_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.md"
            path.write_text(PARSE_FIXTURE_TASKS, encoding="utf-8")
            feature = sync_task_issues.parse_feature(path)

        self.assertEqual("Example Feature", feature["title"])
        self.assertEqual(
            ["phase-1", "phase-2", "phase-4"],
            [phase["key"] for phase in feature["phases"]],
        )
        self.assertEqual(
            ["**Purpose**: Prepare the project."], feature["phases"][0]["summary"]
        )
        self.assertEqual(
            ["T001", "T002", "T060", "T003", "T004"],
            [task["id"] for task in feature["tasks"]],
        )
        self.assertEqual(
            [True, True, False, False, False],
            [task["done"] for task in feature["tasks"]],
        )
        task_060 = next(task for task in feature["tasks"] if task["id"] == "T060")
        self.assertEqual("phase-2", task_060["phase_key"])
        self.assertEqual(
            "T060 Added late but belongs to Phase 2 (depends on T001)", task_060["text"]
        )

    def test_no_task_issue_or_dependency_logic_remains(self):
        for name in (
            "task_marker",
            "task_body",
            "task_title",
            "explicit_blockers",
            "load_configured_blockers",
            "issue_task_id",
        ):
            self.assertFalse(hasattr(sync_task_issues, name), name)

    def test_task_text_keeps_inline_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.md"
            path.write_text(
                "# Tasks: Demo\n\n## Phase 1: One\n\n"
                "- [ ] T001 [P] [US1] Create schema\n"
                "- [ ] T002 Add resolvers (depends on T001)\n",
                encoding="utf-8",
            )
            feature = sync_task_issues.parse_feature(path)

        self.assertEqual("T002 Add resolvers (depends on T001)", feature["tasks"][1]["text"])
        body = sync_task_issues.phase_body("specs/900-demo", feature, feature["phases"][0])
        self.assertIn("- [ ] T001 [P] [US1] Create schema", body)
        self.assertIn("- [ ] T002 Add resolvers (depends on T001)", body)
        self.assertIn("<!-- speckit-phase: specs/900-demo#phase-1 -->", body)
        self.assertIn("<!-- speckit-roadmap-phase: 900#phase-1 -->", body)

    def test_phase_numbers_are_identities_and_gaps_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.md"
            path.write_text(
                "# Tasks: Demo\n\n## Phase 1: One\n\n- [ ] T001 A\n\n"
                "## Phase 3: Three\n\n- [ ] T002 B\n",
                encoding="utf-8",
            )
            feature = sync_task_issues.parse_feature(path)

        self.assertEqual(["phase-1", "phase-3"], [phase["key"] for phase in feature["phases"]])

    def test_scopes_phase_markers_to_feature(self):
        issues = [
            {"number": 1, "body": sync_task_issues.phase_marker("specs/001-first-feature", "phase-1")},
            {"number": 2, "body": sync_task_issues.phase_marker("specs/002-other", "phase-1")},
            {"number": 3, "body": "Task T001 from `specs/001-first-feature/tasks.md`"},
        ]

        phases = sync_task_issues.find_phase_issues(issues, "specs/001-first-feature")

        self.assertEqual({"phase-1": 1}, {key: issue["number"] for key, issue in phases.items()})

    def test_phase_marker_by_a_public_outsider_is_ignored(self):
        marker = sync_task_issues.phase_marker("specs/001-first-feature", "phase-1")
        issues = [
            {"number": 1, "body": marker, "author": "dev", "author_association": "MEMBER"},
            {"number": 2, "body": marker, "author": "stranger", "author_association": "NONE"},
        ]

        phases = sync_task_issues.find_phase_issues(issues, "specs/001-first-feature")

        self.assertEqual({"phase-1": 1}, {key: issue["number"] for key, issue in phases.items()})

    @patch.object(sync_task_issues, "gh")
    def test_creates_issue_with_label(self, gh):
        gh.return_value.stdout = "https://github.com/o/r/issues/1\n"

        sync_task_issues.create_issue("o/r", "Title", "Body", "Story")

        arguments = gh.call_args.args
        self.assertEqual("Story", arguments[arguments.index("--label") + 1])

    @patch.object(sync_task_issues, "gh")
    def test_creates_issue_with_several_labels(self, gh):
        gh.return_value.stdout = "https://github.com/o/r/issues/1\n"

        sync_task_issues.create_issue("o/r", "Title", "Body", ("Planning", "needs-info"))

        arguments = list(gh.call_args.args)
        self.assertEqual(2, arguments.count("--label"))

    @patch.object(sync_task_issues, "gh")
    def test_reads_existing_parent(self, gh):
        gh.return_value.returncode = 0
        gh.return_value.stdout = "79\n"

        parent = sync_task_issues.current_parent_number("repos/example/project", {"number": 80})

        self.assertEqual(79, parent)

    @patch.object(sync_task_issues, "gh")
    def test_missing_parent_returns_none(self, gh):
        gh.return_value.returncode = 1
        gh.return_value.stderr = "gh: No parent issue found (HTTP 404)"

        parent = sync_task_issues.current_parent_number("repos/example/project", {"number": 80})

        self.assertIsNone(parent)

    @patch.object(sync_task_issues, "gh")
    def test_reads_existing_blockers(self, gh):
        gh.return_value.stdout = "13\n16\n"

        blockers = sync_task_issues.current_blocker_numbers("repos/example/project", {"number": 18})

        self.assertEqual({13, 16}, blockers)
        self.assertIn("--paginate", gh.call_args.args)

    def test_parses_done_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.md"
            path.write_text(
                "# Tasks: Demo\n\n## Phase 1: One\n\n"
                "- [x] T001 Done thing\n- [ ] T002 Open thing\n",
                encoding="utf-8",
            )
            feature = sync_task_issues.parse_feature(path)

        self.assertEqual([True, False], [task["done"] for task in feature["tasks"]])

    def test_completed_issues_cascade_to_phase_and_feature(self):
        feature = {
            "phases": [
                {"key": "phase-1", "title": "Phase 1: A"},
                {"key": "phase-2", "title": "Phase 2: B"},
            ],
            "tasks": [
                {"id": "T001", "phase_key": "phase-1", "done": True},
                {"id": "T002", "phase_key": "phase-2", "done": True},
                {"id": "T003", "phase_key": "phase-2", "done": False},
            ],
        }
        phase_issues = {"phase-1": {"number": 10}, "phase-2": {"number": 11}}

        completed = sync_task_issues.completed_issues(feature, {"number": 20}, phase_issues)

        self.assertEqual([10], [issue["number"] for _, issue in completed])

        for task in feature["tasks"]:
            task["done"] = True
        completed = sync_task_issues.completed_issues(feature, {"number": 20}, phase_issues)
        self.assertEqual([10, 11, 20], [issue["number"] for _, issue in completed])

    def test_phase_without_tasks_is_never_complete(self):
        feature = {"phases": [{"key": "phase-1", "title": "Phase 1: A"}], "tasks": []}
        completed = sync_task_issues.completed_issues(
            feature, {"number": 20}, {"phase-1": {"number": 10}}
        )
        self.assertEqual([], completed)

    @patch.object(sync_task_issues, "gh")
    def test_closes_only_open_issues(self, gh):
        closed, already_closed = sync_task_issues.close_completed_issues(
            "repos/example/project",
            [
                ("Phase 1", {"number": 1, "state": "open"}),
                ("Phase 2", {"number": 2, "state": "closed"}),
            ],
            apply=True,
        )

        self.assertEqual((1, 1), (closed, already_closed))
        gh.assert_called_once()
        self.assertIn("repos/example/project/issues/1", gh.call_args.args)

    @patch.object(sync_task_issues, "gh")
    def test_dry_run_does_not_close(self, gh):
        sync_task_issues.close_completed_issues(
            "repos/example/project", [("Phase 1", {"number": 1, "state": "open"})], apply=False
        )

        gh.assert_not_called()

    @patch.object(sync_task_issues, "gh")
    def test_updates_open_phase_when_tasks_md_changes(self, gh):
        feature = {
            "title": "Demo",
            "phases": [{"key": "phase-1", "title": "Phase 1: Renamed", "summary": []}],
            "tasks": [{"id": "T001", "text": "T001 New", "phase_key": "phase-1", "done": False}],
        }
        old = dict(feature, phases=[{"key": "phase-1", "title": "Phase 1: Kickoff", "summary": []}])
        issue = {
            "number": 230,
            "state": "open",
            "title": "Phase 1: Kickoff — Demo",
            "body": sync_task_issues.phase_body("specs/900-demo", old, old["phases"][0]),
        }

        changed = sync_task_issues.update_existing_issues(
            "repos/o/r", "specs/900-demo", feature, None, {"phase-1": issue}, True, None
        )

        self.assertEqual(1, changed)
        self.assertIn("title=Phase 1: Renamed", gh.call_args.args)

    @patch.object(sync_task_issues, "gh")
    def test_skips_unchanged_and_closed_issues(self, gh):
        same = {"number": 1, "state": "open", "title": "T", "body": "B\n"}
        closed = {"number": 2, "state": "closed", "title": "old", "body": "old"}

        self.assertFalse(sync_task_issues.update_issue_content("r", same, "P", "T", "B", True))
        self.assertFalse(sync_task_issues.update_issue_content("r", closed, "P", "T", "B", True))
        gh.assert_not_called()

    def test_finds_removed_phases_but_not_retired_ones(self):
        feature_path = "specs/004-example-state"
        feature = {"phases": [{"key": "phase-1"}]}
        issues = [
            {"number": 1, "body": sync_task_issues.phase_marker(feature_path, "phase-1")},
            {"number": 3, "body": sync_task_issues.phase_marker(feature_path, "phase-2")},
            {"number": 4, "body": sync_task_issues.phase_marker("specs/other", "phase-9")},
            {
                "number": 5,
                "body": sync_task_issues.phase_marker(feature_path, "phase-3"),
                "state": "closed",
                "state_reason": "not_planned",
            },
        ]

        phases = sync_task_issues.find_phase_issues(issues, feature_path)
        removed = sync_task_issues.find_removed_phases(phases, feature)

        self.assertEqual([("phase-2", 3)], [(key, issue["number"]) for key, issue in removed])

    @patch.object(sync_task_issues, "gh")
    def test_retires_removed_phases_as_not_planned_once(self, gh):
        closed = sync_task_issues.retire_removed_phases(
            "repos/o/r",
            [
                ("phase-2", {"number": 2, "state": "open"}),
                ("phase-3", {"number": 3, "state": "closed"}),
            ],
            apply=True,
        )

        self.assertEqual(1, closed)
        self.assertIn("state_reason=not_planned", gh.call_args.args)

    @patch.object(sync_task_issues, "current_parent_number", return_value=5)
    @patch.object(sync_task_issues, "gh")
    def test_moves_phase_to_new_epic(self, gh, _parent):
        result = sync_task_issues.sync_parent(
            "repos/o/r",
            {"number": 6, "id": 60},
            {"number": 1, "id": 10},
            "epic",
            "Phase 2",
            True,
            {},
        )

        self.assertEqual("moved", result)
        self.assertIn("replace_parent=true", gh.call_args.args)


sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_github import FakeGitHub, OWNER, REPO, write_fixture_repo  # noqa: E402
import project_board  # noqa: E402

FEATURE_900 = "specs/900-example-feature"
TASKS_MD = """# Tasks: Issue Tracking API

## Phase 1: Setup

**Goal**: Schema and resolvers.

- [{0}] T001 Create the issue schema
- [{1}] T002 Add the issue resolvers (depends on T001)

## Phase 2: Polish

- [{2}] T003 Document the API
"""
EPIC_900 = "<!-- speckit-roadmap: 900 -->"
SPEC_PLAN_900 = "<!-- speckit-planning: 900#spec-plan -->\n\nPlanning body"
PHASE_1_900 = (
    f"{sync_task_issues.phase_marker(FEATURE_900, 'phase-1')}\n\nProvisional Phase 1 body"
)
PHASE_1_TITLE = "Phase 1: Kickoff — Issue Tracking (API)"


def phase_issue_body(number):
    return sync_task_issues.phase_marker(FEATURE_900, f"phase-{number}") + "\n\nbody"


class PhaseSyncTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = write_fixture_repo(Path(directory.name).resolve())
        self.write_tasks(" ", " ", " ")

    def write_tasks(self, *checks, feature_path=FEATURE_900, text=None):
        feature_dir = self.root / feature_path
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "spec.md").write_text("# Spec\n", encoding="utf-8")
        (feature_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (feature_dir / "tasks.md").write_text(
            text if text is not None else TASKS_MD.format(*checks), encoding="utf-8"
        )

    def initialized(self):
        """A roadmap epic, a merged spec-plan issue and the provisional Phase 1 issue."""
        fake = FakeGitHub()
        fake.add_issue(1, EPIC_900, title="Feature: 900 Issue Tracking (API)", label="Epic")
        fake.add_issue(2, SPEC_PLAN_900, title="SPEC: 900 Create and approve spec and plan",
                       parent=1, state="closed", state_reason="completed")
        fake.add_issue(3, PHASE_1_900, title=PHASE_1_TITLE, parent=1, label="Story")
        return fake

    def test_roadmap_only_skips_features_without_a_matching_entry(self):
        feature_path = "specs/999-untracked"
        self.write_tasks(" ", " ", " ", feature_path=feature_path)
        fake = self.initialized()

        result, output = self.run_sync(
            fake, "--roadmap-only", feature=feature_path
        )

        self.assertEqual(0, result)
        self.assertIn("no matching ROADMAP.md entry", output)
        self.assertEqual([], fake.calls)

    def test_roadmap_only_still_fails_when_roadmap_sync_should_create_the_epic(self):
        fake = FakeGitHub()

        result, output = self.run_sync(fake, "--roadmap-only", "--apply")

        self.assertNotEqual(0, result)
        self.assertIn(
            "Run `python3 scripts/sync-roadmap-issues.py --id 900 --apply` first", result
        )
        self.assertEqual([], fake.mutations())

    def test_roadmap_only_preview_warns_and_skips_when_epic_is_not_created_yet(self):
        fake = FakeGitHub()

        result, output = self.run_sync(fake, "--roadmap-only")

        self.assertEqual(0, result)
        # The prefix depends on GITHUB_ACTIONS; the two tests below pin each form.
        self.assertIn(
            "no managed roadmap Epic for 900 yet; "
            f"run sync-roadmap-issues.py --apply first. Skipping {FEATURE_900}.",
            output,
        )
        self.assertEqual([], fake.mutations())

    def test_preview_warning_is_an_annotation_inside_github_actions(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            result, output = self.run_sync(FakeGitHub(), "--roadmap-only")

        self.assertEqual(0, result)
        self.assertNotIn("Warning:", output)
        self.assertEqual(1, output.count("no managed roadmap Epic for 900"))
        self.assertIn(
            "::warning::no managed roadmap Epic for 900 yet; "
            f"run sync-roadmap-issues.py --apply first. Skipping {FEATURE_900}.",
            output,
        )

    def test_preview_warning_is_plain_outside_github_actions(self):
        env = {key: value for key, value in os.environ.items() if key != "GITHUB_ACTIONS"}
        with patch.dict(os.environ, env, clear=True):
            _, output = self.run_sync(FakeGitHub(), "--roadmap-only")

        self.assertIn("Warning: no managed roadmap Epic for 900 yet;", output)
        self.assertNotIn("::warning::", output)

    def test_manual_mode_skips_undecided_entry_without_an_epic(self):
        roadmap = self.root / "ROADMAP.md"
        roadmap.write_text(
            roadmap.read_text(encoding="utf-8").replace(
                "### 900 — Issue Tracking (API)  [status: planned]",
                "### 900 — Issue Tracking (API)  [status: undecided]",
            ),
            encoding="utf-8",
        )
        fake = FakeGitHub()

        result, output = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertIn("Skipping Phase sync for roadmap entry 900 (undecided).", output)
        self.assertNotIn("No managed roadmap Epic", output)
        self.assertEqual([], fake.mutations())

    def test_dry_run_labels_the_epic_update_as_epic(self):
        fake = self.initialized()
        fake.issues[1]["body"] = f"{EPIC_900}\n\nstale body"

        result, output = self.run_sync(fake)

        self.assertEqual(0, result)
        self.assertIn("[dry-run] update #1 (epic)", output)
        self.assertNotIn("(feature)", output)

    def test_roadmap_only_check_still_fails_when_epic_is_missing(self):
        fake = FakeGitHub()

        result, output = self.run_sync(fake, "--roadmap-only", "--check")

        self.assertIn("No managed roadmap Epic found for entry 900", result)
        self.assertNotIn("Warning:", output)
        self.assertEqual([], fake.mutations())

    def test_roadmap_only_skips_undecided_entries(self):
        roadmap = self.root / "ROADMAP.md"
        roadmap.write_text(
            roadmap.read_text(encoding="utf-8").replace(
                "### 900 — Issue Tracking (API)  [status: planned]",
                "### 900 — Issue Tracking (API)  [status: undecided]",
            ),
            encoding="utf-8",
        )
        fake = FakeGitHub()

        result, output = self.run_sync(fake, "--roadmap-only", "--apply")

        self.assertEqual(0, result)
        self.assertIn("(undecided)", output)
        self.assertEqual([], fake.calls)

    def test_manual_mode_without_roadmap_id_fails_before_any_network_read(self):
        feature_path = "specs/untracked"
        self.write_tasks(" ", " ", " ", feature_path=feature_path)
        fake = FakeGitHub()

        with patch.object(sync_task_issues, "load_issues") as load:
            result, _ = self.run_sync(fake, "--apply", feature=feature_path)

        self.assertIn("No epic issue tracks specs/untracked", result)
        self.assertIn("no roadmap ID prefix", result)
        load.assert_not_called()
        self.assertEqual([], fake.calls)

    def run_sync(
        self, fake, *argv, feature=FEATURE_900, skip_project=True
    ):
        output = io.StringIO()
        args = ["--feature", feature, *argv]
        if skip_project:
            args.append("--skip-project")
        with patch.object(sync_task_issues, "ROOT", self.root), patch.object(
            sync_task_issues, "gh", fake
        ), patch.object(project_board.sync, "gh", fake), patch.object(
            sync_task_issues, "origin_repo", return_value=(OWNER, REPO)
        ), patch.object(sync_task_issues.time, "sleep"), contextlib.redirect_stdout(output):
            try:
                result = sync_task_issues.main(args)
            except SystemExit as error:
                result = error.code
        return result, output.getvalue()

    def phases(self, fake):
        return {n: i for n, i in fake.issues.items() if i["label"] == "Story"}

    def test_workflow_uses_minimal_repository_permissions_and_project_token(self):
        workflow = (Path(__file__).parent.parent / ".github/workflows/sync-phase-issues.yml").read_text(
            encoding="utf-8"
        )
        permissions = workflow.split("permissions:\n", 1)[1].split("\njobs:", 1)[0]
        self.assertIn("contents: read", permissions)
        self.assertIn("issues: write", permissions)
        self.assertIn("pull-requests: read", permissions)
        self.assertNotIn("write-all", permissions)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertNotIn("PROJECT_SYNC_TOKEN", workflow)
        action = (Path(__file__).parent.parent / ".github/actions/board-token/action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("uses: ./.github/actions/board-token", workflow)
        self.assertIn("permission-organization-projects: write", action)
        self.assertIn("schedule:", workflow)
        self.assertIn("github.event_name == 'schedule'", workflow)
        self.assertIn("APPLY_REQUESTED: ${{ inputs.apply }}", workflow)
        self.assertIn("AUTO_APPLY_ENABLED: ${{ vars.PHASE_SYNC_AUTO_APPLY }}", workflow)
        self.assertIn('GITHUB_EVENT_NAME" == "pull_request"', workflow)
        self.assertIn('GITHUB_EVENT_NAME" == "schedule" && "$AUTO_APPLY_ENABLED" == "true"', workflow)
        self.assertIn("issue-sync-preview", workflow)
        self.assertIn("args=(--roadmap-only)", workflow)
        self.assertIn(
            "github.event.pull_request.base.ref == github.event.repository.default_branch",
            workflow,
        )
        self.assertIn("Failed to sync $feature; continuing with remaining features.", workflow)

    def test_updates_provisional_phase_1_and_creates_other_phases_under_epic(self):
        fake = self.initialized()
        planning_before = dict(fake.issues[2])

        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        phases = self.phases(fake)
        self.assertEqual(2, len(phases))
        self.assertEqual("Phase 1: Setup", fake.issues[3]["title"])
        self.assertIn("**Goal**: Schema and resolvers.", fake.issues[3]["body"])
        self.assertIn("- [ ] T002 Add the issue resolvers (depends on T001)", fake.issues[3]["body"])
        phase_2 = next(issue for issue in phases.values() if issue["title"] == "Phase 2: Polish")
        self.assertIn("<!-- speckit-roadmap-phase: 900#phase-2 -->", phase_2["body"])
        self.assertEqual(
            ["Phase 1: Setup", "Phase 2: Polish"], sorted(i["title"] for i in phases.values())
        )
        self.assertEqual([1, 1], [fake.parents[n] for n in phases])
        self.assertEqual(planning_before, fake.issues[2])
        self.assertEqual(1, [i["label"] for i in fake.issues.values()].count("Epic"))

    def test_updates_provisional_phase_1_when_spec_path_differs(self):
        fake = self.initialized()
        provisional_path = "specs/900-example-feature-api"
        fake.issues[3]["body"] = (
            f"{sync_task_issues.phase_marker(provisional_path, 'phase-1')}\n"
            f"{sync_task_issues.roadmap_issues.roadmap_phase_marker('900', 'phase-1')}\n\n"
            "Provisional Phase 1 body"
        )

        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertEqual("Phase 1: Setup", fake.issues[3]["title"])
        self.assertIn(
            sync_task_issues.phase_marker(FEATURE_900, "phase-1"), fake.issues[3]["body"]
        )
        self.assertNotIn(
            sync_task_issues.phase_marker(provisional_path, "phase-1"), fake.issues[3]["body"]
        )
        self.assertEqual(
            1,
            sum(
                sync_task_issues.roadmap_issues.roadmap_phase_marker("900", "phase-1")
                in (issue["body"] or "")
                for issue in fake.issues.values()
            ),
        )

    def test_creates_no_task_issues_and_leaves_unrelated_issues_alone(self):
        fake = self.initialized()
        fake.add_issue(50, "A note about the feature.", title="Unrelated", parent=3)
        before = dict(fake.issues[50])

        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertEqual(before, fake.issues[50])
        self.assertEqual(3, fake.parents[50])
        self.assertFalse(any("issues/50" in arg for call in fake.mutations() for arg in call))
        self.assertFalse(any("dependencies" in arg for call in fake.calls for arg in call))
        self.assertEqual(
            0,
            len([c for c in fake.created() if any(a.startswith("T0") for a in c)]),
        )

    def test_moves_a_phase_from_another_epic_to_its_roadmap_epic(self):
        fake = self.initialized()
        fake.add_issue(9, "A different epic.", title="Other epic", label="Epic")
        fake.parents[3] = 9

        result, output = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertEqual(1, fake.parents[3])
        self.assertIn("moved from #9", output)
        move = next(c for c in fake.calls
                    if "issues/1/sub_issues" in " ".join(c) and "sub_issue_id=3000" in c)
        self.assertIn("replace_parent=true", move)

    def test_fake_refuses_a_second_parent_without_replace_parent(self):
        fake = self.initialized()
        fake.add_issue(9, "A different epic.", title="Other epic", label="Epic")

        result = fake("api", "-X", "POST", f"repos/{OWNER}/{REPO}/issues/9/sub_issues",
                      "-F", "sub_issue_id=3000", check=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("HTTP 422", result.stderr)
        self.assertEqual(1, fake.parents[3])

    def test_skips_phase_sync_for_paused_roadmap_entries(self):
        roadmap_path = self.root / "ROADMAP.md"
        original_roadmap = roadmap_path.read_text(encoding="utf-8")
        for status in ("deferred", "needs-info", "abandoned"):
            with self.subTest(status=status):
                roadmap_path.write_text(
                    original_roadmap.replace(
                        "### 900 — Issue Tracking (API)  [status: planned]",
                        f"### 900 — Issue Tracking (API)  [status: {status}]",
                    ),
                    encoding="utf-8",
                )
                fake = self.initialized()

                result, output = self.run_sync(fake, "--apply")

                self.assertEqual(0, result)
                self.assertIn(f"Skipping Phase sync for roadmap entry 900 ({status})", output)
                self.assertEqual([], fake.mutations())

    def test_roadmap_epic_body_lists_only_spec_plan(self):
        fake = self.initialized()
        self.run_sync(fake, "--apply")

        entry = sync_task_issues.roadmap_issues.load_roadmap_entry(self.root, "900")
        expected = sync_task_issues.roadmap_issues.roadmap_epic_body(
            entry,
            {"spec-plan": 2},
            FEATURE_900,
            sync_task_issues.roadmap_issues.existing_artifacts(self.root, FEATURE_900),
        )
        self.assertEqual(expected, fake.issues[1]["body"])
        self.assertIn("#2", fake.issues[1]["body"])

    def test_check_reports_out_of_sync_then_in_sync(self):
        fake = self.initialized()

        result, output = self.run_sync(fake, "--check")
        self.assertEqual(1, result)
        self.assertEqual([], fake.mutations())
        self.assertIn("out of sync", output)

        self.run_sync(fake, "--apply")
        mutations = len(fake.mutations())
        result, output = self.run_sync(fake, "--check")
        self.assertEqual(0, result)
        self.assertEqual(mutations, len(fake.mutations()))
        self.assertIn("in sync", output)

    def test_check_and_apply_are_exclusive(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result, _ = self.run_sync(FakeGitHub(), "--check", "--apply")
        self.assertEqual(2, result)
        self.assertIn("not allowed with argument", errors.getvalue())

    def test_rerun_is_idempotent(self):
        fake = self.initialized()
        self.run_sync(fake, "--apply")
        fake.calls.clear()

        self.run_sync(fake, "--apply")

        self.assertEqual([], fake.mutations())

    def test_phases_enter_project_as_planned_without_changing_other_statuses(self):
        fake = self.initialized()
        fake.project_items[3] = {"id": "PVTI_3", "status": "In progress", "order": None}

        self.run_sync(
            fake, "--apply", "--project-owner", "example-org", "--project-number", "123",
            skip_project=False,
        )

        phases = self.phases(fake)
        self.assertEqual({"In progress", "Planned"}, {fake.project_items[n]["status"] for n in phases})
        self.assertEqual("In progress", fake.project_items[3]["status"])
        self.assertNotIn(1, fake.project_items)

    def test_project_dry_run_previews_without_mutating(self):
        fake = self.initialized()

        result, output = self.run_sync(
            fake, "--check", "--project-owner", "example-org", "--project-number", "123",
            skip_project=False,
        )

        self.assertEqual(1, result)
        self.assertIn("add #3 to project example-org/123 as Planned", output)
        self.assertEqual([], fake.mutations())

    def test_epic_stays_open_until_spec_plan_is_completed_and_phases_complete(self):
        self.write_tasks("x", "x", "x")
        cases = {
            "open": {"state": "open", "state_reason": None},
            "not_planned": {"state": "closed", "state_reason": "not_planned"},
        }
        for name, values in cases.items():
            with self.subTest(name):
                fake = self.initialized()
                fake.issues[2].update(values)
                _, output = self.run_sync(fake, "--apply")
                self.assertEqual("open", fake.issues[1]["state"])
                self.assertIn("stays open", output)

        fake = self.initialized()
        del fake.issues[2]
        _, output = self.run_sync(fake, "--apply")
        self.assertEqual("open", fake.issues[1]["state"])
        self.assertIn("missing", output)

    def test_epic_stays_open_while_a_phase_has_unchecked_tasks(self):
        self.write_tasks("x", "x", " ")
        fake = self.initialized()

        _, output = self.run_sync(fake, "--apply")

        self.assertEqual("open", fake.issues[1]["state"])
        self.assertIn("stays open", output)
        self.assertEqual("closed", fake.issues[3]["state"])
        open_phases = [i for i in self.phases(fake).values() if i["state"] == "open"]
        self.assertEqual(["Phase 2: Polish"], [i["title"] for i in open_phases])

    def test_phases_and_epic_close_when_everything_is_checked(self):
        self.write_tasks("x", "x", "x")
        fake = self.initialized()

        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        for number in (*self.phases(fake), 1):
            self.assertEqual(("closed", "completed"),
                             (fake.issues[number]["state"], fake.issues[number]["state_reason"]))

    def test_unchecked_work_reopens_a_completed_phase(self):
        self.write_tasks("x", "x", "x")
        fake = self.initialized()
        self.run_sync(fake, "--apply")
        self.write_tasks("x", " ", "x")

        result, output = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertEqual(("open", None), (fake.issues[3]["state"], fake.issues[3]["state_reason"]))
        self.assertIn("- [ ] T002", fake.issues[3]["body"])
        self.assertIn("epic #1 reopened", output)
        self.assertEqual("open", fake.issues[1]["state"])

    BOARD = ("--project-owner", "example-org", "--project-number", "123")

    def test_closing_a_complete_phase_sets_its_board_status_to_done(self):
        self.write_tasks("x", "x", "x")
        fake = self.initialized()
        fake.project_items[3] = {"id": "PVTI_3", "status": "In review", "order": None}

        self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)

        self.assertEqual("closed", fake.issues[3]["state"])
        self.assertEqual("Done", fake.project_items[3]["status"])

    def test_reopening_a_phase_resets_its_board_status_to_the_open_phase_rule(self):
        self.write_tasks("x", "x", "x")
        fake = self.initialized()
        fake.project_items[3] = {"id": "PVTI_3", "status": "Done", "order": None}
        fake.issues[3].update(state="closed", state_reason="completed")
        self.write_tasks("x", " ", "x")

        self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)

        self.assertEqual("open", fake.issues[3]["state"])
        self.assertEqual("Planned", fake.project_items[3]["status"])

    def test_dry_run_previews_the_board_status_without_mutating(self):
        self.write_tasks("x", "x", "x")
        fake = self.initialized()
        fake.project_items[3] = {"id": "PVTI_3", "status": "In review", "order": None}

        _, output = self.run_sync(fake, "--check", *self.BOARD, skip_project=False)

        self.assertIn("[dry-run] #3: In review -> Done", output)
        self.assertEqual("In review", fake.project_items[3]["status"])
        self.assertEqual([], fake.mutations())

    def test_no_board_status_is_touched_when_the_project_is_skipped(self):
        self.write_tasks("x", "x", "x")
        fake = self.initialized()
        fake.project_items[3] = {"id": "PVTI_3", "status": "In review", "order": None}

        self.run_sync(fake, "--apply", skip_project=True)

        self.assertEqual("In review", fake.project_items[3]["status"])

    def test_completed_phase_content_updates_after_tasks_are_added(self):
        fake = self.initialized()
        fake.issues[3].update(state="closed", state_reason="completed")
        self.write_tasks(
            "x",
            "x",
            "x",
            text=TASKS_MD.format("x", "x", "x").replace(
                "Phase 1: Setup", "Phase 1: Foundations"
            ),
        )

        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertEqual("Phase 1: Foundations", fake.issues[3]["title"])
        self.assertIn("Phase 1: Foundations", fake.issues[3]["body"])
        self.assertEqual(
            ("closed", "completed"),
            (fake.issues[3]["state"], fake.issues[3]["state_reason"]),
        )

    def test_new_phase_added_later_is_created_and_blocks_epic(self):
        self.write_tasks("x", "x", "x")
        fake = self.initialized()
        self.run_sync(fake, "--apply")
        self.write_tasks(
            text=TASKS_MD.format("x", "x", "x") + "\n## Phase 4: Extra\n\n- [ ] T004 More\n"
        )

        self.run_sync(fake, "--apply")

        extra = next(i for i in fake.issues.values() if i["title"] == "Phase 4: Extra")
        self.assertEqual(1, fake.parents[extra["number"]])
        self.assertEqual("open", extra["state"])

    def test_renamed_heading_updates_title_but_keeps_issue(self):
        fake = self.initialized()
        self.run_sync(fake, "--apply")
        self.write_tasks(text=TASKS_MD.format(" ", " ", " ").replace("Setup", "Foundations"))

        self.run_sync(fake, "--apply")

        self.assertEqual("Phase 1: Foundations", fake.issues[3]["title"])
        self.assertEqual(2, len(self.phases(fake)))

    def test_removed_planned_phase_is_retired_and_does_not_block_epic(self):
        fake = self.initialized()
        fake.add_issue(7, phase_issue_body(3), title="Phase 3: Gone", parent=1, label="Story")
        self.write_tasks("x", "x", "x")

        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertEqual(("closed", "not_planned"),
                         (fake.issues[7]["state"], fake.issues[7]["state_reason"]))
        self.assertIn(sync_task_issues.PHASE_RETIRED_MARKER, fake.issues[7]["body"])
        self.assertEqual("closed", fake.issues[1]["state"])

    def retired_phase_on_board(self):
        fake = self.initialized()
        fake.add_issue(7, phase_issue_body(3), title="Phase 3: Gone", parent=1, label="Story")
        fake.project_items[7] = {"id": "PVTI_7", "status": "Planned", "order": None}
        self.write_tasks("x", "x", "x")
        return fake

    def test_retiring_a_removed_phase_removes_it_from_the_board(self):
        fake = self.retired_phase_on_board()

        self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)

        self.assertEqual("not_planned", fake.issues[7]["state_reason"])
        self.assertNotIn(7, fake.project_items)
        self.assertEqual([], [c for c in fake.calls if c[:1] == ("project",)])

    def test_an_already_retired_phase_still_on_the_board_is_removed_by_a_later_run(self):
        fake = self.retired_phase_on_board()
        fake.issues[7].update(
            state="closed", state_reason="not_planned",
            body=phase_issue_body(3) + "\n\n" + sync_task_issues.PHASE_RETIRED_MARKER,
        )
        fake.project_items[7]["status"] = "Done"

        self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)

        self.assertNotIn(7, fake.project_items)

    def test_a_paused_phase_keeps_its_board_card(self):
        fake = self.retired_phase_on_board()
        fake.issues[7].update(
            state="closed", state_reason="not_planned",
            body=phase_issue_body(3) + "\n" + sync_task_issues.PHASE_PAUSED_MARKER,
        )
        self.write_tasks("x", "x", "x", text=TASKS_MD.format("x", "x", "x") + "\n## Phase 3: Gone\n\n- [ ] T009 Back\n")

        self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)

        self.assertIn(7, fake.project_items)

    def test_dry_run_previews_the_removal_without_mutating(self):
        fake = self.retired_phase_on_board()

        _, output = self.run_sync(fake, "--check", *self.BOARD, skip_project=False)

        self.assertIn("[dry-run] remove #7 from project example-org/123", output)
        self.assertIn(7, fake.project_items)
        self.assertEqual([], fake.mutations())

    def test_the_board_is_not_touched_when_the_project_is_skipped(self):
        fake = self.retired_phase_on_board()

        self.run_sync(fake, "--apply", skip_project=True)

        self.assertIn(7, fake.project_items)

    def test_a_retired_phase_reopened_by_hand_returns_to_the_board_as_planned(self):
        fake = self.retired_phase_on_board()
        self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)
        self.assertNotIn(7, fake.project_items)
        fake.issues[7].update(state="open", state_reason=None)
        self.write_tasks("x", "x", "x", text=TASKS_MD.format("x", "x", "x") + "\n## Phase 3: Gone\n\n- [ ] T009 Back\n")

        self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)

        self.assertEqual("Planned", fake.project_items[7]["status"])

    def test_dry_run_previews_removal_of_a_paused_phase_removed_from_tasks(self):
        fake = self.initialized()
        fake.add_issue(7, phase_issue_body(3), title="Phase 3: Gone", parent=1, label="Story")
        fake.issues[7].update(
            state="closed", state_reason="not_planned",
            body=phase_issue_body(3) + "\n" + sync_task_issues.PHASE_PAUSED_MARKER,
        )
        fake.project_items[7] = {"id": "PVTI_7", "status": "Backlog", "order": None}
        self.write_tasks("x", "x", "x")

        _, output = self.run_sync(fake, "--check", *self.BOARD, skip_project=False)

        self.assertIn("mark #7 (phase-3) as retired", output)
        self.assertIn("[dry-run] remove #7 from project example-org/123", output)
        self.assertIn(7, fake.project_items)
        self.assertEqual([], fake.mutations())

    def test_apply_retires_a_paused_removed_phase_and_removes_its_card(self):
        fake = self.initialized()
        fake.add_issue(7, phase_issue_body(3), title="Phase 3: Gone", parent=1, label="Story")
        fake.issues[7].update(
            state="closed", state_reason="not_planned",
            body=phase_issue_body(3) + "\n" + sync_task_issues.PHASE_PAUSED_MARKER,
        )
        fake.project_items[7] = {"id": "PVTI_7", "status": "Backlog", "order": None}
        self.write_tasks("x", "x", "x")

        result, _ = self.run_sync(fake, "--apply", *self.BOARD, skip_project=False)

        self.assertEqual(0, result)
        self.assertNotIn(sync_task_issues.PHASE_PAUSED_MARKER, fake.issues[7]["body"])
        self.assertIn(sync_task_issues.PHASE_RETIRED_MARKER, fake.issues[7]["body"])
        self.assertEqual(("closed", "not_planned"),
                         (fake.issues[7]["state"], fake.issues[7]["state_reason"]))
        self.assertNotIn(7, fake.project_items)
        deletions = [c for c in fake.calls if any("deleteProjectV2Item" in a for a in c)]
        self.assertEqual(1, len(deletions))
        self.assertIn("itemId=PVTI_7", deletions[0])

    def test_removed_paused_phase_becomes_retired_and_cannot_be_reused(self):
        fake = self.initialized()
        self.run_sync(fake, "--apply")
        phase2 = fake.issues[4]
        phase2.update(state="closed", state_reason="not_planned")
        phase2["body"] += f"\n\n{sync_task_issues.PHASE_PAUSED_MARKER}\n"
        self.write_tasks(
            text="# Tasks: Issue Tracking API\n\n## Phase 1: Setup\n\n- [ ] T001 Create schema\n"
        )

        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertNotIn(sync_task_issues.PHASE_PAUSED_MARKER, phase2["body"])
        self.assertIn(sync_task_issues.PHASE_RETIRED_MARKER, phase2["body"])

        self.write_tasks(" ", " ", " ")
        result, _ = self.run_sync(fake, "--apply")

        self.assertIsInstance(result, str)
        self.assertIn("phase-2 (#4)", result)
        self.assertIn("never reused", result)

    def test_removed_active_phase_stops_without_changes(self):
        cases = {
            "In progress": lambda fake: fake.branches.append("7-example-feature-phase-3-gone"),
            "In review": lambda fake: fake.add_pull_request(70, "PR", body="Closes #7"),
            "Done": lambda fake: fake.issues[7].update(state="closed", state_reason="completed"),
        }
        for status, arrange in cases.items():
            with self.subTest(status):
                fake = self.initialized()
                fake.add_issue(7, phase_issue_body(3), title="Phase 3: Gone", parent=1, label="Story")
                arrange(fake)

                result, _ = self.run_sync(fake, "--apply")

                self.assertIsInstance(result, str)
                self.assertIn(f"phase-3 (#7, {status})", result)
                self.assertIn("close its issue as not planned", result)
                self.assertEqual([], fake.mutations())

    def removed_phase_3(self, **pull_request):
        fake = self.initialized()
        fake.add_issue(7, phase_issue_body(3), title="Phase 3: Gone", parent=1, label="Story")
        fake.add_pull_request(70, "PR", body="Part of #7", **pull_request)
        return fake, self.run_sync(fake, "--apply")[0]

    def test_trusted_fork_phase_pull_request_blocks_removal_as_in_review(self):
        _, result = self.removed_phase_3(
            head_ref="7-example-feature-phase-3-gone", head_repo="fork/r",
            author_association="COLLABORATOR",
        )

        self.assertIsInstance(result, str)
        self.assertIn("phase-3 (#7, In review)", result)

    def test_untrusted_fork_phase_pull_request_does_not_block_removal(self):
        fake, result = self.removed_phase_3(
            head_ref="7-example-feature-phase-3-gone", head_repo="fork/r",
            author_association="NONE",
        )

        self.assertNotIn("phase-3 (#7", str(result))
        self.assertEqual("closed", fake.issues[7]["state"])

    def test_same_repository_phase_pull_request_reports_in_review_not_in_progress(self):
        fake = self.initialized()
        fake.add_issue(7, phase_issue_body(3), title="Phase 3: Gone", parent=1, label="Story")
        fake.branches.append("7-example-feature-phase-3-gone")
        fake.add_pull_request(70, "PR", body="Part of #7", head_ref="7-example-feature-phase-3-gone")

        result, _ = self.run_sync(fake, "--apply")

        self.assertIn("phase-3 (#7, In review)", result)

    def test_retirement_comment_is_not_duplicated_when_close_fails(self):
        fake = self.initialized()
        self.run_sync(fake, "--apply")
        self.write_tasks(
            text="# Tasks: Issue Tracking API\n\n## Phase 1: Setup\n\n- [ ] T001 Create schema\n"
        )
        failed_once = False

        def fail_close_once(args):
            nonlocal failed_once
            if (
                not failed_once
                and args[:3] == ("api", "-X", "PATCH")
                and args[3] == "repos/o/r/issues/4"
                and "state=closed" in args
            ):
                failed_once = True
                return True
            return False

        fake.fail_on = fail_close_once
        self.run_sync(fake, "--apply")
        self.assertEqual(1, len(fake.comments[4]))
        fake.fail_on = None

        self.run_sync(fake, "--apply")

        self.assertEqual(1, len(fake.comments[4]))
        self.assertEqual("closed", fake.issues[4]["state"])

    def test_retired_phase_number_cannot_be_reused(self):
        fake = self.initialized()
        fake.add_issue(8, phase_issue_body(2), title="Phase 2: Old", parent=1, label="Story",
                       state="closed", state_reason="not_planned")

        result, _ = self.run_sync(fake, "--apply")

        self.assertIsInstance(result, str)
        self.assertIn("phase-2 (#8)", result)
        self.assertIn("never reused", result)
        self.assertEqual([], fake.mutations())

    def test_epic_is_required(self):
        fake = FakeGitHub()
        result, _ = self.run_sync(fake, "--apply")
        self.assertIn("scripts/sync-roadmap-issues.py --id 900 --apply", result)
        self.assertEqual([], fake.mutations())

    def test_feature_path_marker_alone_is_not_an_epic(self):
        fake = FakeGitHub()
        fake.add_issue(
            1, f"<!-- speckit-feature: {FEATURE_900} -->", title="Old", label="Epic"
        )

        result, _ = self.run_sync(fake, "--apply")

        self.assertIn("No managed roadmap Epic found for entry 900", result)
        self.assertEqual([], fake.mutations())

    def test_multiple_roadmap_epics_fail(self):
        fake = self.initialized()
        fake.add_issue(9, EPIC_900)
        result, _ = self.run_sync(fake, "--apply")
        self.assertIn("#1, #9", result)
        self.assertEqual([], fake.mutations())

    def test_multiple_roadmap_epics_explain_marker_removal(self):
        fake = self.initialized()
        fake.add_issue(9, EPIC_900, state="closed", state_reason="not_planned")
        result, _ = self.run_sync(fake, "--apply")
        self.assertIn(EPIC_900, result)
        self.assertIn("Remove the marker", result)
        self.assertIn("closed issues still match", result)

    def test_duplicate_phase_issues_explain_marker_removal(self):
        fake = self.initialized()
        fake.add_issue(10, PHASE_1_900)

        result, _ = self.run_sync(fake, "--apply")

        self.assertIn("#3, #10", result)
        self.assertIn("closed issues still match", result)
        self.assertEqual([], fake.mutations())

    def test_roadmap_epic_requires_roadmap_entry(self):
        (self.root / "ROADMAP.md").write_text("# Roadmap\n\n## Planned Specs\n", encoding="utf-8")
        result, _ = self.run_sync(self.initialized(), "--apply")
        self.assertIn("900", result)
        self.assertIn("ROADMAP.md", result)

    def test_rerun_after_phase_creation_failure_keeps_one_epic(self):
        fake = self.initialized()
        fake.fail_on = lambda args: (
            args[:2] == ("issue", "create") and "Phase 2" in args[args.index("--title") + 1]
        )

        result, _ = self.run_sync(fake, "--apply")
        self.assertIsInstance(result, str)

        fake.fail_on = None
        result, _ = self.run_sync(fake, "--apply")

        self.assertEqual(0, result)
        self.assertEqual(1, len([i for i in fake.issues.values() if EPIC_900 in i["body"]]))
        self.assertEqual(2, len(self.phases(fake)))

    @patch.object(sync_task_issues, "gh")
    def test_load_issues_reads_state_reason(self, gh):
        gh.return_value.stdout = ""
        sync_task_issues.load_issues("repos/o/r")
        self.assertIn("state_reason", gh.call_args.args[-1])
        self.assertIn("labels", gh.call_args.args[-1])


class ProductionJqFilterTest(unittest.TestCase):
    """Run the production `--jq` filters (through the real jq) against raw REST-shaped data."""

    def test_pull_requests_in_the_issue_listing_are_dropped(self):
        fake = FakeGitHub()
        fake.add_issue(1, "An issue", title="Real issue")
        fake.add_pull_request(2, "A pull request", body="<!-- speckit-roadmap: 900 -->")

        raw = json.loads(fake("api", "--paginate",
                              f"repos/{OWNER}/{REPO}/issues?state=all&per_page=100").stdout)
        self.assertEqual([1, 2], [item["number"] for item in raw])
        self.assertIn("pull_request", raw[1])

        with patch.object(sync_task_issues, "gh", fake):
            loaded = sync_task_issues.load_issues(f"repos/{OWNER}/{REPO}")

        self.assertEqual([1], [issue["number"] for issue in loaded])

    def test_filter_flattens_author_assignees_and_labels(self):
        fake = FakeGitHub()
        fake.add_issue(1, "An issue", author="dev", author_association="MEMBER",
                       assignees=["alice"], labels=["Epic", "bug"])

        with patch.object(sync_task_issues, "gh", fake):
            [issue] = sync_task_issues.load_issues(f"repos/{OWNER}/{REPO}")

        self.assertEqual("dev", issue["author"])
        self.assertEqual("MEMBER", issue["author_association"])
        self.assertEqual(["alice"], issue["assignees"])
        self.assertEqual(["Epic", "bug"], issue["labels"])
        self.assertIsNone(issue["state_reason"])

    def test_an_outsider_marker_is_not_trusted_after_loading_through_the_filter(self):
        fake = FakeGitHub()
        marker = "<!-- speckit-roadmap: 900 -->"
        fake.add_issue(1, marker, author="stranger", author_association="NONE")
        fake.add_issue(2, marker, author="dev", author_association="MEMBER")
        fake.add_issue(3, marker, author="app[bot]", author_association="NONE")
        fake.add_issue(4, marker, author="gone", author_association="NONE", labels=["Epic"])

        with patch.object(sync_task_issues, "gh", fake):
            loaded = sync_task_issues.load_issues(f"repos/{OWNER}/{REPO}")

        trusted = {issue["number"]: sync_task_issues.roadmap_issues.is_trusted_author(issue)
                   for issue in loaded}
        self.assertEqual({1: False, 2: True, 3: True, 4: True}, trusted)

    def test_parent_blockers_and_comments_filters_read_the_raw_responses(self):
        fake = FakeGitHub()
        fake.add_issue(1, "Epic")
        fake.add_issue(2, "Phase", parent=1)
        fake.add_issue(3, "Blocker")
        fake.blockers[2] = {3}
        fake.comments[2] = ["first", "second"]
        api = f"repos/{OWNER}/{REPO}"

        with patch.object(sync_task_issues, "gh", fake):
            self.assertEqual(1, sync_task_issues.current_parent_number(api, {"number": 2}))
            self.assertIsNone(sync_task_issues.current_parent_number(api, {"number": 1}))
            self.assertEqual({3}, sync_task_issues.current_blocker_numbers(api, {"number": 2}))
            self.assertTrue(sync_task_issues.github_issue_comments.ensure_issue_comment(
                fake, api, 2, "new", "<!-- m -->", True))
            self.assertFalse(sync_task_issues.github_issue_comments.ensure_issue_comment(
                fake, api, 2, "first", "<!-- m -->", True))

    def test_the_fake_fails_loudly_without_jq(self):
        fake = FakeGitHub()
        with patch("fake_github.shutil.which", return_value=None):
            with self.assertRaisesRegex(AssertionError, "jq"):
                fake("api", "user", "--jq", ".login")


class FakeGitHubStrictnessTest(unittest.TestCase):
    """The fake rejects what real `gh` rejects, and what would silently truncate a listing."""

    API = f"repos/{OWNER}/{REPO}"

    def test_slurp_without_paginate_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "--paginate.*--slurp"):
            FakeGitHub()("api", "user", "--slurp")

    def test_slurp_together_with_jq_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "--slurp.*not supported.*--jq"):
            FakeGitHub()("api", "--paginate", "--slurp", f"{self.API}/branches?per_page=100",
                         "--jq", ".")

    def test_a_filter_that_does_not_compile_fails_loudly_even_with_check_false(self):
        with self.assertRaisesRegex(AssertionError, r"\.\[\]\.nmae \|\| \|"):
            FakeGitHub()("api", "user", "--jq", ".[].nmae || |", check=False)

    def test_a_filter_runtime_error_is_a_failed_call_like_gh(self):
        result = FakeGitHub()("api", "user", "--jq", ".login | error(\"boom\")", check=False)

        self.assertNotEqual(0, result.returncode)

    def test_listing_endpoints_require_paginate(self):
        fake = FakeGitHub()
        fake.add_issue(1, "An issue")
        for path in (f"{self.API}/branches?per_page=100", f"{self.API}/pulls?state=open&per_page=100",
                     f"{self.API}/issues?state=all&per_page=100", f"{self.API}/issues/1/comments",
                     f"{self.API}/issues/1/dependencies/blocked_by"):
            with self.subTest(path=path), self.assertRaisesRegex(AssertionError, "--paginate"):
                fake("api", path)

    def test_a_multi_page_listing_is_read_completely_through_the_production_loader(self):
        fake = FakeGitHub()
        fake.page_size = 2
        for number in range(1, 6):
            fake.add_issue(number, f"Issue {number}")

        with patch.object(sync_task_issues, "gh", fake):
            loaded = sync_task_issues.load_issues(self.API)

        self.assertEqual([1, 2, 3, 4, 5], [issue["number"] for issue in loaded])

    def test_slurp_wraps_each_page_in_one_array(self):
        fake = FakeGitHub()
        fake.page_size = 2
        for number in range(1, 6):
            fake.add_issue(number, f"Issue {number}")

        pages = json.loads(fake("api", "--paginate", "--slurp",
                                f"{self.API}/issues?state=all&per_page=100").stdout)

        self.assertEqual([2, 2, 1], [len(page) for page in pages])

    def _page_lengths(self, query):
        fake = FakeGitHub()
        for number in range(1, 251):
            fake.add_issue(number, f"Issue {number}")
        path = f"{self.API}/issues?state=all{query}"
        pages = json.loads(fake("api", "--paginate", "--slurp", path).stdout)
        return [len(page) for page in pages]

    def test_without_per_page_pages_hold_30_items_like_github(self):
        self.assertEqual([30] * 8 + [10], self._page_lengths(""))

    def test_per_page_100_gives_pages_of_100(self):
        self.assertEqual([100, 100, 50], self._page_lengths("&per_page=100"))

    def test_per_page_above_100_is_capped_at_100(self):
        self.assertEqual([100, 100, 50], self._page_lengths("&per_page=500"))

    def test_per_page_is_read_from_the_endpoint_path_only(self):
        fake = FakeGitHub()
        for number in range(1, 41):
            fake.add_issue(number, f"Issue {number}")

        output = fake("api", "--paginate", f"{self.API}/issues?state=all",
                      "--jq", '"?per_page=7" as $x | length').stdout.split()

        self.assertEqual(["30", "10"], output)

    def test_the_page_size_override_wins_over_per_page(self):
        fake = FakeGitHub()
        fake.page_size = 2
        for number in range(1, 6):
            fake.add_issue(number, f"Issue {number}")

        pages = json.loads(fake("api", "--paginate", "--slurp",
                                f"{self.API}/issues?state=all&per_page=100").stdout)

        self.assertEqual([2, 2, 1], [len(page) for page in pages])

    def test_the_truncation_error_names_the_page_size_of_the_request(self):
        fake = FakeGitHub()
        with self.assertRaisesRegex(AssertionError, "at most 30 items"):
            fake("api", f"{self.API}/issues/1/comments")
        with self.assertRaisesRegex(AssertionError, "at most 50 items"):
            fake("api", f"{self.API}/branches?per_page=50")

    def test_a_paginated_blocker_listing_with_per_page_reads_every_blocker(self):
        # A `?per_page=...` query string must not change the route.
        fake = FakeGitHub()
        for number in range(1, 41):
            fake.add_issue(number, f"Issue {number}")
        fake.add_issue(41, "Blocked")
        fake.blockers[41] = set(range(1, 41))

        numbers = fake("api", "--paginate",
                       f"{self.API}/issues/41/dependencies/blocked_by?per_page=30",
                       "--jq", ".[].number").stdout.split()

        self.assertEqual([str(n) for n in range(1, 41)], numbers)

    def test_current_blocker_numbers_reads_more_than_one_page_of_blockers(self):
        fake = FakeGitHub()
        for number in range(1, 41):
            fake.add_issue(number, f"Issue {number}")
        fake.add_issue(41, "Blocked")
        fake.blockers[41] = set(range(1, 41))

        with patch.object(sync_task_issues, "gh", fake):
            blockers = sync_task_issues.current_blocker_numbers(self.API, {"number": 41})

        self.assertEqual(set(range(1, 41)), blockers)

    def test_an_empty_listing_is_one_empty_page(self):
        pages = json.loads(FakeGitHub()("api", "--paginate", "--slurp",
                                        f"{self.API}/branches?per_page=100").stdout)

        self.assertEqual([[]], pages)


if __name__ == "__main__":
    unittest.main()
