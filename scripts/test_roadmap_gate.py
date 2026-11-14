import re
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import check_roadmap_dependencies as dependencies
import roadmap_gate


ROOT = Path(__file__).resolve().parent.parent
ENTRIES = {
    "012": {"id": "012", "title": "Architecture", "status": "planned"},
    "005": {"id": "005", "title": "Workspace", "status": "planned"},
}


def issue(number, body="", parent=None):
    return {
        "number": number,
        "body": body,
        "parent": parent,
        "state": "open",
        "state_reason": None,
        "labels": ["Epic"],
    }


def labeled(name="prerequisites-acknowledged", actor_type="User",
            created_at="2026-11-03T13:33:46Z", event_id=1, app=None):
    actor = {"login": "someone", "type": actor_type} if actor_type else None
    return {"id": event_id, "event": "labeled", "created_at": created_at,
            "label": {"name": name}, "actor": actor, "performed_via_github_app": app}


def accept_acknowledgement():
    return True, None


BLOCKED = {"result": "blocked", "entry": {"id": "012"}, "diagnostics": [],
           "dependencies": [{"id": "005", "satisfied": False}]}
READY = {"result": "ready", "entry": {"id": "012"}, "diagnostics": [], "dependencies": []}


class RoadmapGateTest(unittest.TestCase):
    def test_unlabelled_ancestor_markers_cannot_set_identity(self):
        chain = [issue(101), {**issue(90, "<!-- speckit-roadmap: 012 -->"), "labels": []}]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(
            dependencies.InvalidData, "no roadmap"
        ):
            roadmap_gate.roadmap_id_from_issue_chain(chain, ENTRIES)

    def test_unlabelled_copy_does_not_make_ancestry_ambiguous(self):
        chain = [issue(101), {**issue(91, "<!-- speckit-roadmap: 005 -->"), "labels": []},
                 issue(90, "<!-- speckit-roadmap: 012 -->")]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(roadmap_gate.roadmap_id_from_issue_chain(chain, ENTRIES), "012")

    def test_run_event_reads_fresh_paginated_labels_not_event_labels(self):
        chain = [issue(101), issue(90, "<!-- speckit-roadmap: 012 -->")]
        status = {"result": "blocked", "entry": {"id": "012"}, "diagnostics": [],
                  "dependencies": [{"id": "005", "satisfied": False}]}
        for event_labels, api_labels, expected in (
            ([{"name": "prerequisites-acknowledged"}], [], "blocked"),
            ([], [{"name": "prerequisites-acknowledged"}], "pass"),
        ):
            event = {"number": 123, "pull_request": {
                "number": 123, "head": {"ref": "101-phase-1-build-feature"}, "labels": event_labels}}
            with (
                patch.object(roadmap_gate.check_roadmap, "validate_roadmap", return_value=ENTRIES),
                patch.object(roadmap_gate, "load_issue_chain", return_value=chain),
                patch.object(roadmap_gate, "live_roadmap_status", return_value=status),
                patch.object(roadmap_gate, "github_pr_events",
                             return_value=[labeled()]) as events,
                patch.object(roadmap_gate.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps([api_labels]), ""
                )) as run,
            ):
                self.assertEqual(roadmap_gate.run_event(event, "owner/repo")["result"], expected)
                if expected == "blocked":
                    events.assert_not_called()
                command = run.call_args.args[0]
                self.assertIn("repos/owner/repo/issues/123/labels?per_page=100", command)
                self.assertIn("--paginate", command)
                self.assertIn("--slurp", command)

    def test_phase_event_requires_valid_pr_number_and_context(self):
        for number in (None, True, 0, "123", 123.0):
            event = {"number": number, "pull_request": {
                "number": number, "head": {"ref": "101-phase-1-build-feature"}}}
            with self.subTest(number=number), self.assertRaisesRegex(
                dependencies.InvalidData, "pull request number"
            ):
                roadmap_gate.run_event(event, "owner/repo")
        with self.assertRaisesRegex(dependencies.InvalidData, "pull request number"):
            roadmap_gate.run_event({"number": 123, "pull_request": {
                "number": 124, "head": {"ref": "101-phase-1-build-feature"}}}, "owner/repo")

    def test_non_phase_event_never_reads_roadmap_or_github(self):
        with (
            patch.object(roadmap_gate.check_roadmap, "validate_roadmap") as roadmap,
            patch.object(roadmap_gate.subprocess, "run") as run,
        ):
            for branch in ("123-chore-tooling-change", "005-workspace-feature"):
                event = {"pull_request": {"head": {"ref": branch}}}
                self.assertEqual(roadmap_gate.run_event(event, None)["result"], "pass")
            roadmap.assert_not_called()
            run.assert_not_called()

    def test_github_parent_distinguishes_absence_errors_and_malformed_json(self):
        for code, output, error, expected in (
            (0, "90", "", 90), (1, "", "HTTP 404", None),
            (1, "", "HTTP 403", "cannot read parent"),
            (0, "not json", "", "invalid parent"),
            (0, "true", "", "invalid parent"), (0, "0", "", "invalid parent"),
        ):
            with self.subTest(output=output, error=error), patch.object(
                roadmap_gate.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, output, error)
            ):
                if isinstance(expected, str):
                    with self.assertRaisesRegex(dependencies.InvalidData, expected):
                        roadmap_gate.github_parent("owner/repo", 101)
                else:
                    self.assertEqual(roadmap_gate.github_parent("owner/repo", 101), expected)

    def test_github_issue_includes_label_metadata(self):
        with patch.object(roadmap_gate.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(issue(90)), ""
        )) as run:
            self.assertEqual(roadmap_gate.github_issue("owner/repo", 90), issue(90))
            self.assertIn("labels", run.call_args.args[0][-1])

    def test_fresh_labels_validate_every_page_and_fail_on_api_errors(self):
        for code, output in (
            (1, ""), (0, "bad json"), (0, "{}"), (0, "[]"),
            (0, '[[{"name": "Epic"}], [{"name": 1}]]'),
            (0, '[[{"name": "Epic"}], {}]'),
        ):
            with self.subTest(code=code, output=output), patch.object(
                roadmap_gate.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, output, "HTTP 403")
            ), self.assertRaises(dependencies.InvalidData):
                roadmap_gate.github_pr_labels("owner/repo", 123)
        with patch.object(roadmap_gate.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, '[[{"name": "Epic"}], [{"name": "prerequisites-acknowledged"}]]', ""
        )):
            self.assertEqual(roadmap_gate.github_pr_labels("owner/repo", 123),
                             {"Epic", "prerequisites-acknowledged"})

    def test_github_issue_fails_closed_on_api_errors_and_malformed_json(self):
        for code, output in ((1, ""), (0, "bad json")):
            with self.subTest(code=code), patch.object(
                roadmap_gate.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, output, "HTTP 403")
            ), self.assertRaises(dependencies.InvalidData):
                roadmap_gate.github_issue("owner/repo", 90)

    def test_live_status_rejects_malformed_json_and_exit_result_mismatch(self):
        for code, output in (
            (0, "bad json"), (0, "[]"),
            (1, json.dumps({"entry": {"id": "012"}, "result": "ready"})),
            (0, json.dumps({"entry": {"id": "005"}, "result": "ready"})),
            (3, ""),
        ):
            with self.subTest(code=code, output=output), patch.object(
                roadmap_gate.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, output, "failure")
            ), self.assertRaises(dependencies.InvalidData):
                roadmap_gate.live_roadmap_status("012")

    def test_main_outputs_decision_and_handles_event_errors(self):
        event = {"pull_request": {"head": {"ref": "123-chore-tooling-change"}}}
        for text, expected in ((json.dumps(event), 0), ("bad json", 1)):
            with (
                patch.dict(roadmap_gate.os.environ, {"GITHUB_EVENT_PATH": "event.json"}, clear=True),
                patch.object(Path, "read_text", return_value=text),
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(roadmap_gate.main(), expected)
                if expected == 0:
                    self.assertEqual(json.loads(output.getvalue())["result"], "pass")
                else:
                    self.assertIn("Roadmap gate failed:", errors.getvalue())

    def test_main_blocked_and_error_decisions_and_missing_event_path(self):
        for decision, message in (
            ({"result": "blocked", "roadmap_id": "012", "blockers": ["005"]}, "005"),
            ({"result": "error", "message": "API failure"}, "API failure"),
        ):
            with (
                patch.dict(roadmap_gate.os.environ, {"GITHUB_EVENT_PATH": "event.json"}, clear=True),
                patch.object(Path, "read_text", return_value="{}"),
                patch.object(roadmap_gate, "run_event", return_value=decision),
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(roadmap_gate.main(), 1)
                self.assertEqual(json.loads(output.getvalue()), decision)
                self.assertIn(message, errors.getvalue())
        with patch.dict(roadmap_gate.os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(roadmap_gate.main(), 1)

    def test_phase_issue_parent_chain_resolves_roadmap_marker(self):
        chain = [
            issue(101),
            issue(90, "<!-- speckit-roadmap: 012 -->"),
        ]
        self.assertEqual(roadmap_gate.roadmap_id_from_issue_chain(chain, ENTRIES), "012")

    def test_phase_without_parent_epic_fails_closed(self):
        with self.assertRaisesRegex(dependencies.InvalidData, "no parent roadmap Epic"):
            roadmap_gate.roadmap_id_from_issue_chain([issue(101)], ENTRIES)

    def test_parent_chain_cycle_fails(self):
        parents = {101: 90, 90: 101}
        issues = {101: issue(101), 90: issue(90)}

        def find_issue(number):
            return issues[number]

        def find_parent(number):
            return parents.get(number)

        with self.assertRaisesRegex(dependencies.InvalidData, "parent cycle"):
            roadmap_gate.load_issue_chain(101, find_issue, find_parent)

    def test_parent_chain_reaches_the_feature_epic(self):
        issues = {
            101: issue(101),
            90: issue(90),
            80: issue(80, "<!-- speckit-roadmap: 012 -->"),
        }
        parents = {101: 90, 90: 80}
        chain = roadmap_gate.load_issue_chain(
            101, issues.__getitem__, parents.get
        )
        self.assertEqual([item["number"] for item in chain], [101, 90, 80])
        self.assertEqual(roadmap_gate.roadmap_id_from_issue_chain(chain, ENTRIES), "012")

    def test_marker_for_missing_entry_is_an_error(self):
        chain = [issue(101), issue(90, "<!-- speckit-roadmap: 999 -->")]
        with self.assertRaisesRegex(dependencies.InvalidData, "unknown roadmap marker"):
            roadmap_gate.roadmap_id_from_issue_chain(chain, ENTRIES)

    def test_ambiguous_marker_identity_is_an_error(self):
        chain = [
            issue(101),
            issue(90, "<!-- speckit-roadmap: 012 -->"),
            issue(80, "<!-- speckit-roadmap: 005 -->"),
        ]
        with self.assertRaisesRegex(dependencies.InvalidData, "ambiguous roadmap identity"):
            roadmap_gate.roadmap_id_from_issue_chain(chain, ENTRIES)

    def test_phase_gate_requires_label_only_for_blocked_prerequisites(self):
        decision = roadmap_gate.evaluate_phase_status(
            "012", BLOCKED, {"prerequisites-acknowledged"}, accept_acknowledgement)
        self.assertEqual(decision["result"], "pass")
        self.assertTrue(decision["override"])

        decision = roadmap_gate.evaluate_phase_status(
            "012", BLOCKED, set(), accept_acknowledgement)
        self.assertEqual(decision["result"], "blocked")

    def test_acknowledgement_verification_is_required_and_never_fails_open(self):
        with self.assertRaises(TypeError):
            roadmap_gate.evaluate_phase_status(
                "012", BLOCKED, {"prerequisites-acknowledged"})
        decision = roadmap_gate.evaluate_phase_status(
            "012", BLOCKED, {"prerequisites-acknowledged"},
            lambda: (False, "cannot be verified"))
        self.assertEqual(decision["result"], "blocked")
        self.assertEqual(decision["acknowledgement_rejected"], "cannot be verified")
        self.assertFalse(hasattr(roadmap_gate, "evaluate_gate"))

    def test_human_label_never_overrides_status_or_identity_errors(self):
        error = {"result": "error", "entry": {"id": "012"},
                 "diagnostics": ["API failure"], "dependencies": []}
        for status, expected in ((error, "error"), (READY, "pass")):
            with self.subTest(status=status):
                decision = roadmap_gate.evaluate_phase_status(
                    "012", status, {"prerequisites-acknowledged"}, accept_acknowledgement)
                self.assertEqual(decision["result"], expected)

        with self.assertRaises(dependencies.InvalidData):
            roadmap_gate.roadmap_id_from_issue_chain([issue(101)], ENTRIES)
        with self.assertRaisesRegex(dependencies.InvalidData, "entry identity"):
            roadmap_gate.evaluate_phase_status(
                "012", {**READY, "entry": {"id": "005"}},
                {"prerequisites-acknowledged"}, accept_acknowledgement)
        with self.assertRaisesRegex(dependencies.InvalidData, "invalid dependency data"):
            roadmap_gate.evaluate_phase_status(
                "012", {**BLOCKED, "dependencies": [{"satisfied": False}]},
                {"prerequisites-acknowledged"}, accept_acknowledgement)

    def test_parsed_non_phase_branches_pass_without_roadmap_reads(self):
        for branch in ("123-chore-tooling-change", "005-workspace-feature"):
            with self.subTest(branch=branch):
                decision = roadmap_gate.non_phase_decision(
                    roadmap_gate.parse_gate_branch(branch))
                self.assertEqual(decision["result"], "pass")
                self.assertEqual(decision["branch_type"], "spec" if branch.startswith("005-") else "issue")

    def test_malformed_branch_and_status_data_fail_closed(self):
        with self.assertRaisesRegex(dependencies.InvalidData, "invalid branch"):
            roadmap_gate.parse_gate_branch("not-a-supported-branch")
        with self.assertRaisesRegex(dependencies.InvalidData, "unexpected status result"):
            roadmap_gate.evaluate_phase_status(
                "012", {"result": "mystery", "entry": {"id": "012"}}, set(),
                accept_acknowledgement)

    def test_waived_decision_lists_blockers_and_main_prints_them(self):
        blocked = {**BLOCKED, "dependencies": [{"id": "005", "satisfied": False},
                                               {"id": "030", "satisfied": False}]}
        decision = roadmap_gate.evaluate_phase_status(
            "012", blocked, {"prerequisites-acknowledged"}, accept_acknowledgement)
        self.assertEqual(decision["result"], "pass")
        self.assertEqual(decision["blockers"], ["005", "030"])
        with (
            patch.dict(roadmap_gate.os.environ, {"GITHUB_EVENT_PATH": "event.json"}, clear=True),
            patch.object(Path, "read_text", return_value="{}"),
            patch.object(roadmap_gate, "run_event", return_value=decision),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(roadmap_gate.main(), 0)
        self.assertIn("waived by prerequisites-acknowledged: 005, 030", errors.getvalue())

    def test_acknowledgement_requires_latest_label_event_by_a_human(self):
        human = labeled(created_at="2026-11-03T12:00:00Z", event_id=10)
        bot = labeled(actor_type="Bot", created_at="2026-11-03T11:00:00Z", event_id=20)
        later_bot = labeled(actor_type="Bot", created_at="2026-11-03T13:00:00Z", event_id=5)
        same_time_bot = labeled(actor_type="Bot", created_at="2026-11-03T12:00:00Z", event_id=11)
        app = labeled(event_id=30, created_at="2026-11-03T14:00:00Z",
                      app={"id": 1, "slug": "some-app"})
        for events, expected in (
            ([human], True),
            ([bot, human], True),
            ([human, bot], True),  # latest by created_at, not by list order
            ([later_bot, human], False),
            ([human, same_time_bot], False),  # equal timestamps: higher id is latest
            ([same_time_bot, human], False),
            ([human, labeled("Epic", "Bot", "2026-11-04T00:00:00Z", 99)], True),
            ([human, app], False),
            ([app], False),
            ([], False),
            ([labeled("Epic", "User")], False),
            ([labeled(actor_type=None)], False),
            ([{**human, "actor": {"login": "x"}}], False),
            ([{**human, "event": "unlabeled"}], False),
            ([{**human, "created_at": None}], False),
            ([{**human, "id": "10"}], False),
            ([{**human, "created_at": "not a timestamp"}], False),
            ([{**human, "created_at": "2026-11-03T12:00:00"}], False),  # naive
            ([human, {**human, "created_at": "2026-11-03T11:00:00"}], False),
            # Compared in UTC: 13:30+02:00 is 11:30Z, earlier than the 12:00Z human.
            ([human, labeled(actor_type="Bot", created_at="2026-11-03T13:30:00+02:00",
                             event_id=40)], True),
            # 14:30+02:00 is 12:30Z, later than the 12:00Z human.
            ([human, labeled(actor_type="Bot", created_at="2026-11-03T14:30:00+02:00",
                             event_id=1)], False),
        ):
            with self.subTest(events=events):
                valid, reason = roadmap_gate.acknowledgement_by_human(events)
                self.assertEqual(valid, expected)
                if not expected:
                    self.assertTrue(reason)
        valid, reason = roadmap_gate.acknowledgement_by_human([app])
        self.assertIn("GitHub App", reason)

    def test_label_events_are_paginated_and_validated(self):
        events = [[labeled()], [{"event": "closed"}]]
        with patch.object(roadmap_gate.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(events), ""
        )) as run:
            self.assertEqual(roadmap_gate.github_pr_events("owner/repo", 123),
                             events[0] + events[1])
            command = run.call_args.args[0]
            self.assertIn("repos/owner/repo/issues/123/events?per_page=100", command)
            self.assertIn("--paginate", command)
            self.assertIn("--slurp", command)
        for code, output in ((1, ""), (0, "bad json"), (0, "{}"), (0, "[{}]"), (0, "[[1]]")):
            with self.subTest(code=code, output=output), patch.object(
                roadmap_gate.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, output, "HTTP 403")
            ), self.assertRaises(dependencies.InvalidData):
                roadmap_gate.github_pr_events("owner/repo", 123)

    def run_gated_event(self, event, status, labels, events, chain=None):
        """Run a Phase/task event with GitHub and roadmap reads patched."""
        chain = chain or [issue(101), issue(90, "<!-- speckit-roadmap: 012 -->")]
        with (
            patch.object(roadmap_gate.check_roadmap, "validate_roadmap", return_value=ENTRIES),
            patch.object(roadmap_gate, "load_issue_chain", return_value=chain) as load,
            patch.object(roadmap_gate, "live_roadmap_status", return_value=status),
            patch.object(roadmap_gate, "github_pr_labels", return_value=labels),
            patch.object(roadmap_gate, "github_pr_events", return_value=events) as fetch_events,
        ):
            return roadmap_gate.run_event(event, "owner/repo"), load, fetch_events

    def test_run_event_rejects_bot_applied_acknowledgement(self):
        event = {"number": 123, "pull_request": {
            "number": 123, "head": {"ref": "101-phase-1-build-feature"}}}
        for actor_type, expected in (("Bot", "blocked"), ("User", "pass"), (None, "blocked")):
            with self.subTest(actor_type=actor_type):
                decision, _, _ = self.run_gated_event(
                    event, BLOCKED, {"prerequisites-acknowledged"},
                    [labeled(actor_type=actor_type)])
                self.assertEqual(decision["result"], expected)
                if expected == "blocked":
                    self.assertIn("prerequisites-acknowledged", decision["acknowledgement_rejected"])
        rejected = {"result": "blocked", "roadmap_id": "012", "blockers": ["005"],
                    "acknowledgement_rejected": "label was applied by a bot account"}
        with (
            patch.dict(roadmap_gate.os.environ, {"GITHUB_EVENT_PATH": "event.json"}, clear=True),
            patch.object(Path, "read_text", return_value="{}"),
            patch.object(roadmap_gate, "run_event", return_value=rejected),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(roadmap_gate.main(), 1)
        self.assertIn("applied by a bot account", errors.getvalue())

    def test_ready_status_never_reads_label_events(self):
        event = {"number": 123, "pull_request": {
            "number": 123, "head": {"ref": "101-phase-1-build-feature"}}}
        decision, _, fetch_events = self.run_gated_event(
            event, READY, {"prerequisites-acknowledged"}, [labeled(actor_type="Bot")])
        self.assertEqual(decision["result"], "pass")
        fetch_events.assert_not_called()

    USER_LABELED_PAYLOAD = {
        "number": 123, "action": "labeled",
        "label": {"name": "prerequisites-acknowledged"},
        "sender": {"login": "someone", "type": "User"},
        "pull_request": {"number": 123, "head": {"ref": "101-phase-1-build-feature"}}}

    def test_labeled_payload_from_a_user_is_human_evidence(self):
        # Used only while the events API shows no labeled event for the label yet.
        decision, _, fetch_events = self.run_gated_event(
            self.USER_LABELED_PAYLOAD, BLOCKED, {"prerequisites-acknowledged"}, [])
        self.assertEqual(decision["result"], "pass")
        self.assertTrue(decision["override"])
        fetch_events.assert_called_once_with("owner/repo", 123)

    def test_label_events_are_authoritative_over_a_user_payload(self):
        app = labeled(app={"id": 1, "slug": "some-app"})
        for events, expected, reason in (
            ([app], "blocked", "GitHub App"),
            ([labeled(actor_type="Bot")], "blocked", "bot account"),
            ([labeled()], "pass", None),
        ):
            with self.subTest(events=events):
                decision, _, fetch_events = self.run_gated_event(
                    self.USER_LABELED_PAYLOAD, BLOCKED, {"prerequisites-acknowledged"}, events)
                self.assertEqual(decision["result"], expected)
                fetch_events.assert_called_once_with("owner/repo", 123)
                if reason:
                    self.assertIn(reason, decision["acknowledgement_rejected"])

    def test_no_payload_evidence_and_no_label_event_is_rejected(self):
        event = {"number": 123, "pull_request": {
            "number": 123, "head": {"ref": "101-phase-1-build-feature"}}}
        decision, _, _ = self.run_gated_event(
            event, BLOCKED, {"prerequisites-acknowledged"}, [{"event": "closed"}])
        self.assertEqual(decision["result"], "blocked")
        self.assertIn("cannot be verified", decision["acknowledgement_rejected"])

    def test_label_event_errors_fail_closed_despite_a_user_payload(self):
        chain = [issue(101), issue(90, "<!-- speckit-roadmap: 012 -->")]
        with (
            patch.object(roadmap_gate.check_roadmap, "validate_roadmap", return_value=ENTRIES),
            patch.object(roadmap_gate, "load_issue_chain", return_value=chain),
            patch.object(roadmap_gate, "live_roadmap_status", return_value=BLOCKED),
            patch.object(roadmap_gate, "github_pr_labels",
                         return_value={"prerequisites-acknowledged"}),
            patch.object(roadmap_gate, "github_pr_events", side_effect=dependencies.InvalidData(
                "Roadmap gate: cannot read pull request events: HTTP 502")),
            self.assertRaisesRegex(dependencies.InvalidData, "cannot read pull request events"),
        ):
            roadmap_gate.run_event(self.USER_LABELED_PAYLOAD, "owner/repo")

    def test_user_payload_without_the_label_in_fresh_labels_is_blocked(self):
        decision, _, fetch_events = self.run_gated_event(
            self.USER_LABELED_PAYLOAD, BLOCKED, set(), [labeled()])
        self.assertEqual(decision["result"], "blocked")
        fetch_events.assert_not_called()

    def test_other_payloads_fall_back_to_label_events(self):
        base = {"number": 123, "pull_request": {
            "number": 123, "head": {"ref": "101-phase-1-build-feature"}}}
        for payload in (
            {"action": "labeled", "label": {"name": "prerequisites-acknowledged"},
             "sender": {"type": "Bot"}},
            {"action": "labeled", "label": {"name": "Epic"}, "sender": {"type": "User"}},
            {"action": "synchronize", "sender": {"type": "User"}},
            {"action": "unlabeled", "label": {"name": "prerequisites-acknowledged"},
             "sender": {"type": "User"}},
        ):
            for events, expected in (([labeled()], "pass"), ([], "blocked")):
                with self.subTest(payload=payload, expected=expected):
                    decision, _, fetch_events = self.run_gated_event(
                        {**base, **payload}, BLOCKED, {"prerequisites-acknowledged"}, events)
                    self.assertEqual(decision["result"], expected)
                    fetch_events.assert_called_once_with("owner/repo", 123)

    def test_is_gated_branch_returns_a_bool(self):
        for branch, expected in (
            ("101-phase-1-build-feature", True),
            ("403-t023-phase-1-build-feature", True),
            ("123-chore-tooling-change", False),
        ):
            with self.subTest(branch=branch):
                result = roadmap_gate.is_gated_branch(roadmap_gate.parse_gate_branch(branch))
                self.assertIs(result, expected)

    def test_legacy_task_branch_is_an_invalid_branch_or_not_gated(self):
        with self.assertRaisesRegex(dependencies.InvalidData, "invalid branch"):
            roadmap_gate.parse_gate_branch("22-t023-bootstrap-database-user")
        metadata = roadmap_gate.parse_gate_branch("403-t023-bootstrap-database-user")
        self.assertEqual(metadata.branch_type, "spec")
        self.assertIs(roadmap_gate.is_gated_branch(metadata), False)

    def test_feature_marker_alone_is_no_roadmap_marker(self):
        chain = [issue(101), issue(90, "<!-- speckit-feature: specs/012-some-feature -->")]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(
            dependencies.InvalidData, "has no roadmap marker"
        ):
            roadmap_gate.roadmap_id_from_issue_chain(chain, ENTRIES)

    def test_non_phase_work_ids_pass_without_roadmap_reads(self):
        for work_id in ("chore", "ci", "docs", "fix", "refactor", "spec", "test"):
            with self.subTest(work_id=work_id):
                event = {"pull_request": {"head": {"ref": f"123-{work_id}-change"}}}
                self.assertEqual(roadmap_gate.run_event(event, None)["result"], "pass")

    def test_run_event_resolves_roadmap_identity_once(self):
        chain = [issue(101), {**issue(91, "<!-- speckit-roadmap: 005 -->"), "labels": []},
                 issue(90, "<!-- speckit-roadmap: 012 -->")]
        event = {"number": 123, "pull_request": {
            "number": 123, "head": {"ref": "101-phase-1-build-feature"}}}
        with (
            patch.object(roadmap_gate.check_roadmap, "validate_roadmap", return_value=ENTRIES),
            patch.object(roadmap_gate, "load_issue_chain", return_value=chain),
            patch.object(roadmap_gate, "live_roadmap_status", return_value=READY),
            patch.object(roadmap_gate, "github_pr_labels", return_value=set()),
            patch.object(roadmap_gate, "roadmap_id_from_issue_chain",
                         wraps=roadmap_gate.roadmap_id_from_issue_chain) as resolve,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(roadmap_gate.run_event(event, "owner/repo")["result"], "pass")
        self.assertEqual(resolve.call_count, 1)

    def test_non_phase_branch_does_not_call_phase_evaluation(self):
        with patch.object(roadmap_gate, "evaluate_phase_status") as evaluate:
            decision = roadmap_gate.run_event(
                {"pull_request": {"head": {"ref": "123-chore-tooling-change"}}}, None)
        evaluate.assert_not_called()
        self.assertEqual(decision["result"], "pass")
        self.assertEqual(decision["branch_type"], "issue")

    def test_workflow_uses_trusted_base_and_read_only_permissions(self):
        workflow = (ROOT / ".github/workflows/roadmap-gate.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertRegex(workflow, r"types:\s*\[opened,\s*reopened,\s*synchronize,\s*labeled,\s*unlabeled\]")
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", workflow)
        self.assertNotRegex(workflow, r"ref:\s*\$\{\{\s*github\.event\.pull_request\.head")
        self.assertRegex(
            workflow,
            r"permissions:\s*\n\s+contents:\s+read\s*\n\s+issues:\s+read\s*\n\s+pull-requests:\s+read\s*\n",
        )
        self.assertNotRegex(workflow, r"pull-requests:\s*write")
        self.assertRegex(workflow, r"actions/checkout@[a-f0-9]{40}\s+# v7")
        self.assertIn("python3 scripts/roadmap_gate.py", workflow)
        self.assertIsNone(re.search(r"permissions:[\s\S]*?issues:\s*write", workflow))


if __name__ == "__main__":
    unittest.main()
