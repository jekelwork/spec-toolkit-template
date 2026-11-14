import contextlib
import importlib.util
import io
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_github import FakeGitHub, OWNER, REPO  # noqa: E402
# Importing project_board registers sync_task_issues in sys.modules, which the import below relies on.
import project_board  # noqa: E402
import roadmap_issues  # noqa: E402
import sync_task_issues  # noqa: E402

SCRIPT_PATH = Path(__file__).with_name("sync-roadmap-issues.py")
SPEC = importlib.util.spec_from_file_location("sync_roadmap_issues", SCRIPT_PATH)
sync_roadmap_issues = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_roadmap_issues)


def roadmap_entry(roadmap_id, title, status="planned", depends="", spec_dir=""):
    return f"""### {roadmap_id} — {title}  [status: {status}]

- **Description:** Description for {title}.
- **Outcome:** Outcome for {title}.
- **Depends on:** {depends}
- **Spec dir:** {spec_dir}
"""


class RoadmapIssueSyncTest(unittest.TestCase):
    def setUp(self):
        roadmap_issues.WARNED_ISSUES.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fake = FakeGitHub()
        self.write_roadmap(
            roadmap_entry("900", "Planned Feature", depends="901")
            + roadmap_entry("901", "Dependency Feature")
            + roadmap_entry("902", "Needs Information", "needs-info")
            + roadmap_entry("903", "Undecided Feature", "undecided")
            + roadmap_entry("904", "Deferred Feature", "deferred")
            + roadmap_entry("905", "Abandoned Feature", "abandoned")
        )

    def write_roadmap(self, text):
        (self.root / "ROADMAP.md").write_text("# Roadmap\n\n" + text, encoding="utf-8")
        (self.root / "specs").mkdir(exist_ok=True)

    def test_non_plan_status_is_rejected_before_any_github_mutation(self):
        self.write_roadmap(
            roadmap_entry("900", "Valid Feature")
            + roadmap_entry("901", "Stale Progress", "implemented")
        )
        with self.assertRaises(SystemExit):
            self.run_sync("--apply")
        self.assertEqual(self.fake.issues, {})

    def test_planned_completed_epic_is_not_reopened_or_duplicated(self):
        self.run_sync("--apply", "--id", "900")
        issue = self.find(roadmap_issues.roadmap_marker("900"))
        issue["state"] = "closed"
        issue["state_reason"] = "completed"
        numbers = set(self.fake.issues)
        self.run_sync("--apply", "--id", "900")
        self.assertEqual(set(self.fake.issues), numbers)
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(issue["state_reason"], "completed")

    def test_board_workflows_use_configured_owner_and_scoped_token(self):
        workflows = Path(__file__).parent.parent / ".github/workflows"
        for name in ("sync-roadmap-issues.yml", "sync-spec-tasks.yml"):
            with self.subTest(workflow=name):
                content = (workflows / name).read_text(encoding="utf-8")
                self.assertIn("owner: ${{ steps.project-config.outputs.owner }}", content)
                self.assertNotRegex(content, r"owner: (?!\$\{\{)")
                self.assertIn("permission-issues: read", content)
                self.assertIn("permission-organization-projects: write", content)
                self.assertIn("secrets.PROJECT_STATUS_APP_ID", content)
                self.assertIn("secrets.PROJECT_BOARD_APP_PRIVATE_KEY", content)
                self.assertIn("--repository-owner", content)
                self.assertIn("repositories: ${{ steps.repo.outputs.name }}", content)

    def test_read_only_roadmap_previews_use_separate_concurrency_group(self):
        workflow = (
            Path(__file__).parent.parent / ".github/workflows/sync-roadmap-issues.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("'issue-sync-preview'", workflow)
        self.assertIn("'issue-sync'", workflow)
        self.assertIn("inputs.apply == true", workflow)

    def test_project_defaults_come_from_shared_context(self):
        project_owner = "configured-org"
        project_number = 42
        self.fake.project_owner = project_owner
        self.fake.project_number = project_number
        context = {"github_project": {"owner": project_owner, "number": project_number}}

        with patch.object(sync_task_issues.project_context, "load_context", return_value=context):
            _, output = self.run_sync("--id", "900", skip_project=False)

        self.assertIn(f"to project {project_owner}/{project_number}", output)

    def run_sync(self, *argv, skip_project=True):
        output = io.StringIO()
        command = list(argv)
        if skip_project:
            command.insert(0, "--skip-project")
        context = self.root / "project-context.toml"
        context.write_text(
            '[project]\nname = "Example"\ndescription = "An example project"\n\n'
            '[github_project]\nowner = "example-org"\nnumber = 123\n',
            encoding="utf-8",
        )
        with (
            patch.object(sync_task_issues.project_context, "DEFAULT_CONFIG", context),
            patch.object(sync_task_issues, "ROOT", self.root),
            patch.object(sync_task_issues, "origin_repo", return_value=(OWNER, REPO)),
            patch.object(sync_task_issues, "gh", self.fake),
            patch.object(sync_roadmap_issues.time, "sleep"),
            contextlib.redirect_stdout(output),
        ):
            result = sync_roadmap_issues.main(command)
        return result, output.getvalue()

    def find(self, marker):
        matches = [issue for issue in self.fake.issues.values() if marker in (issue["body"] or "")]
        self.assertEqual(1, len(matches), matches)
        return matches[0]

    def phase1(self, roadmap_id="900", slug=None):
        marker = roadmap_issues.phase_marker(
            f"specs/{roadmap_id}-{slug or 'planned-feature'}", "phase-1"
        )
        return self.find(marker)

    def materialized_phases(self):
        self.run_sync("--id", "900", "--apply")
        feature_path = "specs/900-planned-feature"
        feature_dir = self.root / feature_path
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "tasks.md").write_text(
            "# Tasks: Planned Feature\n\n"
            "## Phase 1: Setup\n\n- [ ] T001 Setup\n\n"
            "## Phase 2: Delivery\n\n- [ ] T002 Deliver\n",
            encoding="utf-8",
        )
        phase2 = self.fake.add_issue(
            4,
            roadmap_issues.phase_marker(feature_path, "phase-2")
            + "\n"
            + roadmap_issues.roadmap_phase_marker("900", "phase-2"),
            title="Phase 2: Delivery",
            label="Story",
        )
        return self.phase1(), phase2

    def test_planned_creates_epic_spec_plan_and_provisional_phase_1(self):
        self.run_sync("--apply")

        epic = self.find(roadmap_issues.roadmap_marker("900"))
        spec_plan = self.find(roadmap_issues.planning_marker("900", "spec-plan"))
        phase1 = self.phase1()
        self.assertEqual("Epic", epic["labels"][0])
        self.assertCountEqual(["Planning"], spec_plan["labels"])
        self.assertEqual(["Story"], phase1["labels"])
        self.assertEqual("Phase 1: Kickoff — Planned Feature", phase1["title"])
        self.assertIn(
            roadmap_issues.roadmap_phase_marker("900", "phase-1"), phase1["body"]
        )
        self.assertEqual(epic["number"], self.fake.parents[spec_plan["number"]])
        self.assertEqual(epic["number"], self.fake.parents[phase1["number"]])
        self.assertEqual({spec_plan["number"]}, self.fake.blockers[phase1["number"]])
        self.assertNotIn(spec_plan["number"], self.fake.blockers.get(epic["number"], set()))
        self.assertIn(f"#{spec_plan['number']}", epic["body"])
        self.assertFalse(any(roadmap_issues.planning_marker("900", "tasks") in i["body"]
                             for i in self.fake.issues.values()))
        self.assertEqual(3 + 0, len([i for i in self.fake.issues.values()
                                     if "900" in i["body"] or "specs/900" in i["body"]]))
        self.assertEqual({self.find(roadmap_issues.roadmap_marker("901"))["number"]},
                         self.fake.blockers[epic["number"]])

    def test_renamed_roadmap_entry_keeps_the_provisional_phase_1_issue(self):
        self.run_sync("--id", "900", "--apply")
        original_phase1 = self.phase1()
        self.write_roadmap(roadmap_entry("900", "Renamed Feature"))

        self.run_sync("--id", "900", "--apply")

        self.assertEqual(3, len(self.fake.issues))
        self.assertEqual(original_phase1["number"], self.phase1("900", "renamed-feature")["number"])
        self.assertEqual("Phase 1: Kickoff — Renamed Feature", original_phase1["title"])
        self.assertIn(
            roadmap_issues.roadmap_phase_marker("900", "phase-1"), original_phase1["body"]
        )

    def test_needs_info_creates_only_labeled_epic(self):
        self.run_sync("--id", "902", "--apply")

        epic = self.find(roadmap_issues.roadmap_marker("902"))
        self.assertCountEqual(["Epic", "needs-info"], epic["labels"])
        self.assertNotIn(roadmap_issues.planning_marker("902", "spec-plan"), epic["body"])
        self.assertFalse(any(roadmap_issues.planning_marker("902", key) in issue["body"]
                             for issue in self.fake.issues.values()
                             for key, _ in roadmap_issues.PLANNING))

    def test_needs_info_to_planned_adds_planning_and_removes_label(self):
        self.run_sync("--id", "902", "--apply")
        epic = self.find(roadmap_issues.roadmap_marker("902"))
        self.write_roadmap(roadmap_entry("902", "Needs Information"))

        self.run_sync("--id", "902", "--apply")

        self.assertNotIn("needs-info", epic["labels"])
        self.find(roadmap_issues.planning_marker("902", "spec-plan"))
        self.phase1("902", "needs-information")

    def test_needs_info_rerun_is_idempotent(self):
        self.run_sync("--id", "902", "--apply")
        self.fake.calls.clear()

        self.run_sync("--id", "902", "--apply")

        self.assertEqual([], self.fake.mutations())

    def test_undecided_does_not_create_or_change_issues(self):
        self.fake.add_issue(1, f"{roadmap_issues.roadmap_marker('903')}\nexisting")

        self.run_sync("--id", "903", "--apply")

        self.assertEqual([], self.fake.mutations())
        self.assertEqual("open", self.fake.issues[1]["state"])

    def test_deferred_reopens_not_planned_issues_when_entry_returns_to_planned(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature", "deferred"))
        self.fake.add_issue(
            1, f"{roadmap_issues.roadmap_marker('900')}\nbody", title="Feature: 900 Planned Feature",
            state="closed", state_reason="not_planned",
        )

        self.run_sync("--id", "900", "--apply")
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        self.run_sync("--id", "900", "--apply")

        self.assertEqual("open", self.fake.issues[1]["state"])
        self.find(roadmap_issues.planning_marker("900", "spec-plan"))

    def test_abandoned_entry_is_not_reopened(self):
        self.fake.add_issue(
            1, f"{roadmap_issues.roadmap_marker('900')}\nbody", title="Feature: 900 Planned Feature",
            state="closed", state_reason="not_planned", labels=["abandoned"],
        )
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))

        _, output = self.run_sync("--id", "900", "--apply")

        self.assertEqual("closed", self.fake.issues[1]["state"])
        self.assertIn("will not be reopened automatically", output)
        self.assertFalse(any(roadmap_issues.planning_marker("900", key) in issue["body"]
                             for issue in self.fake.issues.values()
                             for key, _ in roadmap_issues.PLANNING))

    def test_issue_with_only_a_feature_path_marker_is_not_an_epic(self):
        self.fake.add_issue(
            1, "<!-- speckit-feature: specs/900-planned-feature -->\nOther", label="Epic"
        )

        result, output = self.run_sync("--id", "900")

        self.assertEqual(0, result)
        self.assertIn("[dry-run] create Feature: 900 Planned Feature", output)

    def test_deferred_and_abandoned_close_with_comment_and_label(self):
        self.write_roadmap(
            roadmap_entry("904", "Deferred Feature", "deferred")
            + roadmap_entry("905", "Abandoned Feature", "abandoned")
        )
        self.fake.add_issue(1, f"{roadmap_issues.roadmap_marker('904')}\nbody")
        self.fake.add_issue(2, f"{roadmap_issues.roadmap_marker('905')}\nbody")
        self.fake.add_issue(
            3, f"{roadmap_issues.planning_marker('905', 'spec-plan')}\nbody"
        )
        self.fake.add_issue(
            4, f"{roadmap_issues.phase_marker('specs/905-abandoned-feature', 'phase-1')}\nbody"
        )

        self.run_sync("--apply")

        self.assertEqual(("closed", "not_planned"),
                         (self.fake.issues[1]["state"], self.fake.issues[1]["state_reason"]))
        self.assertIn("abandoned", self.fake.issues[2]["labels"])
        self.assertIn("abandoned", self.fake.issues[3]["labels"])
        self.assertIn("abandoned", self.fake.issues[4]["labels"])
        self.assertEqual("not_planned", self.fake.issues[4]["state_reason"])
        comments = [call for call in self.fake.calls if call[:3] == ("api", "-X", "POST")
                    and "/comments" in call[3]]
        self.assertEqual(4, len(comments))

    def test_lifecycle_comment_is_not_duplicated_when_close_fails(self):
        self.write_roadmap(roadmap_entry("904", "Deferred Feature", "deferred"))
        self.fake.add_issue(1, f"{roadmap_issues.roadmap_marker('904')}\nbody")
        failed_once = False

        def fail_close_once(args):
            nonlocal failed_once
            if (
                not failed_once
                and args[:3] == ("api", "-X", "PATCH")
                and args[3] == "repos/o/r/issues/1"
                and "state=closed" in args
            ):
                failed_once = True
                return True
            return False

        self.fake.fail_on = fail_close_once
        with self.assertRaises(SystemExit):
            self.run_sync("--apply")
        self.assertEqual(1, len(self.fake.comments[1]))
        self.fake.fail_on = None

        self.run_sync("--apply")

        self.assertEqual(1, len(self.fake.comments[1]))
        self.assertEqual("closed", self.fake.issues[1]["state"])

    def test_removed_roadmap_entries_are_abandoned(self):
        self.write_roadmap("")
        self.fake.add_issue(1, f"{roadmap_issues.roadmap_marker('900')}\nbody")

        _, output = self.run_sync("--apply")

        self.assertIn("entry 900 was removed", output)
        self.assertIn("abandoned", self.fake.issues[1]["labels"])
        self.assertEqual("not_planned", self.fake.issues[1]["state_reason"])

    def test_removed_entry_retires_all_materialized_phases(self):
        phases = self.materialized_phases()
        self.write_roadmap("")

        self.run_sync("--apply")

        for phase in phases:
            self.assertEqual(("closed", "not_planned"), (
                phase["state"], phase["state_reason"]
            ))
            self.assertIn("abandoned", phase["labels"])
            self.assertIn(sync_task_issues.PHASE_RETIRED_MARKER, phase["body"])

    def test_needs_info_pauses_and_reopens_existing_phases(self):
        phases = self.materialized_phases()
        self.write_roadmap(roadmap_entry("900", "Planned Feature", "needs-info"))

        self.run_sync("--id", "900", "--apply")

        for phase in phases:
            self.assertEqual(("closed", "not_planned"), (
                phase["state"], phase["state_reason"]
            ))
            self.assertIn(sync_task_issues.PHASE_PAUSED_MARKER, phase["body"])

        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        self.run_sync("--id", "900", "--apply")

        for phase in phases:
            self.assertEqual("open", phase["state"])

    def test_unowned_blocked_by_link_is_preserved(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        self.fake.add_issue(1, f"{roadmap_issues.roadmap_marker('900')}\nbody")
        self.fake.add_issue(999, "manual blocker")
        self.fake.blockers[1] = {999}

        self.run_sync("--id", "900", "--apply")

        self.assertIn(999, self.fake.blockers[1])

    def test_removed_roadmap_dependency_is_deleted_but_unowned_link_remains(self):
        self.write_roadmap(
            roadmap_entry("900", "Planned Feature")
            + roadmap_entry("901", "Dependency Feature")
        )
        self.fake.add_issue(1, f"{roadmap_issues.roadmap_marker('900')}\nbody")
        self.fake.add_issue(2, f"{roadmap_issues.roadmap_marker('901')}\nbody")
        self.fake.add_issue(999, "manual blocker")
        self.fake.blockers[1] = {2, 999}

        self.run_sync("--apply")

        self.assertEqual({999}, self.fake.blockers[1])

    def test_rerun_is_idempotent(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        self.run_sync("--id", "900", "--apply")
        self.fake.calls.clear()

        self.run_sync("--id", "900", "--apply")

        self.assertEqual([], self.fake.mutations())

    def test_apply_stops_if_created_issues_are_not_visible(self):
        with patch.object(sync_task_issues, "load_issues", return_value=[]):
            with self.assertRaisesRegex(SystemExit, "did not become visible in time"):
                self.run_sync("--id", "900", "--apply")

        self.assertEqual(3, len(self.fake.created()))  # epic, spec-plan and Phase 1

    def test_dry_run_does_not_make_mutating_github_calls(self):
        result, output = self.run_sync("--id", "900")

        self.assertEqual(0, result)
        self.assertIn("[dry-run] create Feature: 900 Planned Feature", output)
        self.assertIn("spec-plan planning issue for 900 a sub-issue", output)
        self.assertIn("create Phase 1: Kickoff — Planned Feature", output)
        self.assertIn("provisional Phase 1 for 900 a sub-issue of its epic", output)
        self.assertIn("provisional Phase 1 for 900 blocked by its spec-plan issue", output)
        self.assertNotIn("tasks planning", output)
        self.assertIn("no tracked epic for dependency 901", output)
        self.assertNotIn("make new epic for 900 blocked by new epic for 901", output)
        self.assertEqual([], self.fake.mutations())

    def test_full_dry_run_previews_links_to_epics_created_in_same_run(self):
        self.write_roadmap(
            roadmap_entry("900", "Planned Feature", depends="901")
            + roadmap_entry("901", "Dependency Feature")
        )
        self.fake.add_issue(1, f"{roadmap_issues.roadmap_marker('900')}\nexisting")

        _, output = self.run_sync()

        self.assertIn("make epic for 900 blocked by new epic for 901", output)

    def test_dry_run_does_not_preview_epic_for_undecided_dependency(self):
        roadmap = (
            roadmap_entry("900", "Planned Feature", depends="903")
            + roadmap_entry("903", "Undecided Feature", "undecided")
        )
        self.write_roadmap(roadmap)

        for existing_epic in (False, True):
            with self.subTest(existing_epic=existing_epic):
                self.fake = FakeGitHub()
                if existing_epic:
                    self.fake.add_issue(
                        1, f"{roadmap_issues.roadmap_marker('900')}\nexisting"
                    )

                _, output = self.run_sync()

                self.assertNotIn("blocked by new epic for 903", output)
                self.assertIn("no tracked epic for dependency 903", output)

    def test_dry_run_previews_project_items_for_issues_not_yet_created(self):
        _, output = self.run_sync("--id", "900", skip_project=False)

        self.assertIn("add the roadmap Epic for 900 to project", output)
        self.assertIn("set Roadmap order for 900 roadmap Epic to 1", output)
        self.assertIn("add planning issues for 900 to project", output)
        self.assertIn("add provisional Phase 1 for 900 to project example-org/123 as Planned", output)

    def test_closed_completed_issues_are_never_mutated(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature", "abandoned"))
        self.fake.add_issue(
            1,
            f"{roadmap_issues.roadmap_marker('900')}\nbody",
            state="closed",
            state_reason="completed",
        )

        self.run_sync("--id", "900", "--apply")

        self.assertEqual([], self.fake.mutations())

    def test_project_items_receive_file_order_without_changing_status(self):
        self.write_roadmap(
            roadmap_entry("900", "Planned Feature")
            + roadmap_entry("901", "Dependency Feature")
        )

        self.run_sync("--apply", skip_project=False)

        first_epic = self.find(roadmap_issues.roadmap_marker("900"))
        second_epic = self.find(roadmap_issues.roadmap_marker("901"))
        first_planning = [
            self.find(roadmap_issues.planning_marker("900", key))
            for key, _ in roadmap_issues.PLANNING
        ]
        phase1 = self.phase1()
        self.assertEqual("Planned", self.fake.project_items[phase1["number"]]["status"])
        self.assertIsNone(self.fake.project_items[phase1["number"]]["order"])
        self.assertEqual(1, self.fake.project_items[first_epic["number"]]["order"])
        self.assertEqual(2, self.fake.project_items[second_epic["number"]]["order"])
        self.assertIsNone(self.fake.project_items[first_epic["number"]]["status"])
        self.assertTrue(
            all(self.fake.project_items[issue["number"]]["order"] is None
                for issue in first_planning)
        )

    def test_project_items_are_still_added_when_order_field_is_missing(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        self.fake.has_roadmap_order = False

        _, output = self.run_sync("--apply", skip_project=False)

        self.assertIn("has no 'Roadmap order' number field", output)
        self.assertTrue(self.fake.project_items)
        self.assertTrue(all(item["order"] is None for item in self.fake.project_items.values()))

    def merged_spec_plan_fixture(self, tasks_exist=False):
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        self.run_sync("--id", "900", "--apply")
        spec_plan = self.find(roadmap_issues.planning_marker("900", "spec-plan"))
        phase1 = self.phase1()
        spec_plan.update(state="closed", state_reason="completed")
        if tasks_exist:
            feature = self.root / "specs/900-planned-feature"
            feature.mkdir(parents=True)
            (feature / "tasks.md").write_text("# Tasks: X\n", encoding="utf-8")
        return spec_plan, phase1

    def test_merging_spec_plan_clears_only_the_phase_1_blocker(self):
        spec_plan, phase1 = self.merged_spec_plan_fixture()
        self.fake.add_issue(999, "manual blocker")
        self.fake.blockers[phase1["number"]].add(999)
        epic = self.find(roadmap_issues.roadmap_marker("900"))
        self.fake.blockers[epic["number"]] = {999}

        self.run_sync("--id", "900", "--apply")

        self.assertEqual({999}, self.fake.blockers[phase1["number"]])
        self.assertEqual({999}, self.fake.blockers[epic["number"]])
        self.fake.calls.clear()

        self.run_sync("--id", "900", "--apply")

        self.assertEqual([], self.fake.mutations())
        self.assertNotIn(spec_plan["number"], self.fake.blockers[phase1["number"]])

    def test_blocker_is_not_readded_after_spec_plan_merge(self):
        _, phase1 = self.merged_spec_plan_fixture()
        self.run_sync("--id", "900", "--apply")
        self.assertEqual(set(), self.fake.blockers.get(phase1["number"], set()))

        self.run_sync("--id", "900", "--apply")

        self.assertEqual(set(), self.fake.blockers.get(phase1["number"], set()))

    def test_phase_1_is_left_to_the_task_sync_once_tasks_md_exists(self):
        _, phase1 = self.merged_spec_plan_fixture(tasks_exist=True)
        phase1.update(title="Phase 1: Generated", body=phase1["body"] + "\nfrom tasks.md")
        before = dict(phase1)
        self.fake.calls.clear()

        self.run_sync("--id", "900", "--apply", skip_project=False)

        self.assertEqual(before, self.fake.issues[phase1["number"]])
        self.assertNotIn(phase1["number"], self.fake.project_items)
        self.assertFalse(any(call[:3] == ("api", "-X", "PATCH")
                             and call[3].endswith(f"issues/{phase1['number']}")
                             for call in self.fake.mutations()))
        self.assertEqual(set(), self.fake.blockers[phase1["number"]])

    def test_deferred_entry_closes_and_later_reopens_provisional_phase_1(self):
        self.run_sync("--id", "900", "--apply")
        phase1 = self.phase1()
        self.write_roadmap(roadmap_entry("900", "Planned Feature", "deferred"))

        self.run_sync("--id", "900", "--apply")

        self.assertEqual(("closed", "not_planned"), (phase1["state"], phase1["state_reason"]))
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))

        self.run_sync("--id", "900", "--apply")

        self.assertEqual("open", phase1["state"])
        self.assertNotIn(sync_task_issues.PHASE_PAUSED_MARKER, phase1["body"])

    def test_deferred_entry_reopens_all_materialized_phases(self):
        phase1, phase2 = self.materialized_phases()
        self.write_roadmap(roadmap_entry("900", "Planned Feature", "deferred"))

        self.run_sync("--id", "900", "--apply")

        for phase in (phase1, phase2):
            self.assertEqual(("closed", "not_planned"), (
                phase["state"], phase["state_reason"]
            ))
            self.assertIn(sync_task_issues.PHASE_PAUSED_MARKER, phase["body"])

        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        self.run_sync("--id", "900", "--apply")

        for phase in (phase1, phase2):
            self.assertEqual("open", phase["state"])
            self.assertNotIn(sync_task_issues.PHASE_PAUSED_MARKER, phase["body"])

    def test_deferred_entry_does_not_close_active_or_completed_phases(self):
        phase1, _ = self.materialized_phases()
        cases = {
            "In progress": lambda: self.fake.branches.append(
                f"{phase1['number']}-planned-feature-phase-1-setup"
            ),
            "In review": lambda: self.fake.add_pull_request(
                70, "Phase PR", body=f"Closes #{phase1['number']}"
            ),
            "Done": lambda: phase1.update(state="closed", state_reason="completed"),
        }
        for status, arrange in cases.items():
            with self.subTest(status=status):
                phase1["state"] = "open"
                phase1["state_reason"] = None
                self.fake.branches.clear()
                self.fake.pull_requests.clear()
                arrange()
                self.write_roadmap(roadmap_entry("900", "Planned Feature", "deferred"))
                self.fake.calls.clear()

                result, output = self.run_sync("--id", "900", "--apply")
                self.assertEqual(1, result)
                self.assertIn(f"phase-1 (#{phase1['number']}, {status})", output)
                self.assertIn("cannot be closed while Phase work is active", output)
                self.assertNotIn("was removed from ROADMAP.md", output)
                self.assertEqual([], self.fake.mutations())

    def test_blocked_removed_entry_does_not_stop_other_roadmap_entries(self):
        phase1, _ = self.materialized_phases()
        phase1.update(state="closed", state_reason="completed")
        self.write_roadmap(roadmap_entry("901", "Dependency Feature"))

        result, output = self.run_sync("--apply")

        self.assertEqual(1, result)
        self.assertIn("Roadmap sync left blocked entries unchanged: 900", output)
        self.assertEqual("open", self.fake.issues[1]["state"])
        self.find(roadmap_issues.roadmap_marker("901"))

        calls_before = len(self.fake.mutations())
        result, _ = self.run_sync("--apply")
        self.assertEqual(1, result)
        self.assertEqual(calls_before, len(self.fake.mutations()))

    def test_provisional_phase_1_content_is_deterministic_and_repaired(self):
        self.run_sync("--id", "900", "--apply")
        phase1 = self.phase1()
        phase1["title"] = "edited"

        self.run_sync("--id", "900", "--apply")

        self.assertEqual("Phase 1: Kickoff — Planned Feature", phase1["title"])
        self.assertIn("speckit-tasks", phase1["body"])

    def test_project_phase_1_planned_status_is_not_overwritten(self):
        self.run_sync("--id", "900", "--apply", skip_project=False)
        phase1 = self.phase1()
        self.fake.project_items[phase1["number"]]["status"] = "In progress"
        self.fake.calls.clear()

        self.run_sync("--id", "900", "--apply", skip_project=False)

        self.assertEqual("In progress", self.fake.project_items[phase1["number"]]["status"])

    def test_removed_entry_closes_provisional_phase_1(self):
        self.run_sync("--id", "900", "--apply")
        phase1 = self.phase1()
        self.write_roadmap("")

        self.run_sync("--apply")

        self.assertEqual("not_planned", phase1["state_reason"])
        self.assertIn("abandoned", phase1["labels"])

    def test_removed_entry_without_epic_still_retires_orphaned_phases(self):
        self.run_sync("--id", "900", "--apply")
        provisional = self.phase1()
        materialized = self.fake.add_issue(
            50,
            sync_task_issues.phase_marker("specs/900-planned-feature", "phase-2")
            + "\n"
            + roadmap_issues.roadmap_phase_marker("900", "phase-2"),
            title="Phase 2: Delivery",
            label="Story",
        )
        for issue in list(self.fake.issues.values()):
            if roadmap_issues.roadmap_marker("900") in (issue["body"] or ""):
                del self.fake.issues[issue["number"]]
        self.write_roadmap("")

        self.run_sync("--apply")

        for phase in (provisional, materialized):
            self.assertEqual(("closed", "not_planned"), (phase["state"], phase["state_reason"]))
            self.assertIn("abandoned", phase["labels"])

    def test_removed_entry_without_epic_keeps_in_progress_phase_guard(self):
        self.run_sync("--id", "900", "--apply")
        provisional = self.phase1()
        for issue in list(self.fake.issues.values()):
            if roadmap_issues.roadmap_marker("900") in (issue["body"] or ""):
                del self.fake.issues[issue["number"]]
        self.fake.branches.append(f"{provisional['number']}-planned-feature-phase-1-setup")
        self.write_roadmap("")

        _, output = self.run_sync("--apply")

        self.assertEqual("open", provisional["state"])
        self.assertIn("[skip]", output)

    def orphan_phase_without_epic(self):
        self.run_sync("--id", "900", "--apply")
        provisional = self.phase1()
        for issue in list(self.fake.issues.values()):
            if roadmap_issues.roadmap_marker("900") in (issue["body"] or ""):
                del self.fake.issues[issue["number"]]
        self.write_roadmap("")
        return provisional

    def test_removed_entry_without_epic_keeps_done_phase_guard(self):
        provisional = self.orphan_phase_without_epic()
        provisional.update(state="closed", state_reason="completed")
        self.fake.calls.clear()

        result, output = self.run_sync("--apply")

        self.assertEqual(1, result)
        self.assertEqual(("closed", "completed"), (provisional["state"], provisional["state_reason"]))
        self.assertNotIn("abandoned", provisional["labels"])
        self.assertEqual([], self.fake.mutations())
        self.assert_removed_entry_resolution_message(output, provisional, "Done")

    def test_removed_entry_without_epic_keeps_in_review_phase_guard(self):
        provisional = self.orphan_phase_without_epic()
        self.fake.add_pull_request(70, "Phase PR", body=f"Closes #{provisional['number']}")
        self.fake.calls.clear()

        result, output = self.run_sync("--apply")

        self.assertEqual(1, result)
        self.assertEqual("open", provisional["state"])
        self.assertNotIn("abandoned", provisional["labels"])
        self.assertEqual([], self.fake.mutations())
        self.assert_removed_entry_resolution_message(output, provisional, "In review")

    def test_hand_retiring_a_blocking_phase_unblocks_the_removed_entry(self):
        provisional = self.orphan_phase_without_epic()
        provisional.update(state="closed", state_reason="completed")
        result, _ = self.run_sync("--apply")
        self.assertEqual(1, result)

        provisional["state_reason"] = "not_planned"
        result, output = self.run_sync("--apply")

        self.assertEqual(0, result)
        self.assertNotIn("[skip]", output)
        self.assertIn(sync_task_issues.PHASE_RETIRED_MARKER, provisional["body"])
        self.assertIn("abandoned", provisional["labels"])

    def assert_removed_entry_resolution_message(self, output, phase, status):
        self.assertIn(f"(#{phase['number']}, {status})", output)
        self.assertIn("was removed from ROADMAP.md", output)
        self.assertIn("restore the entry in ROADMAP.md", output)
        self.assertIn("close the Phase as not planned and label it abandoned", output)
        self.assertNotIn("reopen a completed Phase", output)
        reminder = "close or retarget its open pull request and delete its branch"
        if status == "Done":
            self.assertNotIn(reminder, output)
        else:
            self.assertIn(reminder, output)

    def test_removed_entry_does_not_touch_a_remaining_entry(self):
        self.write_roadmap(
            roadmap_entry("900", "Planned Feature") + roadmap_entry("901", "Dependency Feature")
        )
        self.run_sync("--apply")
        kept_phase1 = self.phase1("901", "dependency-feature")
        kept_phase2 = self.fake.add_issue(
            60,
            roadmap_issues.phase_marker("specs/901-dependency-feature", "phase-2")
            + "\n"
            + roadmap_issues.roadmap_phase_marker("901", "phase-2"),
            title="Phase 2: Delivery",
            label="Story",
        )
        kept_epic = self.find(roadmap_issues.roadmap_marker("901"))
        removed_phase1 = self.phase1()
        removed_phase2 = self.fake.add_issue(
            61,
            roadmap_issues.phase_marker("specs/900-planned-feature", "phase-2")
            + "\n"
            + roadmap_issues.roadmap_phase_marker("900", "phase-2"),
            title="Phase 2: Delivery",
            label="Story",
        )
        self.write_roadmap(roadmap_entry("901", "Dependency Feature"))
        self.fake.calls.clear()

        self.run_sync("--apply")

        for phase in (removed_phase1, removed_phase2):
            self.assertEqual(("closed", "not_planned"), (phase["state"], phase["state_reason"]))
            self.assertIn("abandoned", phase["labels"])
        for issue in (kept_epic, kept_phase1, kept_phase2):
            self.assertEqual("open", issue["state"])
            self.assertNotIn("abandoned", issue["labels"])
        kept_planning = self.find(roadmap_issues.planning_marker("901", "spec-plan"))
        kept_numbers = {
            str(issue["number"]) for issue in (kept_epic, kept_planning, kept_phase1, kept_phase2)
        }

        def targets_kept_issue(arg):
            route = re.search(r"/issues/(\d+)(?:/|$)", arg)
            return arg in kept_numbers or (route is not None and route.group(1) in kept_numbers)

        touched = [
            call
            for call in self.fake.mutations()
            if any(targets_kept_issue(arg) for arg in map(str, call))
        ]
        self.assertEqual([], touched)

    def test_unlabelled_copy_of_an_epic_marker_stops_sync_before_creating_issues(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        marker = roadmap_issues.roadmap_marker("900")
        self.fake.add_issue(1, marker, label="Epic")
        self.fake.add_issue(2, f"copied\n{marker}")
        self.fake.calls.clear()

        with self.assertRaisesRegex(SystemExit, "Multiple issues match roadmap epic 900"):
            self.run_sync("--id", "900", "--apply")

        self.assertEqual([], self.fake.mutations())

    def test_marker_copy_by_a_public_outsider_is_ignored_and_does_not_stop_the_sync(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        marker = roadmap_issues.roadmap_marker("900")
        self.fake.add_issue(
            1, f"copied\n{marker}", author="stranger", author_association="NONE"
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.run_sync("--id", "900", "--apply")

        self.assertIn("#1", out.getvalue())
        self.assertIn("stranger", out.getvalue())
        epics = [i for i in self.fake.issues.values() if marker in i["body"] and i["number"] != 1]
        self.assertEqual(1, len(epics))

    def test_marker_by_a_bot_or_a_collaborator_still_counts(self):
        marker = roadmap_issues.roadmap_marker("900")
        for author, association in (("github-actions[bot]", "NONE"), ("dev", "COLLABORATOR")):
            issue = {"number": 1, "body": marker, "author": author,
                     "author_association": association}
            epic, _ = roadmap_issues.index_roadmap_issues([issue], "900")
            self.assertEqual(1, epic["number"], author)

    def test_a_maintainer_only_label_makes_an_issue_trusted(self):
        marker = roadmap_issues.roadmap_marker("900")
        for label in ("Epic", "Story", "Planning"):
            issue = {"number": 1, "body": marker, "author": "departed-user",
                     "author_association": "NONE", "labels": [label]}
            epic, _ = roadmap_issues.index_roadmap_issues([issue], "900")
            self.assertEqual(1, epic["number"], label)
        unlabelled = {"number": 1, "body": marker, "author": "departed-user",
                      "author_association": "NONE", "labels": ["bug"]}
        epic, _ = roadmap_issues.index_roadmap_issues([unlabelled], "900")
        self.assertIsNone(epic)

    def test_a_labelled_epic_by_a_departed_author_is_not_duplicated(self):
        self.write_roadmap(roadmap_entry("900", "Planned Feature"))
        marker = roadmap_issues.roadmap_marker("900")
        self.fake.add_issue(
            1, marker, label="Epic", author="ghost", author_association="CONTRIBUTOR"
        )

        self.run_sync("--id", "900", "--apply")

        epics = [i for i in self.fake.issues.values() if marker in i["body"]]
        self.assertEqual([1], [i["number"] for i in epics])

    def test_an_outsider_marker_issue_of_a_removed_entry_is_never_touched(self):
        self.write_roadmap("")
        marker = roadmap_issues.roadmap_marker("900")
        self.fake.add_issue(
            1, f"copied\n{marker}", author="stranger", author_association="NONE"
        )
        self.fake.add_issue(
            2,
            f"copied\n{roadmap_issues.planning_marker('900', 'spec-plan')}",
            author="stranger",
            author_association="NONE",
        )
        self.fake.add_issue(
            3,
            f"copied\n{roadmap_issues.roadmap_phase_marker('900', 'phase-1')}",
            author="stranger",
            author_association="NONE",
        )
        self.fake.calls.clear()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.run_sync("--apply")

        self.assertEqual([], self.fake.mutations())
        for number in (1, 2, 3):
            self.assertEqual("open", self.fake.issues[number]["state"])
            self.assertNotIn("abandoned", self.fake.issues[number]["labels"])

    def test_an_outsider_marker_issue_is_not_a_roadmap_entry_for_dependencies(self):
        marker = roadmap_issues.roadmap_marker("950")
        self.fake.add_issue(
            1, f"copied\n{marker}", author="stranger", author_association="NONE"
        )
        self.fake.add_issue(
            2, "x", author="stranger", author_association="NONE"
        )
        self.fake.calls.clear()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.run_sync("--apply")

        touched = [
            call for call in self.fake.mutations() if any("/issues/1" in str(a) or a == 1 for a in call)
        ]
        self.assertEqual([], touched)

    def test_an_outsider_issue_is_warned_about_once_per_run(self):
        outsider = {"number": 7, "body": roadmap_issues.roadmap_marker("900"), "author": "stranger",
                    "author_association": "NONE", "labels": []}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                roadmap_issues.trusted_issues([outsider])

        self.assertEqual(1, err.getvalue().count("#7"))

    def test_only_trusted_issues_add_roadmap_ids(self):
        marker = roadmap_issues.roadmap_marker("950")
        outsider = {"number": 1, "body": marker, "author": "stranger",
                    "author_association": "NONE", "labels": []}
        labelled = {"number": 2, "body": roadmap_issues.roadmap_marker("951"), "author": "ghost",
                    "author_association": "NONE", "labels": ["Epic"]}
        member = {"number": 3, "body": roadmap_issues.roadmap_marker("952"), "author": "dev",
                  "author_association": "MEMBER", "labels": []}

        with contextlib.redirect_stderr(io.StringIO()):
            ids = sync_roadmap_issues.marker_roadmap_ids([outsider, labelled, member])

        self.assertEqual({"951", "952"}, ids)

    def test_find_phase1_ignores_an_outsider_stable_marker(self):
        marker = roadmap_issues.roadmap_phase_marker("900", "phase-1")
        outsider = {"number": 7, "body": marker, "author": "stranger",
                    "author_association": "NONE", "labels": []}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(
                sync_roadmap_issues.find_phase1([outsider], "specs/900-planned-feature")
            )

    def test_find_phase1_keeps_a_labelled_issue_of_a_departed_author(self):
        marker = roadmap_issues.roadmap_phase_marker("900", "phase-1")
        issue = {"number": 7, "body": marker, "author": "ghost",
                 "author_association": "NONE", "labels": ["Story"]}

        found = sync_roadmap_issues.find_phase1([issue], "specs/900-planned-feature")

        self.assertEqual(7, found["number"])

    def test_rate_limit_response_is_retried_with_backoff(self):
        from subprocess import CompletedProcess

        responses = [
            CompletedProcess(["gh"], 1, "", "HTTP 429: secondary rate limit"),
            CompletedProcess(["gh"], 0, "ok", ""),
        ]
        with patch.object(sync_task_issues.subprocess, "run", side_effect=responses) as run, \
             patch.object(sync_task_issues.time, "sleep") as sleep:
            result = sync_task_issues.gh("api", "repos/o/r/issues")

        self.assertEqual("ok", result.stdout)
        self.assertEqual(2, run.call_count)
        sleep.assert_called_once_with(60)

    def test_permission_403_is_not_retried(self):
        from subprocess import CompletedProcess

        response = CompletedProcess(
            ["gh"], 1, "", "HTTP 403: Resource not accessible by integration"
        )
        with patch.object(sync_task_issues.subprocess, "run", return_value=response) as run:
            result = sync_task_issues.gh("api", "repos/o/r/issues", check=False)

        self.assertEqual(1, run.call_count)
        self.assertEqual(1, result.returncode)


class UninitializedRepositoryTest(unittest.TestCase):
    def run_main(self, *directories):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir(exist_ok=True)
            (root / "scripts" / "project_context.py").write_text("", encoding="utf-8")
            for name in directories:
                (root / name).mkdir(parents=True)
            output = io.StringIO()
            with (
                patch.object(sync_task_issues, "ROOT", root),
                patch.object(sync_task_issues, "origin_repo", side_effect=AssertionError("no GitHub access")),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                try:
                    code = sync_roadmap_issues.main(["--skip-project"])
                except SystemExit as error:
                    code = error.code
        return code, output.getvalue(), errors.getvalue()

    def test_an_uninitialized_repository_passes_with_one_clear_line(self):
        code, output, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(output, "Not initialized yet: no ROADMAP.md. See docs/bootstrap.md.\n")

    def test_an_initialized_repository_without_a_roadmap_fails_clearly(self):
        code, _, _ = self.run_main("specs/001-x")
        self.assertIn("ROADMAP.md is missing", str(code))
        self.assertIn("docs/bootstrap.md", str(code))

    def test_a_mistyped_root_is_an_error_not_uninitialized(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch.object(sync_task_issues, "ROOT", Path(directory) / "typo"),
                contextlib.redirect_stdout(output),
                self.assertRaises(SystemExit) as raised,
            ):
                sync_roadmap_issues.main(["--skip-project"])
        self.assertIn("not a repository root", str(raised.exception.code))
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
