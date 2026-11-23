import contextlib
import dataclasses
import io
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_setup  # noqa: E402
import bootstrap  # noqa: E402
import bootstrap_check  # noqa: E402
import bootstrap_github  # noqa: E402
import bootstrap_start  # noqa: E402
import bootstrap_steps  # noqa: E402
from display_text_testing import raw_unsafe  # noqa: E402
import project_board  # noqa: E402
import pr_conventions  # noqa: E402
import project_context  # noqa: E402
import roadmap_issues  # noqa: E402
from test_bootstrap import ReadingRun  # noqa: E402
from test_bootstrap_check import CONTEXT as CHECK_CONTEXT, ROADMAP  # noqa: E402
from test_bootstrap_github import CONTEXT, FakeGitHub  # noqa: E402

REPO = "acme/widgets"
IDS = ("prerequisites", "bootstrap_issue", "project_context", "constitution", "roadmap",
       "github_settings", "board_credential", "board_workflows", "pull_request", "merge",
       "first_sync")
# id: (blocking, actor, needs_decision, needs_confirmation, bypasses_review), from data-model.md
TABLE = {
    "prerequisites": (True, "user", False, False, False),
    "bootstrap_issue": (True, "engine", False, True, True),
    "project_context": (True, "engine", True, True, True),
    "constitution": (True, "user", True, False, False),
    "roadmap": (True, "user", True, False, False),
    "github_settings": (True, "engine", False, True, True),
    "board_credential": (False, "user", False, False, False),
    "board_workflows": (False, "user_web", False, False, False),
    "pull_request": (True, "engine", False, True, False),
    "merge": (True, "user_web", False, False, False),
    "first_sync": (True, "engine", False, True, True),
}
STATES = ("done", "todo", "manual", "optional", "waiting", "not_checked")


UNREAD_TITLE = (
    "Board credential not configured (optional; organization secrets not checked)")


class CatalogueTest(unittest.TestCase):
    def test_eleven_steps_in_the_fixed_order(self):
        self.assertEqual(IDS, tuple(entry.id for entry in bootstrap_steps.CATALOGUE))

    def test_fixed_values_match_the_data_model_table(self):
        for entry in bootstrap_steps.CATALOGUE:
            with self.subTest(step=entry.id):
                self.assertEqual(TABLE[entry.id], (
                    entry.blocking, entry.actor, entry.needs_decision,
                    entry.needs_confirmation, entry.bypasses_review))

    def test_states_are_limited_to_the_six(self):
        self.assertEqual(STATES, bootstrap_steps.STATES)
        with self.assertRaises(ValueError):
            bootstrap_steps.make_step("merge", "finished", "Merged")

    def test_every_catalogue_step_builds_in_every_allowed_state(self):
        for entry in bootstrap_steps.CATALOGUE:
            for state in STATES:
                if state in ("manual", "optional") and entry.blocking:
                    continue
                step = bootstrap_steps.make_step(entry.id, state, "Title")
                self.assertEqual((entry.id, state, entry.blocking), (step.id, step.state,
                                                                     step.blocking))

    def test_manual_and_optional_only_on_non_blocking_steps(self):
        for entry in bootstrap_steps.CATALOGUE:
            for state in ("manual", "optional"):
                with self.subTest(step=entry.id, state=state):
                    if entry.blocking:
                        with self.assertRaises(ValueError):
                            bootstrap_steps.make_step(entry.id, state, "Title")
                    else:
                        bootstrap_steps.make_step(entry.id, state, "Title")

    def test_unknown_step_id_is_refused(self):
        with self.assertRaises(ValueError):
            bootstrap_steps.make_step("nope", "todo", "Title")

    def test_performable_only_with_confirmation(self):
        for entry in bootstrap_steps.CATALOGUE:
            with self.subTest(step=entry.id):
                if entry.needs_confirmation:
                    step = bootstrap_steps.make_step(entry.id, "todo", "Title", performable=True)
                    self.assertTrue(step.performable)
                else:
                    with self.assertRaises(ValueError):
                        bootstrap_steps.make_step(entry.id, "todo", "Title", performable=True)

    def test_performable_defaults_to_false_and_details_to_empty(self):
        step = bootstrap_steps.make_step("merge", "todo", "Title")
        self.assertFalse(step.performable)
        self.assertEqual({}, step.details)
        self.assertIsNone(step.action)


class FakeRepo:
    """Stands in for gh: answers the repository-context reads and records every call."""

    def __init__(self, default="main", present=None, error_on=None, no_default=False,
                 tip_error=False, view_output=None, tip_output="abc123\n",
                 context_text="[project]\n", error_message="HTTP 500: server error"):
        self.calls = []
        self.default = default
        self.present = bootstrap_steps.BOOTSTRAP_FILES if present is None else present
        self.error_on = error_on
        self.no_default = no_default
        self.tip_error = tip_error
        self.view_output = view_output
        self.tip_output = tip_output
        self.context_text = context_text
        self.error_message = error_message

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:4] == ["gh", "repo", "view", REPO]:
            if self.no_default:
                raise bootstrap_github.CommandError("HTTP 500: boom")
            if self.view_output is not None:
                return self.view_output
            return json.dumps({"defaultBranchRef": {"name": self.default}})
        url = next((a for a in argv[2:] if a.startswith("repos/")), "")
        if argv[:2] == ["gh", "api"] and "/contents/" in url:
            path, _, ref = url.partition("/contents/")[2].partition("?ref=")
            raw = ["-H", "Accept: application/vnd.github.raw"]
            if (argv[2:4] == raw) != (path == "project-context.toml"):
                raise AssertionError(f"only project-context.toml is read raw: {argv}")
            if urllib.parse.unquote(ref) != self.default:
                raise AssertionError(f"wrong ref {argv}")
            if path == self.error_on:
                raise bootstrap_github.CommandError(self.error_message)
            if path not in self.present:
                raise bootstrap_github.CommandError("gh: Not Found (HTTP 404)")
            return self.context_text if path == "project-context.toml" else "{}"
        if argv[:2] == ["gh", "api"] and "/branches/" in url:
            if self.tip_error:
                raise bootstrap_github.CommandError("HTTP 500: boom")
            return self.tip_output
        raise AssertionError(f"unexpected command {argv}")


class RepositoryContextTest(unittest.TestCase):
    def read(self, run):
        return bootstrap_steps.read_repository_context(REPO, run)

    def test_all_three_files_on_the_default_branch_mean_merged(self):
        run = FakeRepo(default="trunk")

        context = self.read(run)

        self.assertEqual("trunk", context.default_branch)
        self.assertIs(True, context.bootstrap_merged)
        self.assertEqual("abc123", context.default_tip)
        self.assertIn(["gh", "repo", "view", REPO, "--json", "defaultBranchRef"], run.calls)
        self.assertIn(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                       f"repos/{REPO}/contents/project-context.toml?ref=trunk"], run.calls)
        for path in (".specify/memory/constitution.md", "ROADMAP.md"):
            self.assertIn(["gh", "api", f"repos/{REPO}/contents/{path}?ref=trunk"], run.calls)
        self.assertIn(["gh", "api", f"repos/{REPO}/branches/trunk", "--jq", ".commit.sha"],
                      run.calls)

    def test_the_context_text_is_kept_from_the_one_raw_read(self):
        run = FakeRepo(context_text="[project]\nname = 'x'\n")

        context = self.read(run)

        self.assertEqual("[project]\nname = 'x'\n", context.context_text)
        self.assertIsNone(context.error)
        self.assertEqual(1, sum("project-context.toml" in " ".join(c) for c in run.calls))

    def test_no_text_and_no_error_when_the_file_is_absent(self):
        context = self.read(FakeRepo(present=(".specify/memory/constitution.md", "ROADMAP.md")))

        self.assertIsNone(context.context_text)
        self.assertIsNone(context.error)
        self.assertIs(False, context.bootstrap_merged)

    def test_a_read_error_keeps_its_first_line(self):
        for failing in bootstrap_steps.BOOTSTRAP_FILES:
            with self.subTest(failing=failing):
                run = FakeRepo(error_on=failing, error_message="HTTP 502: bad gateway\nmore")

                context = self.read(run)

                self.assertIsNone(context.bootstrap_merged)
                self.assertEqual("HTTP 502: bad gateway", context.error)
                if failing == "project-context.toml":
                    self.assertIsNone(context.context_text)

    def test_an_unreadable_default_branch_keeps_its_error(self):
        context = self.read(FakeRepo(no_default=True))

        self.assertEqual("HTTP 500: boom", context.error)
        self.assertIsNone(context.context_text)

    def test_a_missing_file_is_not_merged(self):
        for missing in bootstrap_steps.BOOTSTRAP_FILES:
            with self.subTest(missing=missing):
                present = tuple(f for f in bootstrap_steps.BOOTSTRAP_FILES if f != missing)

                context = self.read(FakeRepo(present=present))

                self.assertIs(False, context.bootstrap_merged)
                self.assertEqual("main", context.default_branch)

    def test_a_404_on_every_file_is_not_merged(self):
        self.assertIs(False, self.read(FakeRepo(present=())).bootstrap_merged)

    def test_another_error_makes_merged_unknown(self):
        context = self.read(FakeRepo(error_on="ROADMAP.md"))

        self.assertIsNone(context.bootstrap_merged)
        self.assertEqual("main", context.default_branch)

    def test_a_404_wins_over_another_error_and_the_error_is_kept(self):
        run = FakeRepo(present=("project-context.toml",), error_on="ROADMAP.md")

        context = self.read(run)

        self.assertIs(False, context.bootstrap_merged)
        self.assertIsNotNone(context.error)

    def test_unreadable_default_branch_gives_nulls_and_no_file_reads(self):
        run = FakeRepo(no_default=True)

        context = self.read(run)

        self.assertEqual((None, None, None), (context.default_branch, context.bootstrap_merged,
                                              context.default_tip))
        self.assertEqual([["gh", "repo", "view", REPO, "--json", "defaultBranchRef"]], run.calls)

    def test_an_empty_default_branch_name_is_unreadable(self):
        for name in ("", None):
            with self.subTest(name=name):
                run = FakeRepo(default=name)

                context = self.read(run)

                self.assertEqual((None, None, None), (
                    context.default_branch, context.bootstrap_merged, context.default_tip))
                self.assertEqual(1, len(run.calls))

    def test_malformed_repo_view_output_gives_nulls(self):
        for output in ("not json", "{}", '{"defaultBranchRef": null}', "[]"):
            with self.subTest(output=output):
                run = FakeRepo(view_output=output)

                context = self.read(run)

                self.assertEqual((None, None, None), (
                    context.default_branch, context.bootstrap_merged, context.default_tip))
                self.assertEqual(1, len(run.calls))

    def test_empty_tip_output_is_none(self):
        context = self.read(FakeRepo(tip_output="\n"))

        self.assertIsNone(context.default_tip)
        self.assertIs(True, context.bootstrap_merged)

    def test_branch_names_are_escaped_but_slashes_are_not(self):
        for name, encoded in (("release/1.0", "release/1.0"), ("a#b", "a%23b")):
            with self.subTest(name=name):
                run = FakeRepo(default=name)

                context = self.read(run)

                self.assertEqual(name, context.default_branch)
                self.assertIs(True, context.bootstrap_merged)
                self.assertIn(
                    ["gh", "api", f"repos/{REPO}/contents/ROADMAP.md?ref={encoded}"], run.calls)
                self.assertIn(["gh", "api", f"repos/{REPO}/branches/{encoded}",
                               "--jq", ".commit.sha"], run.calls)

    def test_unreadable_tip_is_none_and_does_not_change_merged(self):
        context = self.read(FakeRepo(tip_error=True))

        self.assertIsNone(context.default_tip)
        self.assertIs(True, context.bootstrap_merged)

    def test_no_git_command_is_ever_run(self):
        run = FakeRepo()

        self.read(run)
        absent = FakeRepo(present=())
        self.read(absent)

        self.assertEqual([], [c for c in run.calls + absent.calls if c[0] != "gh"])

    def test_the_context_is_read_once_per_call(self):
        run = FakeRepo()

        self.read(run)

        self.assertEqual(1, sum(1 for c in run.calls if c[:3] == ["gh", "repo", "view"]))
        self.assertEqual(len(run.calls), len(set(map(tuple, run.calls))))


class Scripted:
    """Stands in for gh and git: the first rule whose prefix matches the call answers it.

    A rule's answer is a string or an exception to raise (or a callable returning one of them).
    A call no rule matches fails the test, so nothing can reach the real GitHub.
    """

    def __init__(self, *rules):
        self.rules = list(rules)
        self.calls = []

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        for prefix, answer in self.rules:
            if argv[:len(prefix)] == list(prefix):
                answer = answer(argv) if callable(answer) else answer
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected command {argv}")

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def gh_error(text, stdout="", returncode=1):
    return bootstrap_github.CommandError(text, stdout=stdout, returncode=returncode)


class RunCommandTest(unittest.TestCase):
    def test_a_failure_keeps_stdout_and_the_exit_code_and_the_message(self):
        done = mock.Mock(returncode=3, stdout="[skip] blocked\n", stderr="")
        with mock.patch.object(bootstrap.subprocess, "run", return_value=done):
            with self.assertRaises(bootstrap_github.CommandError) as raised:
                bootstrap.run_command(["python3", "x.py", "--flag"])

        self.assertEqual("python3 x.py --flag failed", str(raised.exception))
        self.assertEqual("[skip] blocked\n", raised.exception.stdout)
        self.assertEqual(3, raised.exception.returncode)

    def test_stderr_stays_the_message(self):
        done = mock.Mock(returncode=1, stdout="out", stderr="boom\n")
        with mock.patch.object(bootstrap.subprocess, "run", return_value=done):
            with self.assertRaises(bootstrap_github.CommandError) as raised:
                bootstrap.run_command(["gh", "x"])

        self.assertEqual("boom", str(raised.exception))


def auth_output(scopes="'gist', 'project', 'repo'"):
    line = f"  - Token scopes: {scopes}\n" if scopes is not None else ""
    return ("github.com\n  \u2713 Logged in to github.com account tester (keyring)\n"
            f"  - Active account: true\n{line}")


AUTH = ["gh", "auth", "status"]
AUTH_CALL = AUTH + ["--active", "--hostname", "github.com"]


class PrerequisitesTest(unittest.TestCase):
    def detect(self, run, agents_ok=True):
        step = bootstrap_check.Step(
            bootstrap_check.OK if agents_ok else bootstrap_check.FAIL,
            "Agent files are generated and up to date" if agents_ok
            else "Agent files are out of date (1 of 3)",
            None if agents_ok else "run `python3 scripts/agent_setup.py sync`",
            fixable_by_sync=not agents_ok)
        with mock.patch.object(bootstrap_check, "check_agents", return_value=step), \
                mock.patch.object(agent_setup, "configured_names", return_value=[]):
            return bootstrap_steps.detect_prerequisites(Path("."), run)

    def detect_agents(self, fixable_by_sync, next_action):
        step = bootstrap_check.Step(
            bootstrap_check.FAIL, "Agent files are out of date (1 of 3)", next_action,
            fixable_by_sync=fixable_by_sync)
        with mock.patch.object(bootstrap_check, "check_agents", return_value=step):
            return bootstrap_steps.detect_prerequisites(
                Path("."), Scripted((AUTH, auth_output())))

    def test_the_agent_command_follows_fixable_by_sync_not_the_text(self):
        for fixable, text, command in (
            (False, "run `python3 scripts/agent_setup.py sync`",
             "python3 scripts/agent_setup.py check"),
            (True, "regenerate the agent files", "python3 scripts/agent_setup.py sync"),
        ):
            with self.subTest(fixable=fixable):
                step = self.detect_agents(fixable, text)

                self.assertEqual(command, step.action["command"])

    def test_done_with_the_project_scope_listed(self):
        step = self.detect(Scripted((AUTH, auth_output())))

        self.assertEqual(
            ("prerequisites", "done", {"project_scope": "listed", "agent_tools": None}),
            (step.id, step.state, step.details))
        self.assertIsNone(step.action)
        self.assertFalse(step.performable)

    def test_a_missing_project_scope_is_todo_with_the_refresh_command(self):
        step = self.detect(Scripted((AUTH, auth_output("'gist', 'repo'"))))

        self.assertEqual(("todo", "missing"), (step.state, step.details["project_scope"]))
        self.assertEqual("gh auth refresh -s project", step.action["command"])
        self.assertEqual("docs/bootstrap.md#prerequisites", step.action["doc"])

    def test_read_project_is_not_the_project_scope(self):
        step = self.detect(Scripted((AUTH, auth_output("'read:project', 'repo'"))))

        self.assertEqual(("todo", "missing"), (step.state, step.details["project_scope"]))

    def test_unreadable_scopes_do_not_block(self):
        for output in (auth_output(None), auth_output("none")):
            with self.subTest(output=output):
                step = self.detect(Scripted((AUTH, output)))

                self.assertEqual(("done", "not_readable"),
                                 (step.state, step.details["project_scope"]))

    def test_gh_missing_or_logged_out_is_todo_with_the_login_command(self):
        for message in ("cannot run gh: No such file or directory",
                        "You are not logged into any GitHub hosts."):
            with self.subTest(message=message):
                run = Scripted((AUTH, gh_error(message)))

                step = self.detect(run)

                self.assertEqual("todo", step.state)
                self.assertEqual("gh auth login", step.action["command"])
                self.assertIn(message.split(":")[0], step.title)
                self.assertNotIn("project_scope", step.details)
                self.assertEqual(1, len(run.calls))

    def test_an_old_gh_without_the_active_flag_needs_an_upgrade(self):
        run = Scripted((AUTH, gh_error("unknown flag: --active\nUsage: gh auth status")))

        step = self.detect(run)

        self.assertEqual("todo", step.state)
        self.assertIn("2.40", step.title)
        self.assertNotIn("not logged in", step.title)
        self.assertNotEqual("gh auth login", step.action["command"])

    def test_outdated_agent_files_are_todo_with_the_sync_command(self):
        step = self.detect(Scripted((AUTH, auth_output())), agents_ok=False)

        self.assertEqual("todo", step.state)
        self.assertIn("Agent files are out of date", step.title)
        self.assertEqual("python3 scripts/agent_setup.py sync", step.action["command"])
        self.assertEqual("listed", step.details["project_scope"])

    def test_other_agent_problems_are_checked_not_synced(self):
        for title in ("Agent setup has problems: bad tool", "Agent files cannot be checked: x"):
            with self.subTest(title=title):
                failed = bootstrap_check.Step(
                    bootstrap_check.FAIL, title, "run `python3 scripts/agent_setup.py check`")
                with mock.patch.object(bootstrap_check, "check_agents", return_value=failed):
                    step = bootstrap_steps.detect_prerequisites(
                        Path("."), Scripted((AUTH, auth_output())))

                self.assertEqual("python3 scripts/agent_setup.py check", step.action["command"])

    def test_login_comes_before_scope_and_scope_before_agent_files(self):
        step = self.detect(Scripted((AUTH, auth_output("'repo'"))), agents_ok=False)

        self.assertEqual("gh auth refresh -s project", step.action["command"])

    def test_it_only_reads(self):
        run = Scripted((AUTH, auth_output()))

        self.detect(run)

        self.assertEqual([AUTH_CALL], run.calls)


class AgentToolsDetailTest(unittest.TestCase):
    """`details.agent_tools` on `prerequisites` (FR-001a, research D10 and D17)."""

    def detect(self, run, saved, agents=None):
        agents = agents or bootstrap_check.Step(bootstrap_check.OK, "Agent files ok", None)
        with mock.patch.object(agent_setup, "configured_names", return_value=saved), \
                mock.patch.object(bootstrap_check, "check_agents", return_value=agents):
            return bootstrap_steps.detect_prerequisites(Path("."), run)

    def branches(self):
        stale = bootstrap_check.Step(
            bootstrap_check.FAIL, "Agent files are out of date (1 of 3)", "regenerate",
            fixable_by_sync=True)
        return {
            "gh missing": (Scripted((AUTH, gh_error("cannot run gh: not found"))), None),
            "gh too old": (Scripted((AUTH, gh_error("unknown flag: --active"))), None),
            "scope missing": (Scripted((AUTH, auth_output("'repo'"))), None),
            "stale": (Scripted((AUTH, auth_output())), stale),
            "done": (Scripted((AUTH, auth_output())), None),
        }

    def test_the_key_is_in_every_branch_including_gh_failures(self):
        for name, (run, agents) in self.branches().items():
            for saved, expected in (([], None), (["codex", "claude"], ["claude", "codex"])):
                with self.subTest(branch=name, saved=saved):
                    step = self.detect(run, saved, agents)

                    self.assertEqual(expected, step.details["agent_tools"])
                    run.calls.clear()

    def test_the_key_is_in_the_branch_where_the_agent_check_is_unreadable(self):
        with mock.patch.object(agent_setup, "configured_names", return_value=["copilot"]), \
                mock.patch.object(bootstrap_check, "check_agents", side_effect=OSError("boom")):
            step = bootstrap_steps.detect_prerequisites(
                Path("."), Scripted((AUTH, auth_output())))

        self.assertEqual(["copilot"], step.details["agent_tools"])
        self.assertEqual("listed", step.details["project_scope"])

    def test_unknown_saved_names_follow_the_known_ones_in_saved_order(self):
        step = self.detect(Scripted((AUTH, auth_output())),
                           ["zed", "codex", "aider", "claude", "zed"])

        self.assertEqual(["claude", "codex", "zed", "aider"], step.details["agent_tools"])

    def test_state_title_and_action_do_not_depend_on_the_choice(self):
        for name, (run, agents) in self.branches().items():
            with self.subTest(branch=name):
                without = self.detect(run, [], agents)
                run.calls.clear()
                saved = self.detect(run, ["claude"], agents)
                run.calls.clear()

                self.assertEqual((without.state, without.title, without.action),
                                 (saved.state, saved.title, saved.action))

    def test_gh_failures_still_have_no_project_scope(self):
        for name in ("gh missing", "gh too old"):
            run, agents = self.branches()[name]
            with self.subTest(branch=name):
                step = self.detect(run, ["claude"], agents)

                self.assertEqual({"agent_tools"}, set(step.details))

    def test_the_choice_is_read_without_a_github_call(self):
        run = Scripted((AUTH, auth_output()))

        self.detect(run, ["claude"])

        self.assertEqual([AUTH_CALL], run.calls)

    def test_it_reads_the_local_git_config_of_the_root(self):
        environ = mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        )
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("GIT_DIR", "GIT_WORK_TREE"):
            os.environ.pop(name, None)  # restored by mock.patch.dict on stop
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "-C", tmp, "config", "--local", "agent-setup.tools",
                            "codex,claude"], check=True)
            ok = bootstrap_check.Step(bootstrap_check.OK, "ok", None)
            with mock.patch.object(bootstrap_check, "check_agents", return_value=ok):
                step = bootstrap_steps.detect_prerequisites(
                    Path(tmp), Scripted((AUTH, auth_output())))

        self.assertEqual(["claude", "codex"], step.details["agent_tools"])


MARKER = "<!-- spec-toolkit:bootstrap -->"


def issue(number, title="CHORE: bootstrap", body=MARKER, author="tester"):
    return {"number": number, "title": title, "body": body, "author": {"login": author}}


def context(merged=False, default="main", tip="abc123", text=None, error=None):
    return bootstrap_steps.RepositoryContext(
        default_branch=default, bootstrap_merged=merged, default_tip=tip, context_text=text,
        error=error)


class IssueWorld:
    """The answers gh and git give while bootstrap is not merged."""

    def __init__(self, issues=(), local=(), remote=(), current="main", status="", login="tester",
                 issues_error=None, branches_error=None):
        self.rules = [
            (["gh", "api", "user"], f"{login}\n"),
            (["gh", "issue", "list"], issues_error or json.dumps(list(issues))),
            (["gh", "api", "--paginate"], branches_error or "".join(f"{b}\n" for b in remote)),
            (["git", "branch", "--show-current"], f"{current}\n"),
            (["git", "branch"], "".join(f"{b}\n" for b in local)),
            (["git", "status"], status),
        ]

    def run(self):
        return Scripted(*self.rules)


class BootstrapIssueTest(unittest.TestCase):
    def detect(self, world, merged=False, repo=REPO):
        run = world.run()
        step = bootstrap_steps.detect_bootstrap_issue(Path("."), run, repo, context(merged))
        return step, run

    def candidates(self, *pairs):
        return [{"number": n, "title": "CHORE: bootstrap", "branch": b} for n, b in pairs]

    def test_merged_bootstrap_is_done_without_any_call(self):
        step, run = self.detect(IssueWorld(), merged=True)

        self.assertEqual("done", step.state)
        self.assertEqual([], run.calls)

    def test_one_candidate_with_its_branch_checked_out_is_done(self):
        world = IssueWorld([issue(7)], local=["7-chore-bootstrap", "main"],
                           current="7-chore-bootstrap")

        step, _ = self.detect(world)

        self.assertEqual("done", step.state)
        self.assertEqual(self.candidates((7, "7-chore-bootstrap")), step.details["candidates"])
        self.assertIsNone(step.action)

    def test_one_candidate_with_a_local_branch_that_is_not_checked_out(self):
        world = IssueWorld([issue(7)], local=["7-chore-bootstrap", "main"])

        step, _ = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual("git switch 7-chore-bootstrap", step.action["command"])

    def test_one_candidate_whose_branch_exists_only_on_github(self):
        world = IssueWorld([issue(7)], local=["main"], remote=["main", "7-chore-bootstrap"])

        step, run = self.detect(world)

        self.assertEqual("todo", step.state)
        self.assertEqual("git fetch origin 7-chore-bootstrap && git switch 7-chore-bootstrap",
                         step.action["command"])
        self.assertEqual(self.candidates((7, "7-chore-bootstrap")), step.details["candidates"])
        self.assertIn(["gh", "api", "--paginate", f"repos/{REPO}/branches", "--jq", ".[].name"],
                      run.calls)

    def test_one_candidate_without_a_branch_is_developed_from_the_default_branch(self):
        world = IssueWorld([issue(7)], local=["main"])

        step, _ = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual("gh issue develop 7 --base main --name 7-chore-bootstrap --checkout",
                         step.action["command"])
        self.assertEqual(self.candidates((7, None)), step.details["candidates"])

    def test_no_candidate_means_creating_the_issue_and_its_branch(self):
        step, _ = self.detect(IssueWorld([], local=["main"]))

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual([], step.details["candidates"])
        self.assertIsNone(step.action["command"])

    def test_several_candidates_are_listed_and_the_user_chooses(self):
        world = IssueWorld([issue(7), issue(9, "CHORE: bootstrap again")],
                           local=["main"], current="main")

        step, _ = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual(
            [{"number": 7, "title": "CHORE: bootstrap", "branch": None},
             {"number": 9, "title": "CHORE: bootstrap again", "branch": None}],
            step.details["candidates"])
        self.assertIn("--issue", step.action["text"])

    def test_several_candidates_with_one_branch_checked_out_is_that_choice(self):
        world = IssueWorld([issue(7), issue(9, "CHORE: bootstrap again")],
                           local=["9-chore-bootstrap-x"], current="9-chore-bootstrap-x")

        step, _ = self.detect(world)

        self.assertEqual("done", step.state)
        self.assertIn("#9", step.title)
        self.assertEqual(
            [{"number": 7, "title": "CHORE: bootstrap", "branch": None},
             {"number": 9, "title": "CHORE: bootstrap again", "branch": "9-chore-bootstrap-x"}],
            step.details["candidates"])

    def test_a_marker_issue_of_another_user_is_not_a_candidate(self):
        world = IssueWorld([issue(7, author="stranger")], local=["main"])

        step, _ = self.detect(world)

        self.assertEqual([], step.details["candidates"])

    def test_an_issue_without_the_marker_needs_a_matching_branch(self):
        hand_made = issue(9, body="made by hand", author="stranger")
        for local, expected in ((["main"], []),
                                (["main", "9-chore-bootstrap-by-hand"],
                                 self.candidates((9, "9-chore-bootstrap-by-hand")))):
            with self.subTest(local=local):
                step, _ = self.detect(IssueWorld([hand_made], local=local))

                self.assertEqual(expected, step.details["candidates"])

    def test_a_branch_of_a_closed_issue_is_not_a_candidate(self):
        world = IssueWorld([issue(7)], local=["main", "5-chore-bootstrap"])

        step, _ = self.detect(world)

        self.assertEqual(self.candidates((7, None)), step.details["candidates"])

    def test_a_branch_name_with_shell_metacharacters_is_not_a_candidate_branch(self):
        names = ["7-chore-bootstrap;x", "7-chore-bootstrap$(id)", "7-chore-bootstrap`id`",
                 "7-chore-bootstrap&x", "7-chore-bootstrap|x", "7-chore-bootstrap'x"]
        for name in names:
            with self.subTest(name=name):
                world = IssueWorld([issue(7)], local=[name, "main"], remote=[name])

                step, _ = self.detect(world)

                self.assertEqual(self.candidates((7, None)), step.details["candidates"])
                self.assertNotIn(name, step.action["command"] or "")

    def test_a_default_branch_that_needs_quoting_is_printed_quoted(self):
        run = IssueWorld([issue(7)], local=["main"]).run()

        step = bootstrap_steps.detect_bootstrap_issue(
            Path("."), run, REPO, context(default="release;v1"))

        self.assertEqual(
            "gh issue develop 7 --base 'release;v1' --name 7-chore-bootstrap --checkout",
            step.action["command"])

    def test_a_suffixed_branch_is_printed_in_the_fetch_and_checkout_command(self):
        world = IssueWorld([issue(7)], local=["main"], remote=["main", "7-chore-bootstrap-a1"])

        step, _ = self.detect(world)

        self.assertEqual(
            "git fetch origin 7-chore-bootstrap-a1 && git switch 7-chore-bootstrap-a1",
            step.action["command"])

    def test_the_branch_pattern_accepts_only_names_the_pr_conventions_accept(self):
        accepted = ["7-chore-bootstrap", "12-chore-bootstrap", "9-chore-bootstrap-by-hand",
                    "7-chore-bootstrap-a1", "7-chore-bootstrap-a-b-c"]
        rejected = ["012-chore-bootstrap", "12-chore-bootstrap_X", "12-chore-bootstrap-X",
                    "0-chore-bootstrap", "7-chore-bootstrap.x", "7-chore-bootstrap-",
                    "7-chore-bootstrap--a", "7-chore-bootstrap\n", "7-chore-bootstrap;x",
                    "7chore-bootstrap"]
        for name in accepted:
            with self.subTest(accepted=name):
                self.assertTrue(bootstrap_steps.BRANCH_PATTERN.match(name))
                self.assertTrue(pr_conventions.ISSUE_BRANCH_PATTERN.match(name))
        for name in rejected:
            with self.subTest(rejected=name):
                self.assertIsNone(bootstrap_steps.BRANCH_PATTERN.match(name))

    def test_other_branch_names_are_ignored(self):
        world = IssueWorld([issue(7)], local=["7-feature-thing", "7chore-bootstrap", "main"])

        step, _ = self.detect(world)

        self.assertEqual(self.candidates((7, None)), step.details["candidates"])

    def test_the_checked_out_branch_is_preferred_when_an_issue_has_several(self):
        world = IssueWorld([issue(7)], local=["7-chore-bootstrap-a", "7-chore-bootstrap-b"],
                           remote=["7-chore-bootstrap-c"], current="7-chore-bootstrap-b")

        step, _ = self.detect(world)

        self.assertEqual("done", step.state)
        self.assertEqual("7-chore-bootstrap-b", step.details["candidates"][0]["branch"])

    def test_other_uncommitted_files_make_it_not_performable(self):
        world = IssueWorld([issue(7)], local=["main"], status=" M scripts/other.py\0")

        step, _ = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("scripts/other.py", step.title)

    def test_only_bootstrap_files_changed_stays_performable(self):
        status = ("?? project-context.toml\0 M ROADMAP.md\0"
                  "?? .specify/memory/constitution.md\0")

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertTrue(step.performable)

    def test_a_rename_counts_as_another_file(self):
        status = "R  docs/ROADMAP.md\0ROADMAP.md\0"

        step, _ = self.detect(IssueWorld([], local=["main"], status=status))

        self.assertFalse(step.performable)

    def test_untracked_files_outside_the_bootstrap_files_stay_performable_and_are_named(self):
        status = "?? notes/a.md\0?? scratch.txt\0?? ROADMAP.md\0"

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertTrue(step.title.endswith(
            "; untracked files, carried over and not committed: notes/a.md, scratch.txt"),
            step.title)
        self.assertNotIn("other uncommitted", step.title)
        self.assertEqual(1, bootstrap_steps.FORMAT_VERSION)

    def test_a_changed_tracked_file_blocks_with_the_commit_stash_or_revert_text(self):
        status = " M scripts/a.py\0A  docs/b.md\0"

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual("Commit, stash or revert the changes to scripts/a.py, docs/b.md, "
                         "then run start again.", step.action["text"])

    def test_tracked_and_untracked_changes_together_block_and_name_both(self):
        status = " M scripts/a.py\0?? notes/a.md\0"

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertFalse(step.performable)
        self.assertIn("changed tracked files: scripts/a.py", step.title)
        self.assertNotIn("other uncommitted", step.title)
        self.assertIn("untracked files, carried over and not committed: notes/a.md", step.title)
        self.assertIn("scripts/a.py", step.action["text"])
        self.assertNotIn("notes/a.md", step.action["text"])

    def test_an_untracked_constitution_in_a_new_folder_is_a_bootstrap_file(self):
        status = "?? .specify/memory/constitution.md\0"

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertTrue(step.performable)

    def test_a_rename_of_a_bootstrap_file_to_another_path_is_another_file(self):
        status = "R  docs/ROADMAP.md\0ROADMAP.md\0"

        step, _ = self.detect(IssueWorld([issue(7)], local=["main"], status=status))

        self.assertIn("docs/ROADMAP.md", step.title)

    def test_unknown_merge_state_is_detected_locally(self):
        step, _ = self.detect(IssueWorld([issue(7)], local=["7-chore-bootstrap"],
                                         current="7-chore-bootstrap"), merged=None)

        self.assertEqual("done", step.state)

    def test_a_gh_error_is_not_checked_with_the_first_line(self):
        step_run = Scripted(
            (["gh", "api", "user"], gh_error("HTTP 401: Bad credentials\nsecond line")))

        step = bootstrap_steps.detect_bootstrap_issue(Path("."), step_run, REPO, context(False))

        self.assertEqual("not_checked", step.state)
        self.assertIn("HTTP 401: Bad credentials", step.title)
        self.assertIsNotNone(step.action)
        self.assertNotIn("second line", step.title)
        self.assertFalse(step.performable)

    def test_a_branch_listing_error_is_not_checked(self):
        world = IssueWorld([issue(7)], local=["main"])
        world.rules[2] = (["gh", "api", "--paginate"], gh_error("HTTP 500: boom"))

        step, _ = self.detect(world)

        self.assertEqual("not_checked", step.state)

    def test_without_a_known_default_branch_nothing_is_performable(self):
        run = IssueWorld([issue(7)], local=["main"]).run()

        step = bootstrap_steps.detect_bootstrap_issue(
            Path("."), run, REPO, context(None, default=None))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIsNone(step.action["command"])
        self.assertIn("issue #7", step.action["text"])

    def test_it_only_reads(self):
        _, run = self.detect(IssueWorld([issue(7)], local=["main"]))

        for call in run.calls:
            self.assertIn(call[:3], (["gh", "api", "user"], ["gh", "issue", "list"],
                                     ["gh", "api", "--paginate"], ["git", "branch", "--show-current"],
                                     ["git", "branch", "--format=%(refname:short)"],
                                     ["git", "status", "--porcelain"]))


class LocalFilesTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class ProjectContextStepTest(LocalFilesTestCase):
    def detect(self, ctx):
        return bootstrap_steps.detect_project_context(self.root, ctx, None)

    def test_merged_and_valid_on_the_default_branch_is_done_whatever_the_local_file_is(self):
        self.write("project-context.toml", "garbage = [")

        step = self.detect(context(True, text=CONTEXT.format(approvals=1)))

        self.assertEqual(("project_context", "done"), (step.id, step.state))
        self.assertIsNone(step.action)

    def test_merged_but_invalid_on_the_default_branch_names_the_error_and_the_doc(self):
        text = CONTEXT.format(approvals=1).split("[review]")[0]

        step = self.detect(context(True, text=text))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn(project_context.MISSING_REVIEW_MESSAGE.splitlines()[0], step.title)
        self.assertNotIn("\n", step.title)
        self.assertEqual("docs/bootstrap.md#project-contexttoml", step.action["doc"])

    def test_merged_with_text_that_is_not_toml_is_todo(self):
        step = self.detect(context(True, text="not = [toml"))

        self.assertEqual(("todo", False), (step.state, step.performable))

    def test_merged_without_readable_text_is_not_checked_with_the_read_error(self):
        step = self.detect(context(True, text=None, error="HTTP 500: boom"))

        self.assertEqual("not_checked", step.state)
        self.assertIn("HTTP 500: boom", step.title)
        self.assertIsNotNone(step.action)

    def test_not_merged_with_a_valid_local_file_is_done(self):
        self.write("project-context.toml", CONTEXT.format(approvals=1))

        self.assertEqual("done", self.detect(context(False)).state)
        self.assertEqual("done", self.detect(context(None)).state)

    def test_not_merged_without_a_local_file_is_performable(self):
        step = self.detect(context(False))

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertIn("missing", step.title)

    def test_an_invalid_local_file_is_todo_with_the_error_and_not_performable(self):
        self.write("project-context.toml", CONTEXT.format(approvals=1).split("[review]")[0])

        step = self.detect(context(False))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("invalid", step.title)
        self.assertEqual("docs/bootstrap.md#project-contexttoml", step.action["doc"])

    def test_the_local_file_is_ignored_once_merged(self):
        step = self.detect(context(True, text=CONTEXT.format(approvals=1)))

        self.assertEqual("done", step.state)


class ConstitutionStepTest(LocalFilesTestCase):
    PATH = ".specify/memory/constitution.md"

    def test_merged_is_done_and_carries_the_default_branch_approval_count(self):
        step = bootstrap_steps.detect_constitution(
            self.root, context(True, text=CONTEXT.format(approvals=2)))

        self.assertEqual(("done", 2), (step.state, step.details["required_approvals"]))

    def test_missing_names_the_skill_and_the_local_approval_count(self):
        self.write("project-context.toml", CONTEXT.format(approvals=3))

        step = bootstrap_steps.detect_constitution(self.root, context(False))

        self.assertEqual(("todo", False, 3),
                         (step.state, step.performable, step.details["required_approvals"]))
        self.assertIn("speckit-constitution", step.action["text"])
        self.assertIn("(3)", step.action["text"])
        self.assertEqual("docs/bootstrap.md#constitution", step.action["doc"])
        self.assertIn(self.PATH, step.title)

    def test_an_existing_file_is_done(self):
        self.write(self.PATH, "# C\n")
        self.write("project-context.toml", CONTEXT.format(approvals=0))

        step = bootstrap_steps.detect_constitution(self.root, context(None))

        self.assertEqual(("done", 0), (step.state, step.details["required_approvals"]))

    def test_without_a_readable_context_the_approval_count_is_unknown(self):
        step = bootstrap_steps.detect_constitution(self.root, context(False))

        self.assertEqual("todo", step.state)
        self.assertIsNone(step.details["required_approvals"])
        self.assertIn("speckit-constitution", step.action["text"])
        self.assertNotIn("(None)", step.action["text"])


class RoadmapStepTest(LocalFilesTestCase):
    def test_merged_is_done_without_reading_the_local_file(self):
        self.assertEqual("done", bootstrap_steps.detect_roadmap(self.root, context(True)).state)

    def test_missing_is_todo_naming_roadmap_write(self):
        step = bootstrap_steps.detect_roadmap(self.root, context(False))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("roadmap-write", step.action["text"])
        self.assertEqual("docs/bootstrap.md#roadmap", step.action["doc"])

    def test_invalid_is_todo_with_the_first_line_of_the_error(self):
        self.write("ROADMAP.md", "nothing useful\n")

        step = bootstrap_steps.detect_roadmap(self.root, context(None))

        self.assertEqual("todo", step.state)
        self.assertIn("invalid", step.title)
        self.assertNotIn("\n", step.title)

    def test_valid_is_done(self):
        self.write("ROADMAP.md", ROADMAP)

        self.assertEqual("done", bootstrap_steps.detect_roadmap(self.root, context(False)).state)


class SettingsGitHub(FakeGitHub):
    """FakeGitHub plus the repository visibility and a switch that refuses the rulesets GET."""

    def __init__(self, *args, visibility="PUBLIC", ruleset_error=None, visibility_error=False,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.visibility = visibility
        self.ruleset_error = ruleset_error
        self.ruleset_error_once = False
        self.visibility_error = visibility_error

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        if argv == ["gh", "repo", "view", REPO, "--json", "visibility"]:
            self.calls.append(argv)
            if self.visibility_error:
                raise bootstrap_github.CommandError("HTTP 500: boom")
            return json.dumps({"visibility": self.visibility})
        if (self.ruleset_error and argv[:2] == ["gh", "api"] and "/rulesets" in argv[2]
                and "-X" not in argv):
            self.calls.append(argv)
            error = self.ruleset_error
            if self.ruleset_error_once:
                self.ruleset_error = None
            raise bootstrap_github.CommandError(error)
        return super().__call__(argv, cwd)


PLAN_REFUSAL = ("gh: Upgrade to GitHub Pro or make this repository public to enable this "
                "feature. (HTTP 403)")


class RulesetWarningTest(unittest.TestCase):
    def warn(self, **kwargs):
        run = SettingsGitHub(**kwargs)
        return bootstrap_steps.ruleset_warning(REPO, run), run

    def test_a_private_repository_on_a_plan_without_rulesets_gets_the_plan_hint(self):
        warning, _ = self.warn(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)

        self.assertEqual(bootstrap_github.PLAN_HINT, warning)

    def test_a_refusal_for_missing_rights_gets_the_admin_hint(self):
        warning, _ = self.warn(visibility="PRIVATE",
                               ruleset_error="gh: Resource not accessible (HTTP 403)")

        self.assertEqual(bootstrap_github.ADMIN_HINT, warning)

    def test_a_404_or_another_error_could_not_confirm_with_the_first_line(self):
        for error in ("gh: Not Found (HTTP 404)", "HTTP 500: server error\nmore"):
            with self.subTest(error=error):
                warning, _ = self.warn(visibility="PRIVATE", ruleset_error=error)

                self.assertIn("could not confirm that rulesets are available", warning)
                self.assertIn(error.splitlines()[0], warning)
                self.assertNotIn("more", warning)
                self.assertNotEqual(bootstrap_github.ADMIN_HINT, warning)

    def test_a_private_repository_whose_rulesets_are_readable_has_no_warning(self):
        warning, run = self.warn(visibility="PRIVATE")

        self.assertIsNone(warning)
        self.assertEqual(1, len([c for c in run.calls if "/rulesets" in " ".join(c)]))

    def test_a_public_repository_is_not_probed(self):
        warning, run = self.warn(visibility="PUBLIC", ruleset_error=PLAN_REFUSAL)

        self.assertIsNone(warning)
        self.assertEqual([], [c for c in run.calls if "/rulesets" in " ".join(c)])

    def test_unreadable_visibility_gives_no_warning(self):
        warning, _ = self.warn(visibility_error=True, ruleset_error=PLAN_REFUSAL)

        self.assertIsNone(warning)

    def test_the_probe_only_reads(self):
        _, run = self.warn(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)

        self.assertEqual([], run.writes)


class GitHubSettingsTest(LocalFilesTestCase):
    def setUp(self):
        super().setUp()
        self.write("project-context.toml", CONTEXT.format(approvals=1))

    def apply(self, run, approvals=1):
        """Bring the fake's repository in line with approvals, as `github --apply` would."""
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        (Path(other.name) / "project-context.toml").write_text(CONTEXT.format(approvals=approvals))
        with mock.patch("sys.stdout"):
            code = bootstrap.main(["github", "--apply"], root=Path(other.name), run=run,
                                  is_tty=False)
        self.assertEqual(0, code)
        run.writes.clear()

    def detect(self, run, ctx=None):
        return bootstrap_steps.detect_github_settings(
            self.root, run, REPO, ctx or context(False), bootstrap_steps.ruleset_warning(REPO, run))

    def test_the_detector_takes_the_warning_and_does_not_probe_again(self):
        run = SettingsGitHub(visibility="PRIVATE")
        self.apply(run)
        run.calls.clear()

        step = bootstrap_steps.detect_github_settings(
            self.root, run, REPO, context(False), "shared warning")

        self.assertEqual("shared warning", step.details["ruleset_warning"])
        self.assertNotIn(["gh", "repo", "view", REPO, "--json", "visibility"], run.calls)

        step = bootstrap_steps.detect_github_settings(self.root, run, REPO, context(False), None)

        self.assertNotIn("ruleset_warning", step.details)

    def test_drift_is_todo_performable_and_changes_nothing(self):
        run = SettingsGitHub()

        step = self.detect(run)

        self.assertEqual(("github_settings", "todo", True), (step.id, step.state, step.performable))
        self.assertIn("differ", step.title)
        self.assertEqual("python3 scripts/bootstrap.py github", step.action["command"])
        self.assertEqual([], run.writes)

    def test_no_drift_is_done(self):
        run = SettingsGitHub()
        self.apply(run)

        step = self.detect(run)

        self.assertEqual("done", step.state)
        self.assertIsNone(step.action)
        self.assertEqual([], run.writes)

    def test_the_approval_count_comes_from_the_default_branch_once_merged(self):
        run = SettingsGitHub()
        self.apply(run, approvals=2)  # the repository follows the default branch's file
        self.write("project-context.toml", CONTEXT.format(approvals=1))  # stale local copy

        merged = self.detect(run, context(True, text=CONTEXT.format(approvals=2)))
        local = self.detect(run, context(False))

        self.assertEqual("done", merged.state)
        self.assertEqual("todo", local.state)

    def test_a_ruleset_stricter_than_the_context_is_a_decision_not_performable(self):
        run = SettingsGitHub()
        self.apply(run, approvals=2)  # the repository requires more than the local file says

        step = self.detect(run)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("--allow-weaken", step.action["text"])
        self.assertIn("stricter", step.action["text"])
        self.assertEqual("python3 scripts/bootstrap.py github --allow-weaken",
                         step.action["command"])
        self.assertIn("preview", step.action["text"])
        self.assertTrue(step.action["text"].endswith("."))
        self.assertEqual("GitHub settings differ (1 to decide)", step.title)
        self.assertEqual(bootstrap_steps.SETTINGS_DOC, step.action["doc"])

    def test_drift_with_a_decision_counts_both_in_the_title(self):
        plan = bootstrap_github.Plan()
        plan.add("Labels", "create x")
        plan.warnings.append("Warning: stricter; --allow-weaken")

        with mock.patch.object(bootstrap_github, "plan_settings", return_value=(plan, None)):
            step = self.detect(SettingsGitHub())

        self.assertEqual("GitHub settings differ (2 change(s), 1 to decide)", step.title)

    def test_no_drift_title_mentions_fork_approval(self):
        run = SettingsGitHub()
        self.apply(run)

        self.assertIn("fork approval", self.detect(run).title)

    def test_unloadable_context_is_not_checked_with_the_first_line(self):
        self.write("project-context.toml", "[project]\nname = 'x'\n")

        step = self.detect(SettingsGitHub())

        self.assertEqual(("not_checked", False), (step.state, step.performable))
        self.assertIn("missing sections", step.title)
        self.assertIsNotNone(step.action)
        self.assertNotIn("\n", step.title)

    def test_a_missing_local_file_is_not_checked(self):
        (self.root / "project-context.toml").unlink()

        step = self.detect(SettingsGitHub())

        self.assertEqual("not_checked", step.state)
        self.assertEqual(bootstrap_steps.CONTEXT_DOC, step.action["doc"])

    def test_an_invalid_default_branch_file_is_not_checked_even_if_the_local_one_is_fine(self):
        step = self.detect(SettingsGitHub(), context(True, text="[project]\n"))

        self.assertEqual("not_checked", step.state)

    def test_a_gh_error_is_not_checked_with_the_first_line(self):
        step = self.detect(SettingsGitHub(board=False))

        self.assertEqual("not_checked", step.state)
        self.assertIn("no GitHub Project #3 found for acme", step.title)
        self.assertEqual(bootstrap_steps.SETTINGS_DOC, step.action["doc"])

    def test_a_board_owner_other_than_the_repository_owner_is_not_checked(self):
        self.write("project-context.toml",
                   CONTEXT.format(approvals=1).replace('owner = "acme"', 'owner = "other"'))

        step = self.detect(SettingsGitHub())

        self.assertEqual("not_checked", step.state)
        self.assertIn("github_project.owner must match", step.title)
        self.assertIsNotNone(step.action)

    def test_ruleset_warning_lands_in_details_for_a_private_repository_only(self):
        private = SettingsGitHub(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)

        step = self.detect(private)

        self.assertEqual(bootstrap_github.PLAN_HINT, step.details["ruleset_warning"])
        self.assertEqual("not_checked", step.state)
        self.assertEqual(bootstrap_steps.SETTINGS_DOC, step.action["doc"])
        self.assertNotIn("ruleset_warning", self.detect(SettingsGitHub()).details)

    def test_the_warning_is_kept_when_everything_else_is_done(self):
        run = SettingsGitHub(visibility="PRIVATE")
        self.apply(run)
        run.ruleset_error = "HTTP 500: server error"
        run.ruleset_error_once = True  # only the probe fails; the plan reads the rulesets

        step = self.detect(run)

        self.assertEqual("done", step.state)
        self.assertIn("could not confirm", step.details["ruleset_warning"])


class ProjectContextWarningTest(LocalFilesTestCase):
    def test_the_ruleset_warning_is_filled_on_project_context(self):
        run = SettingsGitHub(visibility="PRIVATE", ruleset_error=PLAN_REFUSAL)
        self.write("project-context.toml", CONTEXT.format(approvals=1))

        for ctx in (context(False), context(True, text=CONTEXT.format(approvals=1))):
            with self.subTest(merged=ctx.bootstrap_merged):
                warning = bootstrap_steps.ruleset_warning(REPO, run)
                step = bootstrap_steps.detect_project_context(self.root, ctx, warning)

                self.assertEqual("done", step.state)
                self.assertEqual(bootstrap_github.PLAN_HINT, step.details["ruleset_warning"])

    def test_the_given_warning_is_used_as_is(self):
        self.write("project-context.toml", CONTEXT.format(approvals=1))

        step = bootstrap_steps.detect_project_context(self.root, context(False), "shared")

        self.assertEqual("shared", step.details["ruleset_warning"])

    def test_no_warning_key_for_a_public_repository(self):
        step = bootstrap_steps.detect_project_context(self.root, context(False), None)

        self.assertNotIn("ruleset_warning", step.details)


def secret_world(secrets=(), variables=(), secrets_error=None, org_secrets=None):
    """gh answers for the credential report; values are never part of any answer."""
    return Scripted(
        (["gh", "secret", "list"],
         secrets_error or json.dumps([{"name": name} for name in secrets])),
        (["gh", "variable", "list"], json.dumps([{"name": name} for name in variables])),
        (["gh", "api", f"repos/{REPO}/actions/organization-secrets?per_page=100"],
         json.dumps({"secrets": [{"name": name} for name in org_secrets or ()]})),
        (["gh", "api", f"repos/{REPO}/actions/organization-variables?per_page=100"],
         json.dumps({"variables": []})),
    )


class BoardCredentialTest(unittest.TestCase):
    def detect(self, run):
        return bootstrap_steps.detect_board_credential(REPO, run)

    def test_app_secrets_both_set_is_done(self):
        step = self.detect(secret_world(bootstrap_github.APP_SECRETS))

        self.assertEqual(("board_credential", "done"), (step.id, step.state))
        self.assertIsNone(step.action)
        self.assertEqual(list(bootstrap_github.APP_SECRETS), step.details["secrets"])

    def test_the_token_secret_alone_is_done(self):
        step = self.detect(secret_world([bootstrap_github.TOKEN_SECRET, "UNRELATED"]))

        self.assertEqual("done", step.state)
        self.assertEqual([bootstrap_github.TOKEN_SECRET], step.details["secrets"])

    def test_an_organization_secret_counts(self):
        step = self.detect(secret_world(org_secrets=[bootstrap_github.TOKEN_SECRET]))

        self.assertEqual("done", step.state)

    def test_not_configured_is_optional_with_the_credentials_guide(self):
        step = self.detect(secret_world(["UNRELATED"]))

        self.assertEqual("optional", step.state)
        self.assertEqual(bootstrap_github.CREDENTIALS_GUIDE, step.action["doc"])
        self.assertEqual([], step.details["secrets"])
        self.assertNotIn("UNRELATED", json.dumps(step.details))

    def test_not_configured_gives_the_command_to_set_the_token_secret(self):
        expected = f"gh secret set {bootstrap_github.TOKEN_SECRET} -R acme/widgets"
        for org in (None, ["UNRELATED_ORG"]):
            with self.subTest(organization_secrets=org):
                step = self.detect(secret_world(["UNRELATED"], org_secrets=org))
                self.assertEqual(expected, step.action["command"])
        run = secret_world(["UNRELATED"])
        run.rules[2] = (run.rules[2][0], gh_error("HTTP 403: forbidden"))
        self.assertEqual(expected, self.detect(run).action["command"])

    def test_the_secret_command_quotes_the_repository(self):
        step = bootstrap_steps.detect_board_credential("ac me/widgets", Scripted(
            (["gh", "secret", "list"], "[]"), (["gh", "api"], json.dumps({"secrets": []}))))

        self.assertEqual(f"gh secret set {bootstrap_github.TOKEN_SECRET} -R 'ac me/widgets'",
                         step.action["command"])

    def test_the_title_of_a_not_configured_credential_is_unchanged(self):
        step = self.detect(secret_world(["UNRELATED"]))

        self.assertEqual("Board credential not configured (optional)", step.title)

    def test_an_incomplete_app_keeps_a_null_command(self):
        step = self.detect(secret_world([bootstrap_github.APP_SECRETS[0]]))

        self.assertIsNone(step.action["command"])

    def test_half_an_app_is_optional_and_names_the_missing_part(self):
        step = self.detect(secret_world([bootstrap_github.APP_SECRETS[0]]))

        self.assertEqual("optional", step.state)
        self.assertIn(bootstrap_github.APP_SECRETS[0], step.title)
        self.assertIn(bootstrap_github.APP_SECRETS[1], step.action["text"])

    def test_unreadable_secret_names_are_not_checked_with_an_action(self):
        step = self.detect(secret_world(secrets_error=gh_error("HTTP 403: forbidden")))

        self.assertEqual("not_checked", step.state)
        self.assertIn("HTTP 403: forbidden", step.title)
        self.assertIsNotNone(step.action)

    def test_unread_organization_secrets_are_said_in_the_title_when_nothing_is_set(self):
        run = secret_world(["UNRELATED"])
        run.rules[2] = (run.rules[2][0], gh_error("HTTP 403: forbidden"))

        step = self.detect(run)

        self.assertEqual("optional", step.state)
        self.assertEqual(UNREAD_TITLE, step.title)
        self.assertEqual(1, step.title.count("("))
        step = self.detect(secret_world(["UNRELATED"]))
        self.assertEqual("Board credential not configured (optional)", step.title)

    def test_unread_organization_secrets_are_said_for_half_an_app_too(self):
        run = secret_world([bootstrap_github.APP_SECRETS[0]])
        run.rules[2] = (run.rules[2][0], gh_error("HTTP 403: forbidden"))

        step = self.detect(run)

        self.assertIn("(organization secrets not checked)", step.title)

    def test_it_lists_names_only_and_never_reads_a_value(self):
        run = secret_world(bootstrap_github.APP_SECRETS)

        self.detect(run)

        allowed = (["gh", "secret", "list"], ["gh", "variable", "list"], ["gh", "api"])
        for call in run.calls:
            self.assertIn(call[:3] if call[1] != "api" else call[:2], allowed)
            if call[1] != "api":
                self.assertIn("--json", call)
                self.assertEqual("name", call[call.index("--json") + 1])
        self.assertTrue(run.calls)


WORKFLOW_PR = "Pull request linked to issue"
WORKFLOW_SUB = "Auto-add sub-issues to project"


def workflows_world(states=None, error=None):
    """The board GraphQL answer; `states` maps a workflow name to its enabled flag."""
    states = {WORKFLOW_PR: True, WORKFLOW_SUB: True} if states is None else states
    nodes = [{"name": name, "enabled": enabled} for name, enabled in states.items()]
    answer = error or json.dumps(
        {"data": {"repositoryOwner": {"projectV2": {"workflows": {"nodes": nodes}}}}})
    return Scripted((["gh", "api", "graphql"], answer))


class BoardWorkflowsTest(LocalFilesTestCase):
    def setUp(self):
        super().setUp()
        self.write("project-context.toml", CONTEXT.format(approvals=1))

    def detect(self, run, ctx=None):
        return bootstrap_steps.detect_board_workflows(self.root, run, ctx or context(False))

    def test_both_enabled_is_done_and_reports_both(self):
        step = self.detect(workflows_world())

        self.assertEqual(("board_workflows", "done"), (step.id, step.state))
        self.assertIsNone(step.action)
        self.assertEqual(
            'Board workflow "Pull request linked to issue" is on; check that it sets Status '
            "to In review (the API cannot read it)", step.title)
        self.assertEqual({WORKFLOW_PR: True, WORKFLOW_SUB: True}, step.details["workflows"])

    def test_pull_request_linked_off_is_manual_naming_the_page_and_the_status(self):
        step = self.detect(workflows_world({WORKFLOW_PR: False, WORKFLOW_SUB: True}))

        self.assertEqual("manual", step.state)
        self.assertIn(WORKFLOW_PR, step.title)
        self.assertIn("Workflows", step.action["text"])
        self.assertIn("In review", step.action["text"])
        self.assertIn(WORKFLOW_PR, step.action["text"])
        self.assertIsNone(step.action["command"])
        self.assertEqual("docs/bootstrap.md#github-settings", step.action["doc"])
        self.assertEqual({WORKFLOW_PR: False, WORKFLOW_SUB: True}, step.details["workflows"])

    def test_the_sub_issues_workflow_is_reported_in_details_only(self):
        step = self.detect(workflows_world({WORKFLOW_PR: True, WORKFLOW_SUB: False}))

        self.assertEqual("done", step.state)
        self.assertIsNone(step.action)
        self.assertFalse(step.details["workflows"][WORKFLOW_SUB])

    def test_an_unreadable_board_is_not_checked_and_keeps_the_nulls(self):
        step = self.detect(workflows_world(error=gh_error("HTTP 403: scope")))

        self.assertEqual("not_checked", step.state)
        self.assertEqual({WORKFLOW_PR: None, WORKFLOW_SUB: None}, step.details["workflows"])
        self.assertIsNotNone(step.action)

    def test_a_missing_workflow_name_is_not_checked(self):
        step = self.detect(workflows_world({WORKFLOW_SUB: True}))

        self.assertEqual("not_checked", step.state)
        self.assertEqual({WORKFLOW_PR: None, WORKFLOW_SUB: True}, step.details["workflows"])

    def test_the_board_comes_from_the_default_branch_once_merged(self):
        self.write("project-context.toml", "not a context")
        run = workflows_world()

        step = self.detect(run, context(True, text=CONTEXT.format(approvals=1)))

        self.assertEqual("done", step.state)
        self.assertEqual(1, len(run.ran("gh", "api", "graphql")))

    def test_an_unreadable_context_is_not_checked_with_an_action(self):
        (self.root / "project-context.toml").unlink()
        run = workflows_world()

        step = self.detect(run)

        self.assertEqual("not_checked", step.state)
        self.assertIsNotNone(step.action)
        self.assertEqual([], run.calls)

    def test_it_never_blocks(self):
        for states in ({WORKFLOW_PR: False, WORKFLOW_SUB: False}, {}):
            step = self.detect(workflows_world(states))
            self.assertFalse(step.blocking)


BRANCH = "7-chore-bootstrap"
OID = "oid-pr-head"
TIP = "tip-default"


def pr(number=5, state="OPEN", branch=BRANCH, oid=OID, checks=(), fork=False):
    return {"number": number, "title": "CHORE: bootstrap", "state": state,
            "headRefName": branch, "headRefOid": oid, "statusCheckRollup": list(checks),
            "isCrossRepository": fork}


class PrWorld:
    """A fake git and gh for the pull request steps; every call is recorded and nothing writes.

    `contains` is a set of (ancestor, descendant) pairs; `HEAD` resolves to `head`. A pair not
    listed fails like git does (exit 1), and a commit in `missing` fails like exit 128.
    """

    def __init__(self, prs=(), current=BRANCH, local=(BRANCH, "main"), status="", head="head1",
                 contains=(), missing=(), pushed=None, list_error=None, tip_exists=True,
                 tip_error=None, view_error=None):
        self.prs = list(prs)
        self.current = current
        self.local = list(local)
        self.status = status
        self.head = head
        self.contains = set(contains)
        self.missing = set(missing)
        self.pushed = pushed or {}  # branch -> its tip on GitHub; absent means a 404
        self.list_error = list_error
        self.tip_exists = tip_exists
        self.tip_error = tip_error  # the branch lookup fails with this instead of a 404
        self.view_error = view_error
        self.calls = []

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            if self.list_error:
                raise gh_error(self.list_error)
            return json.dumps([{k: v for k, v in item.items() if k != "statusCheckRollup"}
                               for item in self.prs])
        if argv[:3] == ["gh", "pr", "view"]:
            if self.view_error:
                raise gh_error(self.view_error)
            item = next(item for item in self.prs if str(item["number"]) == argv[3])
            return json.dumps({key: item[key] for key in ("headRefOid", "statusCheckRollup")})
        if argv[:3] == ["git", "branch", "--show-current"]:
            return f"{self.current}\n"
        if argv[:2] == ["git", "branch"]:
            return "".join(f"{name}\n" for name in self.local)
        if argv[:2] == ["git", "status"]:
            return self.status
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return f"{self.head}\n"
        if argv[:3] == ["git", "cat-file", "-e"]:
            if not self.tip_exists:
                raise gh_error("fatal: Not a valid object name")
            return ""
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            ancestor, descendant = argv[3:5]
            descendant = self.head if descendant == "HEAD" else descendant
            if ancestor in self.missing or descendant in self.missing:
                raise gh_error("fatal: Not a valid commit name", returncode=128)
            if ancestor == descendant or (ancestor, descendant) in self.contains:
                return ""
            raise gh_error("")
        if argv[:2] == ["gh", "api"] and "/branches/" in argv[2]:
            branch = argv[2].partition("/branches/")[2]
            if self.tip_error:
                raise gh_error(self.tip_error)
            if branch not in self.pushed:
                raise gh_error("gh: Not Found (HTTP 404)")
            return f"{self.pushed[branch]}\n"
        raise AssertionError(f"unexpected command {argv}")


def own_change(path="ROADMAP.md"):
    return f" M {path}\0"


class PullRequestsReadTest(unittest.TestCase):
    def read(self, world):
        return bootstrap_steps.read_pull_requests(Path("."), world, REPO)

    def test_an_open_pull_request_without_a_number_is_an_unreadable_report(self):
        item = pr(5, "OPEN")
        del item["number"]

        found = self.read(PrWorld([item]))

        self.assertIsNone(found.open)
        self.assertIn("could not be read", found.error)

    def test_only_bootstrap_branches_count_and_the_open_one_wins(self):
        world = PrWorld([pr(9, "OPEN", "feature-x"), pr(6, "CLOSED"), pr(5, "OPEN"),
                         pr(3, "MERGED")])

        found = self.read(world)

        self.assertEqual(5, found.open["number"])
        self.assertEqual(6, found.closed["number"])
        self.assertEqual(3, found.merged["number"])
        self.assertIsNone(found.error)

    def test_the_newest_merged_pr_is_the_one_named(self):
        world = PrWorld([pr(3, "MERGED"), pr(7, "MERGED"), pr(4, "MERGED")])

        self.assertEqual(7, self.read(world).merged["number"])

    def test_the_listing_asks_for_the_fork_flag_but_not_for_the_checks(self):
        world = PrWorld([pr()])

        self.read(world)

        (call,) = world.ran("gh", "pr", "list")
        fields = call[call.index("--json") + 1].split(",")
        self.assertIn("isCrossRepository", fields)
        self.assertNotIn("statusCheckRollup", fields)

    def test_only_the_chosen_open_pr_is_viewed_for_its_head_and_checks(self):
        checks = [{"name": "lint", "conclusion": "FAILURE"}]
        world = PrWorld([pr(9, "OPEN", "feature-x"), pr(6, "CLOSED"), pr(5, checks=checks),
                         pr(3, "MERGED")])

        found = self.read(world)

        (call,) = world.ran("gh", "pr", "view")
        self.assertEqual("5", call[3])
        self.assertEqual("headRefOid,statusCheckRollup", call[call.index("--json") + 1])
        self.assertEqual(checks, found.open["statusCheckRollup"])
        self.assertEqual(OID, found.open["headRefOid"])

    def test_without_an_open_pr_nothing_is_viewed(self):
        world = PrWorld([pr(6, "CLOSED")])

        self.read(world)

        self.assertEqual([], world.ran("gh", "pr", "view"))

    def test_a_view_error_keeps_its_first_line(self):
        found = self.read(PrWorld([pr()], view_error="HTTP 502: bad gateway\nmore"))

        self.assertEqual("HTTP 502: bad gateway", found.error)

    def test_the_checked_out_branch_is_read_once(self):
        world = PrWorld([pr()])

        found = bootstrap_steps.read_pull_requests(Path("."), world, REPO)
        bootstrap_steps.detect_pull_request(
            Path("."), world, REPO, context(False, tip=TIP), found)

        self.assertEqual(BRANCH, found.current)
        self.assertEqual(1, len(world.ran("git", "branch", "--show-current")))

    def test_a_fork_pull_request_on_a_bootstrap_branch_is_ignored(self):
        world = PrWorld([pr(8, "OPEN", fork=True), pr(7, "CLOSED", fork=True),
                         pr(6, "MERGED", fork=True)])

        found = self.read(world)

        self.assertEqual((None, None, None), (found.open, found.closed, found.merged))

    def test_a_fork_pull_request_is_not_the_bootstrap_pull_request_or_merge(self):
        world = PrWorld([pr(8, "OPEN", fork=True)], contains={(TIP, "head1")})
        found = self.read(world)

        step = bootstrap_steps.detect_pull_request(
            Path("."), world, REPO, context(False, tip=TIP), found)
        merge = bootstrap_steps.detect_merge(context(False, tip=TIP), found)

        self.assertNotIn("#8", step.title)
        self.assertEqual("todo", step.state)
        self.assertEqual("todo", merge.state)
        self.assertNotIn("#8", merge.title)

    def test_the_checked_out_branch_is_preferred_among_open_ones(self):
        other = "8-chore-bootstrap"
        world = PrWorld([pr(5, "OPEN", other), pr(4, "OPEN", BRANCH)], current=BRANCH)

        self.assertEqual(4, self.read(world).open["number"])

    def test_a_listing_error_keeps_its_first_line(self):
        found = self.read(PrWorld(list_error="HTTP 502: bad gateway\nmore"))

        self.assertEqual("HTTP 502: bad gateway", found.error)
        self.assertIsNone(found.open)

    def test_it_only_reads(self):
        world = PrWorld([pr()])

        self.read(world)

        for call in world.calls:
            self.assertIn(call[:3], (["gh", "pr", "list"], ["gh", "pr", "view"],
                                     ["git", "branch", "--show-current"]))


def origin_reads(fetch=("acme", "widgets"), pushes=None):
    """The origin facts `detect_*` and `start` both read: the fetch URL and the push URLs."""
    return bootstrap_steps.OriginReads(
        lambda: fetch, None if pushes is None else (lambda: pushes))


# (fetch, push URLs) pairs: the first four are fine for `gh` on acme/widgets, the rest stop
ORIGIN_CASES = (
    (("acme", "widgets"), None, False),
    (("Acme", "Widgets"), [("acme", "widgets")], False),
    (("acme", "widgets"), [("acme", "widgets"), ("ACME", "WIDGETS")], False),
    (("acme", "widgets"), [("acme", "widgets")], False),
    (("other", "fork"), None, True),
    (None, None, True),
    (("acme", "widgets"), [("other", "fork")], True),
    (("acme", "widgets"), [("acme", "widgets"), ("other", "fork")], True),
    (("acme", "widgets"), [None], True),
    (("acme", "widgets"), [("acme", "widgets"), None], True),
)


class OriginAgreementTest(unittest.TestCase):
    """`check --json` must say `performable: false` exactly when `start --confirm` would stop on
    `origin` (research D4), with the same title and command: both run `OriginReads.stop`."""

    def start_stop(self, fetch, pushes, push, mismatch):
        env = bootstrap_start.Env(
            root=Path("."), remote_default=lambda: fetch,
            push_remote_default=None if pushes is None else (lambda: pushes),
            run=lambda argv, cwd=None: "", _repo=REPO)
        return bootstrap_start.remote_mismatch(env, mismatch, push=push)

    def test_a_bootstrap_issue_that_start_would_stop_on_is_not_performable(self):
        for fetch, pushes, stops in ORIGIN_CASES:
            with self.subTest(fetch=fetch, pushes=pushes):
                world = IssueWorld([issue(7)], local=["main"])

                step = bootstrap_steps.detect_bootstrap_issue(
                    Path("."), world.run(), REPO, context(), origin=origin_reads(fetch, pushes))
                reason = self.start_stop(fetch, pushes, False, bootstrap_steps.MISMATCH_ISSUE)

                self.assertEqual(reason is None, step.performable)
                if reason:
                    self.assertEqual(reason["command"], step.action["command"])
                    self.assertEqual(reason["text"], step.action["text"])
                    self.assertIn(reason["title"], step.title)

    def test_a_pull_request_that_start_would_stop_on_is_not_performable(self):
        for fetch, pushes, stops in ORIGIN_CASES:
            with self.subTest(fetch=fetch, pushes=pushes):
                world = PrWorld([pr()], head=OID, status=own_change(), contains={(TIP, OID)})
                found = bootstrap_steps.read_pull_requests(Path("."), world, REPO)

                step = bootstrap_steps.detect_pull_request(
                    Path("."), world, REPO, context(False, tip=TIP), found,
                    origin=origin_reads(fetch, pushes))
                reason = self.start_stop(fetch, pushes, True, bootstrap_steps.MISMATCH_PR)

                self.assertEqual(stops, reason is not None)
                self.assertEqual(reason is None, step.performable)
                if reason:
                    self.assertEqual(reason["command"], step.action["command"])
                    self.assertEqual(reason["text"], step.action["text"])
                    self.assertIn(reason["title"], step.title)

    def test_the_origin_is_not_read_when_nothing_would_be_done(self):
        reads = []
        origin = bootstrap_steps.OriginReads(lambda: reads.append("fetch") or ("other", "fork"))
        world = PrWorld([pr()], head=OID, contains={(TIP, OID)})  # up to date: done
        found = bootstrap_steps.read_pull_requests(Path("."), world, REPO)

        step = bootstrap_steps.detect_pull_request(
            Path("."), world, REPO, context(False, tip=TIP), found, origin=origin)

        self.assertEqual("done", step.state)
        self.assertEqual([], reads)

    def test_the_report_hands_the_origin_reads_to_both_steps(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        issue_step = mock.Mock(wraps=bootstrap_steps.detect_bootstrap_issue)
        pr_step = mock.Mock(wraps=bootstrap_steps.detect_pull_request)
        with mock.patch.object(bootstrap_steps, "detect_bootstrap_issue", issue_step), \
                mock.patch.object(bootstrap_steps, "detect_pull_request", pr_step):
            bootstrap_steps.build_report(
                Path(directory.name), SetUpRepositoryRun(), lambda: ("other", "fork"),
                push_remote_default=lambda: [("other", "fork")])

        for called in (issue_step, pr_step):
            origin = called.call_args.kwargs["origin"]
            self.assertIsInstance(origin, bootstrap_steps.OriginReads)
            self.assertEqual(("other", "fork"), origin.fetch())
        self.assertEqual([("other", "fork")], pr_step.call_args.kwargs["origin"].pushes())


class PullRequestStepTest(unittest.TestCase):
    def detect(self, world, ctx=None, prs=None):
        ctx = ctx or context(False, tip=TIP)
        found = prs or bootstrap_steps.read_pull_requests(Path("."), world, REPO)
        return bootstrap_steps.detect_pull_request(Path("."), world, REPO, ctx, found)

    def current_world(self, **kwargs):
        """The branch is checked out and HEAD is the PR's head commit, level with the default."""
        kwargs.setdefault("head", OID)
        kwargs.setdefault("contains", {(TIP, OID)})
        return PrWorld([pr()], **kwargs)

    def test_merged_bootstrap_is_done_without_any_call(self):
        world = PrWorld([pr()])
        found = bootstrap_steps.PullRequests()

        step = bootstrap_steps.detect_pull_request(
            Path("."), world, REPO, context(True), found)

        self.assertEqual(("pull_request", "done"), (step.id, step.state))
        self.assertEqual([], world.calls)

    def test_open_pr_at_head_with_nothing_uncommitted_is_done(self):
        step = self.detect(self.current_world())

        self.assertEqual("done", step.state)
        self.assertEqual({"number": 5, "branch": BRANCH}, step.details["pull_request"])
        self.assertIsNone(step.action)

    def test_other_uncommitted_files_do_not_matter_for_done(self):
        step = self.detect(self.current_world(status=own_change("notes.txt")))

        self.assertEqual("done", step.state)

    # (1) the current branch is not the PR's head branch
    def test_wrong_branch_with_no_local_work_to_leave_out_is_done(self):
        for local in (["main"], ["main", BRANCH]):
            with self.subTest(local=local):
                world = PrWorld([pr()], current="main", local=local,
                                contains={(BRANCH, OID)})
                step = self.detect(world)
                self.assertEqual("done", step.state)

    def test_wrong_branch_with_a_local_ref_not_in_the_pr_head_is_todo_checkout(self):
        world = PrWorld([pr()], current="main", local=["main", BRANCH])

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual(f"git switch {BRANCH}", step.action["command"])

    def test_a_pr_head_missing_locally_is_todo_checkout_and_never_compares_with_origin(self):
        world = PrWorld([pr()], current="main", local=["main", BRANCH], missing={OID},
                        contains={(BRANCH, f"origin/{BRANCH}")})

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertNotIn("has local work", step.title)
        self.assertEqual(f"git switch {BRANCH}", step.action["command"])
        for call in world.ran("git", "merge-base"):
            self.assertNotIn(f"origin/{BRANCH}", call)

    def test_wrong_branch_with_uncommitted_bootstrap_files_is_todo_checkout(self):
        world = PrWorld([pr()], current="main", local=["main"], status=own_change())

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual(f"git switch {BRANCH}", step.action["command"])

    # (2) HEAD does not contain the PR's head commit
    def test_head_without_the_pr_head_commit_is_todo_pull_rebase(self):
        world = PrWorld([pr()], head="local-only")

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual(f"git pull --rebase origin {BRANCH}", step.action["command"])

    def test_a_head_commit_missing_locally_counts_as_not_contained(self):
        world = PrWorld([pr()], head="local-only", missing={OID})

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual(f"git pull --rebase origin {BRANCH}", step.action["command"])

    def test_not_containing_the_head_commit_wins_over_uncommitted_changes(self):
        world = PrWorld([pr()], head="local-only", status=own_change())

        step = self.detect(world)

        self.assertFalse(step.performable)
        self.assertEqual(f"git pull --rebase origin {BRANCH}", step.action["command"])

    # (3) uncommitted changes, or HEAD ahead
    def test_uncommitted_bootstrap_files_are_todo_and_performable(self):
        step = self.detect(self.current_world(status=own_change()))

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertIn("ROADMAP.md", step.title)

    def test_head_ahead_of_the_pr_head_commit_is_todo_and_performable(self):
        world = PrWorld([pr()], head="ahead1", contains={(OID, "ahead1"), (TIP, "ahead1")})

        step = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))

    def test_behind_the_default_branch_is_not_performable_and_names_the_pull(self):
        world = PrWorld([pr()], head=OID, status=own_change())  # TIP not contained in HEAD

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual("git pull origin main", step.action["command"])

    def test_a_default_tip_missing_locally_counts_as_behind_and_says_fetch(self):
        world = PrWorld([pr()], head=OID, status=own_change(), missing={TIP}, tip_exists=False)

        step = self.detect(world)

        self.assertFalse(step.performable)
        self.assertEqual("git fetch origin && git pull origin main", step.action["command"])

    def test_an_unreadable_default_tip_is_not_performable(self):
        world = PrWorld([pr()], head=OID, status=own_change())

        step = self.detect(world, context(False, tip=None))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("could not be read", step.title)
        self.assertEqual(bootstrap_steps.RERUN, step.action["command"])
        for call in world.calls:  # no git call may be built from the missing tip
            self.assertNotIn(None, call)
            self.assertFalse(any("None" in part for part in call), call)

    def test_an_unreadable_default_branch_is_not_performable(self):
        world = PrWorld([pr()], head=OID, status=own_change())

        step = self.detect(world, context(False, default=None, tip=TIP))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("could not be read", step.title)
        for call in world.calls:
            self.assertFalse(any("None" in part for part in call), call)

    def test_other_uncommitted_files_are_listed_but_do_not_block(self):
        status = own_change() + own_change("notes.txt")
        step = self.detect(self.current_world(status=status))

        self.assertTrue(step.performable)
        self.assertIn("notes.txt", step.title)

    # no open PR
    def test_no_pr_and_a_branch_not_pushed_is_todo_and_performable(self):
        world = PrWorld([], contains={(TIP, "head1")})

        step = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertEqual(1, len(world.ran("gh", "api")))

    def test_only_a_404_on_the_branch_lookup_means_not_pushed(self):
        world = PrWorld([], contains={(TIP, "head1")}, tip_error="HTTP 502: bad gateway\nmore")

        step = self.detect(world)

        self.assertEqual(("pull_request", "not_checked", False),
                         (step.id, step.state, step.performable))
        self.assertIn("HTTP 502: bad gateway", step.title)
        self.assertNotIn("more", step.title)
        self.assertIsNotNone(step.action)

    def test_no_pr_with_a_pushed_tip_that_head_contains_is_performable(self):
        world = PrWorld([], pushed={BRANCH: "pushed1"},
                        contains={("pushed1", "head1"), (TIP, "head1")})

        self.assertTrue(self.detect(world).performable)

    def test_no_pr_with_a_pushed_tip_head_lacks_is_pull_rebase(self):
        world = PrWorld([], pushed={BRANCH: "pushed1"}, contains={(TIP, "head1")})

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual(f"git pull --rebase origin {BRANCH}", step.action["command"])

    def test_no_pr_and_nothing_committed_beyond_the_default_branch_is_not_performable(self):
        world = PrWorld([], head=TIP, contains={(TIP, TIP)})

        step = self.detect(world)

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIn("Commit the bootstrap files first", step.action["text"])
        self.assertTrue(step.action["doc"])

    def test_no_pr_with_uncommitted_bootstrap_files_at_the_default_tip_is_performable(self):
        world = PrWorld([], head=TIP, status=own_change())

        self.assertTrue(self.detect(world).performable)

    def test_no_pr_behind_the_default_branch_is_not_performable(self):
        step = self.detect(PrWorld([]))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual("git pull origin main", step.action["command"])

    def test_no_pr_off_the_bootstrap_branch_is_not_performable(self):
        step = self.detect(PrWorld([], current="main"))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIsNotNone(step.action)

    def test_a_closed_pr_is_named_and_a_new_one_is_offered(self):
        world = PrWorld([pr(4, "CLOSED")], contains={(TIP, "head1")})

        step = self.detect(world)

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertIn("PR #4 was closed without merging", step.title)
        self.assertEqual({"number": 4, "state": "closed"}, step.details["earlier_pull_request"])

    def test_a_merged_pr_whose_files_are_not_on_the_default_branch_is_named(self):
        world = PrWorld([pr(6, "MERGED")], contains={(TIP, "head1")})

        step = self.detect(world)

        self.assertEqual("todo", step.state)
        self.assertIn("#6", step.title)
        self.assertEqual({"number": 6, "state": "merged"}, step.details["earlier_pull_request"])

    def test_an_unknown_merge_state_names_no_merged_pr_but_still_a_closed_one(self):
        unknown = context(None, tip=TIP)
        world = PrWorld([pr(6, "MERGED")], contains={(TIP, "head1")})

        step = self.detect(world, unknown)

        self.assertNotIn("#6", step.title)
        self.assertNotIn("earlier_pull_request", step.details)
        world = PrWorld([pr(6, "MERGED"), pr(4, "CLOSED")], contains={(TIP, "head1")})

        step = self.detect(world, unknown)

        self.assertEqual({"number": 4, "state": "closed"}, step.details["earlier_pull_request"])

    def test_an_unreadable_listing_is_not_checked_with_an_action(self):
        step = self.detect(PrWorld(list_error="HTTP 502: bad gateway"))

        self.assertEqual("not_checked", step.state)
        self.assertIn("HTTP 502: bad gateway", step.title)
        self.assertIsNotNone(step.action)

    def test_it_only_reads(self):
        world = PrWorld([], contains={(TIP, "head1")}, status=own_change())

        self.detect(world)

        for call in world.calls:
            self.assertIn(call[:3], (["gh", "pr", "list"], ["gh", "pr", "view"],
                                     ["git", "branch", "--show-current"],
                                     ["git", "branch", "--format=%(refname:short)"],
                                     ["git", "status", "--porcelain"], ["git", "rev-parse", "HEAD"],
                                     ["git", "merge-base", "--is-ancestor"],
                                     ["git", "cat-file", "-e"]) + (["gh", "api", call[2]],))


class MergeStepTest(unittest.TestCase):
    def detect(self, prs, ctx=None):
        return bootstrap_steps.detect_merge(ctx or context(False), prs)

    def found(self, *items, error=None):
        return bootstrap_steps.read_pull_requests(Path("."), PrWorld(items, list_error=error),
                                                  REPO)

    def test_merged_bootstrap_is_done(self):
        step = self.detect(bootstrap_steps.PullRequests(), context(True))

        self.assertEqual(("merge", "done"), (step.id, step.state))

    def test_an_open_pr_is_waiting_with_the_failing_checks(self):
        checks = [{"name": "lint", "conclusion": "FAILURE", "status": "COMPLETED"},
                  {"name": "tests", "conclusion": "SUCCESS", "status": "COMPLETED"},
                  {"name": "roadmap", "conclusion": "", "status": "IN_PROGRESS"},
                  {"context": "legacy", "state": "ERROR"}]

        step = self.detect(self.found(pr(checks=checks)))

        self.assertEqual("waiting", step.state)
        self.assertIn("#5", step.title)
        self.assertEqual(["lint", "legacy"], step.details["failing_checks"])
        self.assertEqual(5, step.details["pull_request"]["number"])
        self.assertIsNotNone(step.action)

    def test_a_waiting_merge_gives_the_command_to_update_the_default_branch(self):
        step = self.detect(self.found(pr()), context(False, default="main"))

        self.assertEqual("Review and merge PR #5 on GitHub, then run the command below.",
                         step.action["text"])
        self.assertEqual("git switch main && git pull --ff-only origin main",
                         step.action["command"])

    def test_the_merge_command_quotes_the_default_branch(self):
        step = self.detect(self.found(pr()), context(False, default="re lease"))

        self.assertEqual("git switch 're lease' && git pull --ff-only origin 're lease'",
                         step.action["command"])

    def test_a_default_branch_with_an_unsafe_character_gives_no_merge_command(self):
        step = self.detect(self.found(pr()), context(False, default=f"rel{BIDI}ease"))

        self.assertIsNone(step.action["command"])
        self.assertIn("cannot be shown safely", step.action["text"])

    def test_an_unknown_default_branch_gives_no_merge_command(self):
        step = self.detect(self.found(pr()), context(False, default=None))

        self.assertEqual("waiting", step.state)
        self.assertIsNone(step.action["command"])
        self.assertEqual("Review and merge PR #5 on GitHub.", step.action["text"])

    def test_failing_check_names_are_listed_once(self):
        checks = [{"name": "lint", "conclusion": "FAILURE"},
                  {"name": "lint", "conclusion": "TIMED_OUT"},
                  {"context": "lint", "state": "ERROR"}]

        step = self.detect(self.found(pr(checks=checks)))

        self.assertEqual(["lint"], step.details["failing_checks"])

    def test_a_cancelled_run_is_ignored_when_a_run_of_the_same_name_succeeded(self):
        checks = [{"name": "tests", "conclusion": "CANCELLED"},
                  {"name": "tests", "conclusion": "SUCCESS"},
                  {"name": "lint", "conclusion": "CANCELLED"},
                  {"name": "docs", "conclusion": "FAILURE"},
                  {"name": "docs", "conclusion": "SUCCESS"},
                  {"name": "build", "conclusion": "SUCCESS"},
                  {"name": "build", "conclusion": "CANCELLED"}]  # cancelled after the success

        step = self.detect(self.found(pr(checks=checks)))

        self.assertEqual(["lint", "docs", "build"], step.details["failing_checks"])

    def test_an_open_pr_without_failures_lists_none(self):
        step = self.detect(self.found(pr()))

        self.assertEqual([], step.details["failing_checks"])

    def test_no_pr_is_todo_pointing_at_the_pull_request_step(self):
        step = self.detect(self.found())

        self.assertEqual("todo", step.state)
        self.assertIn("pull_request", step.action["text"])

    def test_a_closed_pr_is_todo_pointing_at_the_pull_request_step(self):
        step = self.detect(self.found(pr(4, "CLOSED")))

        self.assertEqual("todo", step.state)
        self.assertIn("pull_request", step.action["text"])

    def test_an_unknown_merge_state_does_not_claim_a_merged_pr(self):
        step = self.detect(self.found(pr(6, "MERGED")), context(None))

        self.assertEqual("todo", step.state)
        self.assertEqual("There is no open bootstrap pull request to merge", step.title)

    def test_merged_without_the_files_says_to_check_what_it_contained(self):
        step = self.detect(self.found(pr(6, "MERGED")))

        self.assertEqual("todo", step.state)
        self.assertEqual("PR #6 was merged but the bootstrap files are not on main; check what "
                         "it contained", step.title)

    def test_an_unreadable_listing_is_not_checked_with_an_action(self):
        step = self.detect(self.found(error="HTTP 502: bad gateway"))

        self.assertEqual("not_checked", step.state)
        self.assertIn("HTTP 502: bad gateway", step.title)
        self.assertIsNotNone(step.action)


ROADMAP_HEAD = "# Roadmap\n\n## Planned Specs\n\n"
RAW_ROADMAP = ["gh", "api", "-H", "Accept: application/vnd.github.raw"]
SYNC_DRY_RUN = ["python3", "scripts/sync-roadmap-issues.py"]
# the exact call sync-task-issues.py's load_issues makes, reused by the first_sync step
LOAD_ISSUES = [
    "gh", "api", "--paginate", f"repos/{REPO}/issues?state=all&per_page=100", "--jq",
    ".[] | select(.pull_request == null) | "
    "{number, id, node_id, title, body, parent, state, state_reason, "
    "author: .user.login, author_association, "
    "assignees: [.assignees[]?.login], labels: [.labels[]?.name]}",
]


def entry_text(roadmap_id, title, status="planned", spec_dir=""):
    return (f"### {roadmap_id} — {title}  [status: {status}]\n\n"
            f"- **Description:** D.\n- **Outcome:** O.\n- **Scope (in):** S.\n"
            f"- **Depends on:** none\n- **Spec dir:** {spec_dir}\n- **Notes:** N.\n\n")


def roadmap_text(*entries):
    return ROADMAP_HEAD + "".join(entries)


def marked(number, marker, author="tester", association="OWNER", labels=()):
    return {"number": number, "body": marker, "author": author, "state": "open",
            "author_association": association, "labels": list(labels)}


def issues_for(roadmap_id, path, epic=True, planning=True, phase1=True, stable_phase=True):
    found, number = [], int(roadmap_id) * 10
    if epic:
        found.append(marked(number, roadmap_issues.roadmap_marker(roadmap_id)))
    if planning:
        found.append(marked(number + 1, roadmap_issues.planning_marker(roadmap_id, "spec-plan")))
    if phase1:
        marker = (roadmap_issues.roadmap_phase_marker(roadmap_id, "phase-1") if stable_phase
                  else roadmap_issues.phase_marker(path, "phase-1"))
        found.append(marked(number + 2, marker))
    return found


class FirstSyncTest(unittest.TestCase):
    ONE = roadmap_text(entry_text("001", "Alpha"))

    def world(self, roadmap=None, head=TIP, dirty="", dry="[dry-run] one\n[dry-run] two\n",
              roadmap_error=None):
        text = self.ONE if roadmap is None else roadmap
        return Scripted(
            (RAW_ROADMAP, roadmap_error or text),
            (["git", "rev-parse", "HEAD"], f"{head}\n"),
            (["git", "status", "--porcelain"], dirty),
            (SYNC_DRY_RUN, dry),
        )

    def detect(self, run, issues=(), ctx=None, skip_project=False, load_error=None):
        """Answers the sync's real issue listing through `run`; nothing is patched."""
        listing = load_error or "".join(json.dumps(issue) + "\n" for issue in issues)
        run.rules.insert(0, (LOAD_ISSUES, listing))
        original = project_board.sync.gh
        step = bootstrap_steps.detect_first_sync(
            Path("."), run, REPO, ctx or context(True, tip=TIP), skip_project)
        self.assertIs(original, project_board.sync.gh)  # the adapter is always removed
        return step

    def everything(self):
        return issues_for("001", "specs/001-alpha")

    def test_every_planned_entry_with_its_three_issues_is_done(self):
        run = self.world()

        step = self.detect(run, self.everything())

        self.assertEqual(("first_sync", "done"), (step.id, step.state))
        self.assertIsNone(step.action)
        self.assertFalse(step.performable)
        self.assertEqual([LOAD_ISSUES], [c for c in run.calls if c[:3] == LOAD_ISSUES[:3]])

    def test_the_phase_one_issue_is_found_by_the_path_marker_too(self):
        found = issues_for("001", "specs/001-alpha", stable_phase=False)

        self.assertEqual("done", self.detect(self.world(), found).state)

    def test_the_roadmap_spec_dir_names_the_path_marker(self):
        roadmap = roadmap_text(entry_text("001", "Alpha", spec_dir="specs/001-custom"))
        found = issues_for("001", "specs/001-custom", stable_phase=False)

        self.assertEqual("done", self.detect(self.world(roadmap), found).state)

    def test_no_planned_entries_is_done_with_a_note(self):
        roadmap = roadmap_text(entry_text("001", "Alpha", status="deferred"))

        step = self.detect(self.world(roadmap))

        self.assertEqual("done", step.state)
        self.assertIn("nothing to sync yet", step.title)

    def test_each_missing_issue_makes_it_todo_and_is_named(self):
        for missing, label in (({"epic": False}, "Epic"), ({"planning": False}, "planning"),
                               ({"phase1": False}, "Phase 1")):
            with self.subTest(missing=label):
                found = issues_for("001", "specs/001-alpha", **missing)
                step = self.detect(self.world(), found)
                self.assertEqual("todo", step.state)
                self.assertIn("001", step.title)
                self.assertIn(label, step.title)

    def test_a_newly_planned_entry_reopens_a_finished_step(self):
        roadmap = roadmap_text(entry_text("001", "Alpha"), entry_text("002", "Beta"))

        step = self.detect(self.world(roadmap), self.everything())

        self.assertEqual("todo", step.state)
        self.assertIn("002", step.title)
        self.assertNotIn("001", step.title)

    def test_an_untrusted_marker_copy_does_not_count(self):
        found = [marked(1, roadmap_issues.roadmap_marker("001"), author="stranger",
                        association="NONE")] + issues_for("001", "specs/001-alpha", epic=False)

        step = self.detect(self.world(), found)

        self.assertEqual("todo", step.state)
        self.assertIn("Epic", step.title)

    def test_a_todo_on_the_clean_tip_with_a_good_dry_run_is_performable(self):
        step = self.detect(self.world(), [])

        self.assertEqual(("todo", True), (step.state, step.performable))
        self.assertIsNotNone(step.action)
        self.assertEqual(2, step.details["pending_changes"])

    def test_pending_changes_count_dry_run_lines_and_never_change_the_state(self):
        step = self.detect(self.world(dry="[dry-run] a\nnoise\n[dry-run] b\n[dry-run] c\n"),
                           self.everything())

        self.assertEqual(("done", 3), (step.state, step.details["pending_changes"]))

    def test_a_head_that_is_not_the_tip_skips_the_dry_run_and_names_the_fix(self):
        run = self.world(head="other")

        step = self.detect(run, [])

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIsNone(step.details["pending_changes"])
        self.assertEqual("git switch main && git pull --ff-only origin main",
                         step.action["command"])
        self.assertEqual([], run.ran(*SYNC_DRY_RUN))

    def test_a_dirty_roadmap_or_specs_skips_the_dry_run_and_names_the_paths(self):
        run = self.world(dirty=" M ROADMAP.md\0?? specs/001-alpha/spec.md\0"
                               "R  scripts/new.py\0scripts/old.py\0")

        step = self.detect(run, [])

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIsNone(step.details["pending_changes"])
        self.assertIn("ROADMAP.md", step.action["text"])
        self.assertIn("specs/001-alpha/spec.md", step.action["text"])
        self.assertIn("scripts/new.py, scripts/old.py", step.action["text"])
        self.assertEqual([], run.ran(*SYNC_DRY_RUN))

    ORIGIN_READ = ["git", "config", "--get", "remote.origin.url"]

    def origin_world(self, url, **kwargs):
        run = self.world(**kwargs)
        run.rules.insert(0, (self.ORIGIN_READ, url + "\n"))
        return run

    def detect_checked(self, run, issues=()):
        listing = "".join(json.dumps(issue) + "\n" for issue in issues)
        run.rules.insert(0, (LOAD_ISSUES, listing))
        return bootstrap_steps.detect_first_sync(
            Path("."), run, REPO, context(True, tip=TIP), False, check_origin=True)

    def test_an_origin_start_would_stop_on_is_not_performable_with_the_same_stop(self):
        env = bootstrap_start.Env(root=Path("."), remote_default=lambda: None, _repo=REPO,
                                  run=lambda argv, cwd=None: "")
        for url, stops in (("https://github.com/acme/widgets.git", False),
                           ("git@github.com:ACME/widgets.git", False),
                           ("https://github.com/other/fork.git", True),
                           ("git@github-work:acme/widgets.git", True),
                           ("ssh://git@github.com/acme/widgets.git", True)):
            with self.subTest(url=url):
                step = self.detect_checked(self.origin_world(url))
                env.run = lambda argv, cwd=None, url=url: url + "\n"
                reason = bootstrap_start.origin_mismatch(
                    env, "first_sync", bootstrap_steps.MISMATCH_SYNC)

                self.assertEqual(stops, reason is not None)
                self.assertEqual(not stops, step.performable)
                if stops:
                    self.assertIn(reason["title"], step.title)
                    self.assertEqual(reason["command"], step.action["command"])
                    self.assertEqual(reason["text"], step.action["text"])

    def test_a_done_step_with_an_unreadable_origin_stays_done_without_a_dry_run(self):
        run = self.world()
        run.rules.insert(0, (self.ORIGIN_READ, gh_error("exit code 1")))

        step = self.detect_checked(run, self.everything())

        self.assertEqual(("done", None), (step.state, step.details["pending_changes"]))
        self.assertEqual([], run.ran(*SYNC_DRY_RUN))

    def test_missing_issues_with_an_unreadable_origin_is_still_not_checked(self):
        run = self.world()
        run.rules.insert(0, (self.ORIGIN_READ, gh_error("exit code 1")))

        step = self.detect_checked(run, [])

        self.assertEqual("not_checked", step.state)

    def test_the_origin_is_not_read_when_dirty_or_off_the_tip(self):
        for name, run, issues in (
                ("dirty", self.origin_world("x", dirty=" M scripts/a.py\0"), ()),
                ("off the tip", self.origin_world("x", head="other"), ())):
            with self.subTest(case=name):
                self.detect_checked(run, issues)

                self.assertEqual([], run.ran(*self.ORIGIN_READ))

    def test_a_done_step_with_a_bad_origin_skips_the_informational_dry_run(self):
        run = self.origin_world("https://github.com/other/fork.git")

        step = self.detect_checked(run, self.everything())

        self.assertEqual(("done", None), (step.state, step.details["pending_changes"]))
        self.assertEqual([], run.ran(*SYNC_DRY_RUN))

    def test_a_done_step_with_a_good_origin_counts_pending_changes(self):
        run = self.origin_world("https://github.com/acme/widgets.git")

        step = self.detect_checked(run, self.everything())

        self.assertEqual(("done", 2), (step.state, step.details["pending_changes"]))

    def test_an_unchecked_first_sync_reports_null_pending_changes(self):
        run = self.world(roadmap_error=gh_error("HTTP 404: Not Found"))

        step = self.detect(run, [], ctx=context(True, tip=TIP))

        self.assertEqual("not_checked", step.state)
        self.assertEqual({"pending_changes": None}, step.details)

    def test_credentials_in_the_origin_url_never_reach_the_step(self):
        for url, shown in (
                ("https://user:SECRET@github.com/acme/widgets.git",
                 "https://***@github.com/acme/widgets.git"),
                ("https://SECRET@github.com/acme/widgets.git",
                 "https://***@github.com/acme/widgets.git"),
                ("ssh://git:SECRET@github.com/acme/widgets.git",
                 "ssh://***@github.com/acme/widgets.git")):
            with self.subTest(url=url):
                step = self.detect_checked(self.origin_world(url))

                self.assertFalse(step.performable)
                self.assertNotIn("SECRET", json.dumps(dataclasses.asdict(step)))
                self.assertIn(shown, step.title)

    def test_a_bad_origin_never_runs_the_dry_run_so_its_error_cannot_leak(self):
        url = "https://user:SECRET@github.com/acme/widgets.git"
        run = self.origin_world(url)
        run.rules.insert(0, (SYNC_DRY_RUN, gh_error(f"Remote is not a GitHub URL: {url!r}")))

        step = self.detect_checked(run)

        self.assertEqual([], run.ran(*SYNC_DRY_RUN))
        self.assertNotIn("SECRET", json.dumps(dataclasses.asdict(step)))
        self.assertFalse(step.performable)

    def test_the_syncs_own_error_message_masks_the_credentials(self):
        url = "https://user:SECRET@github.com/acme/widgets.git"
        done = mock.Mock(stdout=url + "\n")
        with mock.patch.object(project_board.sync.subprocess, "run", return_value=done):
            with self.assertRaises(SystemExit) as raised:
                project_board.sync.origin_repo()

        self.assertNotIn("SECRET", str(raised.exception.code))
        self.assertIn("https://***@github.com/acme/widgets.git", str(raised.exception.code))
        self.assertIs(project_context.mask_url_credentials, bootstrap_steps.mask_url_credentials)

    def test_the_masking_changes_only_the_userinfo(self):
        mask = bootstrap_steps.mask_url_credentials
        self.assertEqual("https://github.com/acme/widgets.git",
                         mask("https://github.com/acme/widgets.git"))
        self.assertEqual("git@github.com:acme/widgets.git", mask("git@github.com:acme/widgets.git"))
        self.assertEqual("https://***@github.com/a/b", mask("https://u:p@w@github.com/a/b"))
        self.assertEqual("https://github.com/a@b/c", mask("https://github.com/a@b/c"))

    def test_an_unchecked_detection_never_reads_the_origin(self):
        run = self.origin_world("https://github.com/other/fork.git")

        step = self.detect(run, [])

        self.assertTrue(step.performable)
        self.assertEqual([], run.ran(*self.ORIGIN_READ))

    def test_the_clean_check_covers_everything_the_dispatched_run_reads(self):
        for path in ("scripts/sync-roadmap-issues.py", "project-context.toml",
                     ".specify/memory/constitution.md"):
            with self.subTest(path=path):
                run = self.world(dirty=f" M {path}\0")

                step = self.detect(run, [])

                self.assertIn(["git", "status", "--porcelain", "-z", "--", "ROADMAP.md", "specs/",
                               "scripts/", ".github/workflows/", ".github/actions/",
                               "project-context.toml",
                               ".specify/memory/constitution.md"], run.calls)
                self.assertIsNone(step.details["pending_changes"])
                self.assertIn(path, step.action["text"])
                self.assertEqual([], run.ran(*SYNC_DRY_RUN))

    def test_done_on_a_dirty_checkout_still_reports_null(self):
        step = self.detect(self.world(dirty=" M ROADMAP.md\0"), self.everything())

        self.assertEqual(("done", None), (step.state, step.details["pending_changes"]))

    def test_a_failing_dry_run_keeps_the_state_and_offers_no_apply(self):
        failing = gh_error("something broke\nsecond line")
        run = self.world(dry=failing)

        step = self.detect(run, [])

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertIsNone(step.details["pending_changes"])
        self.assertIn("preview failed", step.action["text"])
        self.assertIn("something broke (exit code 1)", step.action["text"])
        self.assertNotIn("second line", step.action["text"])

    def test_blocked_entries_name_the_roadmap_fix(self):
        # the real script prints this to stdout and exits non-zero with an empty stderr
        out = ("[skip] 001: blocked by 000\n"
               "Roadmap sync left blocked entries unchanged: 001\n")

        step = self.detect(self.world(dry=gh_error("", stdout=out, returncode=1)), [])

        self.assertFalse(step.performable)
        self.assertIn("fix the roadmap entries named above", step.action["text"])
        self.assertIn("exit code 1", step.action["text"])

    def test_a_failing_dry_run_shows_its_exit_code(self):
        step = self.detect(self.world(dry=gh_error("boom", returncode=7)), [])

        self.assertIn("exit code 7", step.action["text"])

    def test_the_dry_run_skips_the_board_only_when_asked(self):
        for skip in (False, True):
            with self.subTest(skip=skip):
                run = self.world()
                self.detect(run, self.everything(), skip_project=skip)
                (call,) = run.ran(*SYNC_DRY_RUN)
                self.assertEqual(skip, "--skip-project" in call)
                self.assertNotIn("--apply", call)

    def test_exits_from_the_reused_code_are_not_checked_with_the_first_line(self):
        cases = {
            "load": dict(load_error=gh_error("boom\nmore")),
            "duplicates": dict(issues=self.everything() + issues_for(
                "001", "specs/001-alpha", planning=False, phase1=False)),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                step = self.detect(self.world(), **kwargs)
                self.assertEqual("not_checked", step.state)
                self.assertIsNotNone(step.action)
                self.assertNotIn("\n", step.title)
        step = self.detect(self.world(), load_error=gh_error("boom\nmore"))
        self.assertIn("failed: boom", step.title)
        self.assertEqual(f"repos/{REPO}/issues?state=all&per_page=100" in step.title, True)

    def test_an_unreadable_issue_listing_is_not_checked_with_its_first_line(self):
        for name, listing in (("not json", "<html>\nBad gateway"), ("truncated", '{"number": 1')):
            with self.subTest(case=name):
                step = self.detect(self.world(), load_error=listing)
                self.assertEqual(("first_sync", "not_checked"), (step.id, step.state))
                self.assertNotIn("\n", step.title)
                self.assertIsNotNone(step.action)
                if name == "not json":
                    self.assertIn("Expecting value", step.title)  # the JSON error's first line

    def test_the_sync_gh_is_restored_after_a_failed_load(self):
        original = project_board.sync.gh

        self.detect(self.world(), load_error=gh_error("boom"))

        self.assertIs(original, project_board.sync.gh)

    def test_an_unparsable_roadmap_is_not_checked(self):
        step = self.detect(self.world("### 1 — Broken  [status: planned]\n"), [])

        self.assertEqual("not_checked", step.state)
        self.assertIn("Cannot parse ROADMAP.md", step.title)

    def test_an_unreadable_roadmap_is_not_checked(self):
        for error in (gh_error("gh: Not Found (HTTP 404)"), gh_error("HTTP 502: bad")):
            with self.subTest(error=str(error)):
                step = self.detect(self.world(roadmap_error=error), [])
                self.assertEqual("not_checked", step.state)
                self.assertIsNotNone(step.action)

    def test_an_unknown_merge_state_is_not_checked_without_any_call(self):
        run = self.world()

        step = self.detect(run, [], ctx=context(None, tip=TIP))

        self.assertEqual("not_checked", step.state)
        self.assertIsNotNone(step.action)
        self.assertEqual([], run.calls)

    def test_before_the_merge_it_waits_without_any_call(self):
        run = self.world()

        step = self.detect(run, [], ctx=context(False, tip=TIP))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual([], run.calls)

    def test_a_merged_repository_whose_tip_could_not_be_read_says_to_rerun(self):
        run = self.world()

        step = self.detect(run, [], ctx=context(True, tip=None))

        self.assertEqual(("todo", False), (step.state, step.performable))
        self.assertEqual(bootstrap_steps.RERUN, step.action["command"])
        self.assertIn("could not be read", step.action["text"])
        self.assertNotIn("Check out", step.action["text"])
        self.assertEqual([], run.ran(*SYNC_DRY_RUN))

    def test_it_only_reads(self):
        run = self.world()

        self.detect(run, [])

        allowed = (RAW_ROADMAP, ["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"],
                   SYNC_DRY_RUN, LOAD_ISSUES)
        for call in run.calls:
            self.assertTrue(any(call[:len(prefix)] == prefix for prefix in allowed), call)
        self.assertEqual(5, len(run.calls))


DETECTORS = {
    "detect_prerequisites": "prerequisites", "detect_bootstrap_issue": "bootstrap_issue",
    "detect_project_context": "project_context", "detect_constitution": "constitution",
    "detect_roadmap": "roadmap", "detect_github_settings": "github_settings",
    "detect_board_credential": "board_credential", "detect_board_workflows": "board_workflows",
    "detect_pull_request": "pull_request", "detect_merge": "merge",
    "detect_first_sync": "first_sync",
}
CONFIRMING = [entry.id for entry in bootstrap_steps.CATALOGUE if entry.needs_confirmation]


class SetUpRepositoryRun(ReadingRun):
    """A gh and git for a repository whose bootstrap is merged: every read has an answer."""

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        answer = None
        if argv[:3] == ["gh", "repo", "view"] and "defaultBranchRef" in argv:
            answer = '{"defaultBranchRef": {"name": "main"}}'
        elif argv[:3] == ["gh", "pr", "list"]:
            answer = "[]"
        elif argv[:2] == ["gh", "api"] and "--paginate" in argv and "/issues?" in argv[3]:
            answer = ""
        elif argv[:2] == ["gh", "api"] and "/contents/" in argv[-1]:
            path = argv[-1].split("/contents/")[1].split("?")[0]
            answer = {"project-context.toml": CHECK_CONTEXT.format(approvals=1),
                      "ROADMAP.md": ROADMAP}.get(path, "x")
        elif argv[:2] == ["gh", "api"] and "/branches/" in argv[-1] + argv[-3]:
            answer = "abc\n"
        elif argv[:3] == ["git", "rev-parse", "HEAD"]:
            answer = "abc\n"
        elif argv[:4] == ["git", "config", "--get", "remote.origin.url"]:
            answer = "https://github.com/acme/widgets.git\n"
        elif argv[0] == "python3":
            answer = ""
        if answer is None:
            return super().__call__(argv, cwd)
        self.calls.append(argv)
        return answer


class RealDetectorsTest(unittest.TestCase):
    """build_report with every real detector: a wiring mistake must not hide as not_checked."""

    def test_a_set_up_repository_has_no_not_checked_step(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "project-context.toml").write_text(CHECK_CONTEXT.format(approvals=1))
        (root / ".specify" / "memory").mkdir(parents=True)
        (root / ".specify" / "memory" / "constitution.md").write_text("# C\n")
        (root / "ROADMAP.md").write_text(ROADMAP)
        files = ({Path(".claude/agents/a.md"): "x"}, [])
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "agents" / "a.md").write_text("x")
        for target, value in (("find_problems", []), ("expected_agent_files", files)):
            patcher = mock.patch.object(bootstrap_check.agent_setup, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        report = bootstrap_steps.build_report(root, SetUpRepositoryRun(), lambda: None)

        self.assertEqual(list(IDS), [step["id"] for step in report["steps"]])
        for step in report["steps"]:
            self.assertNotEqual("not_checked", step["state"], step["title"])
            self.assertNotIn("positional argument", step["title"])


BOOTSTRAP_DOC = Path(__file__).resolve().parent.parent / "docs" / "bootstrap.md"
SECTION_HEADING = "## Machine-readable status"


def documented_status(path=BOOTSTRAP_DOC):
    """Parse the "Machine-readable status" section of docs/bootstrap.md.

    Returns (tables, version, example). Each `###` subsection with a table maps the backticked
    first cell of a row to the text of its second cell (`details_keys` reads the keys written
    as "`key`:" from it; a row with no key says "none"). A row whose first cell is not
    backticked, or a duplicate row, is an error; "Exit codes" is keyed by its plain number.
    The version is the backticked number after "`format_version` is"; the example is the
    first json block of the section, parsed.
    """
    text = path.read_text()
    start = text.index(SECTION_HEADING)
    rest = text[start + len(SECTION_HEADING):]
    end = rest.find("\n## ")
    section = rest if end < 0 else rest[:end]
    tables = {}
    for part in section.split("\n### ")[1:]:
        title, _, body = part.partition("\n")
        title = title.strip()
        rows = {}
        header = True
        for line in body.splitlines():
            if not line.startswith("|"):
                header = True
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if header or set(cells[0]) <= set("-: "):
                header = header and not set(cells[0]) <= set("-: ")
                continue
            pattern = r"(\d)" if title == "Exit codes" else r"`(\w+)`"
            first = re.fullmatch(pattern, cells[0])
            if not first:
                raise AssertionError(f"{title}: first cell is not a name: {cells[0]!r}")
            name = first.group(1)
            if name in rows:
                raise AssertionError(f"{title}: duplicate row {name!r}")
            rows[name] = cells[1]
        if rows:
            tables[title] = rows
    version = re.search(r"`format_version` is `(\d+)`", section)
    block = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    return (tables, int(version.group(1)) if version else None,
            json.loads(block.group(1)) if block else None)


def backticked(cell):
    return set(re.findall(r"`(\w+)`", cell))


def details_keys(tables):
    """Step id to the set of `details` keys its row documents."""
    return {step_id: set(re.findall(r"`(\w+)`:", cell))
            for step_id, cell in tables["Steps and details keys"].items()}


class DocumentedStatusTest(unittest.TestCase):
    """The doc's "Machine-readable status" section matches the engine's output both ways (D8)."""

    def make_root(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "project-context.toml").write_text(CHECK_CONTEXT.format(approvals=1))
        (root / ".specify" / "memory").mkdir(parents=True)
        (root / ".specify" / "memory" / "constitution.md").write_text("# C\n")
        (root / "ROADMAP.md").write_text(ROADMAP)
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "agents" / "a.md").write_text("x")
        files = ({Path(".claude/agents/a.md"): "x"}, [])
        for target, value in (("find_problems", []), ("expected_agent_files", files)):
            patcher = mock.patch.object(bootstrap_check.agent_setup, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return root

    def report(self, root, warning=None, prs=None, merged=None):
        """build_report on a set-up repository, with optional conditional facts patched in."""
        real_context = bootstrap_steps.read_repository_context
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                bootstrap_steps, "ruleset_warning", return_value=warning))
            if prs is not None:
                stack.enter_context(mock.patch.object(
                    bootstrap_steps, "read_pull_requests", return_value=prs))
            if merged is not None:
                stack.enter_context(mock.patch.object(
                    bootstrap_steps, "read_repository_context",
                    side_effect=lambda *args: dataclasses.replace(
                        real_context(*args), bootstrap_merged=merged)))
            return bootstrap_steps.build_report(root, SetUpRepositoryRun(), lambda: None)

    def engine_reports(self):
        """Reports from fixtures that together make every documented details key appear."""
        failing = {"name": "ci", "conclusion": "FAILURE"}
        open_pr = {"number": 7, "headRefName": "5-chore-bootstrap", "headRefOid": "abc",
                   "statusCheckRollup": [failing]}
        fixtures = [
            ({"ruleset_warning"}, self.report(
                self.make_root(), warning="rulesets could not be read")),
            ({"failing_checks", "pull_request"}, self.report(
                self.make_root(), merged=False, prs=bootstrap_steps.PullRequests(
                    open=open_pr, current="5-chore-bootstrap"))),
            ({"earlier_pull_request"}, self.report(
                self.make_root(), merged=False, prs=bootstrap_steps.PullRequests(
                    closed={"number": 3}, current="main"))),
        ]
        for keys, report in fixtures:
            found = {key for step in report["steps"] for key in step["details"]}
            self.assertLessEqual(keys, found)
        return [report for _, report in fixtures]

    def test_the_documented_fields_and_keys_match_the_engine(self):
        self.check_against_engine(*documented_status()[:2])

    def check_against_engine(self, tables, version):
        reports = self.engine_reports()
        engine_steps = {}
        for report in reports:
            for step in report["steps"]:
                engine_steps.setdefault(step["id"], set()).update(step["details"])

        self.assertEqual(bootstrap_steps.FORMAT_VERSION, version)
        self.assertEqual(set(reports[0]), set(tables["Report fields"]))
        step_fields = set(reports[0]["steps"][0])
        self.assertEqual(step_fields, set(tables["Step fields"]))
        self.assertEqual(list(IDS), list(tables["Steps and details keys"]))
        self.assertEqual(engine_steps, details_keys(tables))
        self.assertEqual(set(bootstrap_steps.STATES), backticked(tables["Step fields"]["state"]))
        self.assertEqual({entry.actor for entry in bootstrap_steps.CATALOGUE},
                         backticked(tables["Step fields"]["actor"]))
        self.assertEqual(self.engine_results(), backticked(tables["Report fields"]["result"]))
        self.assertEqual({code: self.exit_code_of(meaning)
                          for code, meaning in tables["Exit codes"].items()},
                         {"0": 0, "1": 1, "2": 2})
        earlier_states = {
            step["details"]["earlier_pull_request"]["state"]
            for report in self.earlier_pull_request_reports() for step in report["steps"]
            if "earlier_pull_request" in step["details"]}
        self.assertEqual(earlier_states, backticked(
            tables["Steps and details keys"]["pull_request"].split("`earlier_pull_request`")[1]
        ) - {"number", "state"})

    def earlier_pull_request_reports(self):
        return [self.report(self.make_root(), merged=False, prs=bootstrap_steps.PullRequests(
            **{kind: {"number": 3}, "current": "main"})) for kind in ("merged", "closed")]

    def engine_results(self):
        """The values `_result` returns, from one fixture per outcome."""
        todo = bootstrap_steps.make_step("roadmap", "todo", "t")
        done = bootstrap_steps.make_step("roadmap", "done", "t")
        manual = bootstrap_steps.make_step("board_workflows", "manual", "t")
        return {bootstrap_steps._result(steps) for steps in ([todo], [done, manual], [done])}

    def exit_code(self, **patch):
        """bootstrap.py's exit code for `check --json` with build_report patched."""
        with mock.patch.object(bootstrap_steps, "build_report", **patch):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                return bootstrap.main(["check", "--json"], root=".", run=None)

    def exit_code_of(self, meaning):
        """The code the engine gives for what a documented meaning names: results, or no report."""
        results = backticked(meaning) & self.engine_results()
        if not results:
            return self.exit_code(side_effect=RuntimeError("no report"))
        codes = {self.exit_code(return_value={"result": result}) for result in results}
        self.assertEqual(1, len(codes), meaning)
        return codes.pop()

    def test_the_documented_example_has_the_documented_fields(self):
        tables, _, example = documented_status()

        self.assertIsNotNone(example, "no json example in the section")
        self.assertEqual(set(tables["Report fields"]), set(example))
        self.assertEqual(bootstrap_steps.FORMAT_VERSION, example["format_version"])
        self.assertIn(example["result"], backticked(tables["Report fields"]["result"]))
        for step in example["steps"]:
            self.assertEqual(set(tables["Step fields"]), set(step))
            self.assertIn(step["state"], backticked(tables["Step fields"]["state"]))
            self.assertIn(step["actor"], backticked(tables["Step fields"]["actor"]))
            self.assertLessEqual(set(step["details"]), details_keys(tables)[step["id"]])


class ReportTest(unittest.TestCase):
    """build_report(root, run, remote_default) -> dict (contracts/check-json.md, data-model.md)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls = {}
        self.patchers = []
        self.addCleanup(self.reset_world)

    def reset_world(self):
        """Stop every patcher and forget the recorded calls, so a subtest starts clean."""
        while self.patchers:
            self.patchers.pop().stop()
        self.calls = {}

    def patch(self, name, **kwargs):
        patcher = mock.patch.object(bootstrap_steps, name, **kwargs)
        patched = patcher.start()
        self.patchers.append(patcher)
        return patched

    def fake_world(self, states=None, performable=(), ctx=None, warning="warn"):
        """Patch every detector to return the given state per step; record their arguments."""
        states = {step_id: "done" for step_id in IDS} | (states or {})
        self.ctx = ctx or context(True, text="[project]\n")
        self.prs = bootstrap_steps.PullRequests(current="main")
        self.patch("read_repository_context", side_effect=self.record(
            "context", lambda *args: self.ctx))
        self.patch("read_pull_requests", side_effect=self.record("prs", lambda *args: self.prs))
        self.patch("ruleset_warning", side_effect=self.record("warning", lambda *args: warning))
        for name, step_id in DETECTORS.items():
            def detect(*args, step_id=step_id, **kwargs):
                return bootstrap_steps.make_step(
                    step_id, states[step_id], f"{step_id} title",
                    performable=step_id in performable,
                    # set once `gh auth status` succeeded, which lets the GitHub steps run
                    details={"project_scope": "listed"} if step_id == "prerequisites" else None)
            self.patch(name, side_effect=self.record(name, detect))
        self.patch_repo()

    def patch_repo(self):
        patcher = mock.patch.object(
            bootstrap_github, "repository_name", return_value=REPO)
        self.repository_name = patcher.start()
        self.patchers.append(patcher)

    def record(self, name, answer):
        def call(*args, **kwargs):
            self.calls.setdefault(name, []).append((args, kwargs))
            return answer(*args, **kwargs)
        return call

    def build(self, run=None, remote_default=lambda: None):
        return bootstrap_steps.build_report(self.root, run or Scripted(), remote_default)

    def test_the_document_has_the_contract_shape_with_all_eleven_steps_in_order(self):
        self.fake_world(ctx=context(False, default="trunk"))

        report = self.build()

        self.assertEqual(
            ["format_version", "result", "next_step", "bootstrap_merged", "default_branch",
             "steps"], list(report))
        self.assertEqual(1, report["format_version"])
        self.assertEqual((False, "trunk"), (report["bootstrap_merged"], report["default_branch"]))
        self.assertEqual(list(IDS), [step["id"] for step in report["steps"]])
        self.assertEqual(
            ["id", "title", "state", "blocking", "actor", "needs_decision", "needs_confirmation",
             "performable", "bypasses_review", "action", "details"], list(report["steps"][0]))
        json.dumps(report)  # serialisable as is

    def test_an_unreadable_default_branch_is_null_in_the_report(self):
        self.fake_world(ctx=context(None, default=None, tip=None, error="boom"))

        report = self.build()

        self.assertIsNone(report["bootstrap_merged"])
        self.assertIsNone(report["default_branch"])

    def test_result_is_incomplete_when_a_blocking_step_is_todo_waiting_or_not_checked(self):
        # data-model.md: "`incomplete` if any blocking step is `todo`, `waiting` or `not_checked`"
        for step_id in IDS:
            for state in ("todo", "waiting", "not_checked"):
                if not bootstrap_steps._BY_ID[step_id].blocking:
                    continue
                with self.subTest(step=step_id, state=state):
                    self.reset_world()
                    self.fake_world({step_id: state, "board_workflows": "manual"})
                    self.assertEqual("incomplete", self.build()["result"])

    def test_result_is_complete_with_manual_settings_when_only_a_manual_step_remains(self):
        # "else `complete_with_manual_settings` if any step is `manual`"
        self.fake_world({"board_workflows": "manual"})

        report = self.build()

        self.assertEqual("complete_with_manual_settings", report["result"])
        self.assertIsNone(report["next_step"])

    def test_result_is_complete_when_nothing_is_open(self):
        # "else `complete`": a repository bootstrapped by hand
        self.fake_world({"board_credential": "optional"})

        report = self.build()

        self.assertEqual("complete", report["result"])
        self.assertIsNone(report["next_step"])

    def test_a_non_blocking_step_that_is_not_checked_never_makes_the_result_incomplete(self):
        # research D11: only board_credential and board_workflows are non-blocking
        self.fake_world({"board_credential": "not_checked", "board_workflows": "not_checked"})

        report = self.build()

        self.assertEqual("complete", report["result"])
        self.assertIsNone(report["next_step"])

    def test_next_step_is_the_first_blocking_step_that_is_not_done(self):
        # "ID of the first blocking step that is not `done`"; non-blocking steps never count
        self.fake_world({"board_credential": "optional", "board_workflows": "manual",
                         "merge": "waiting", "first_sync": "todo", "constitution": "todo"})

        report = self.build()

        self.assertEqual("constitution", report["next_step"])
        self.assertEqual("incomplete", report["result"])

    def test_an_unreadable_blocking_step_is_the_next_step(self):
        # research D11: "It counts as the next step when it is the first unfinished one"
        self.fake_world({"roadmap": "not_checked", "merge": "todo"})

        self.assertEqual("roadmap", self.build()["next_step"])

    def test_a_repository_bootstrapped_by_hand_names_only_the_missing_parts(self):
        self.fake_world({"first_sync": "todo", "board_credential": "optional"},
                        performable=("first_sync",))

        report = self.build()

        self.assertEqual("incomplete", report["result"])
        self.assertEqual("first_sync", report["next_step"])
        self.assertEqual(["first_sync"], [step["id"] for step in report["steps"]
                                           if step["state"] not in ("done", "optional")])

    def test_performable_comes_from_the_detector_for_confirmation_steps_only(self):
        self.fake_world({"project_context": "todo"}, performable=("project_context",))

        steps = {step["id"]: step for step in self.build()["steps"]}

        self.assertTrue(steps["project_context"]["performable"])
        self.assertEqual(["project_context"],
                         [step_id for step_id, step in steps.items() if step["performable"]])
        self.assertIn("project_context", CONFIRMING)

    def test_the_ruleset_warning_is_computed_once_and_shared_by_both_steps(self):
        self.fake_world(warning="shared warning")

        self.build()

        self.assertEqual(1, len(self.calls["warning"]))
        self.assertEqual(REPO, self.calls["warning"][0][0][0])
        (context_args, _), = self.calls["detect_project_context"]
        (settings_args, _), = self.calls["detect_github_settings"]
        self.assertEqual("shared warning", context_args[-1])
        self.assertEqual("shared warning", settings_args[-1])

    def test_the_repository_context_and_pull_requests_are_read_once(self):
        self.fake_world()

        self.build()

        self.assertEqual(1, len(self.calls["context"]))
        self.assertEqual(1, len(self.calls["prs"]))
        for name in ("detect_pull_request", "detect_merge"):
            (args, _), = self.calls[name]
            self.assertIs(self.prs, args[-1])
        for name in ("detect_bootstrap_issue", "detect_pull_request", "detect_first_sync"):
            (args, _), = self.calls[name]
            self.assertIs(self.ctx, args[3])
        (args, _), = self.calls["detect_merge"]
        self.assertIs(self.ctx, args[0])

    def test_the_first_sync_skips_the_board_unless_the_credential_is_done(self):
        for state, expected in (("done", False), ("optional", True), ("not_checked", True)):
            with self.subTest(credential=state):
                self.reset_world()
                self.fake_world({"board_credential": state})

                self.build()

                (_, kwargs), = self.calls["detect_first_sync"]
                self.assertIs(expected, kwargs["skip_project"])

    def test_a_detector_that_raises_becomes_a_not_checked_step(self):
        self.fake_world()
        self.patch("detect_roadmap", side_effect=bootstrap_github.CommandError("gh: boom"))

        report = self.build()

        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual("not_checked", steps["roadmap"]["state"])
        self.assertIn("boom", steps["roadmap"]["title"])
        self.assertEqual("incomplete", report["result"])
        self.assertEqual("roadmap", report["next_step"])

    def test_the_repository_is_resolved_with_the_given_root_run_and_remote_lookup(self):
        self.fake_world()
        run, remote = Scripted(), lambda: ("acme", "widgets")

        self.build(run, remote)

        self.repository_name.assert_called_once_with(run, self.root, remote)

    def test_an_unresolvable_repository_gives_not_checked_github_steps_not_an_error(self):
        self.fake_world()
        self.repository_name.side_effect = ValueError("cannot determine the repository")

        report = self.build()

        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual(11, len(steps))
        for step_id in ("bootstrap_issue", "github_settings", "board_credential", "pull_request",
                        "merge", "first_sync"):
            self.assertEqual("not_checked", steps[step_id]["state"], step_id)
            self.assertIn("cannot determine the repository", steps[step_id]["title"], step_id)
        self.assertEqual("incomplete", report["result"])
        self.assertNotIn("ruleset_warning", self.calls)
        self.assertNotIn("context", self.calls)

    GITHUB_STEPS = ("bootstrap_issue", "github_settings", "board_credential", "board_workflows",
                    "pull_request", "merge", "first_sync")

    def test_with_gh_logged_out_only_gh_auth_status_runs_and_github_steps_are_not_checked(self):
        real = bootstrap_steps.detect_prerequisites
        self.fake_world()
        self.patch("detect_prerequisites", side_effect=real)
        self.patch("detect_board_workflows", side_effect=bootstrap_steps.detect_board_workflows)
        (self.root / "project-context.toml").write_text(
            '[project]\nname = "w"\n\n[github_project]\nowner = "acme"\nnumber = 3\n')
        run = Scripted((["gh", "auth", "status"], gh_error("not logged in")))

        report = self.build(run, lambda: ("acme", "widgets"))

        self.assertEqual([["gh", "auth", "status", "--active", "--hostname", "github.com"]],
                         run.calls)
        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual("todo", steps["prerequisites"]["state"])
        for step_id in self.GITHUB_STEPS:
            self.assertEqual("not_checked", steps[step_id]["state"], step_id)
            self.assertIn(steps["prerequisites"]["title"], steps[step_id]["title"], step_id)
        for step_id in ("project_context", "constitution", "roadmap"):
            self.assertEqual("done", steps[step_id]["state"], step_id)
        self.repository_name.assert_not_called()
        self.assertNotIn("context", self.calls)
        self.assertNotIn("prs", self.calls)

    def test_a_step_that_builds_an_invalid_step_is_a_wiring_bug_not_a_not_checked_step(self):
        self.fake_world()
        self.patch("detect_roadmap", side_effect=lambda *args: bootstrap_steps.make_step(
            "roadmap", "no-such-state", "x"))

        with self.assertRaises(bootstrap_steps.StepError):
            self.build()

    def test_a_key_or_type_error_inside_a_detector_is_not_swallowed(self):
        for error in (KeyError("k"), TypeError("takes 1 positional argument but 2 were given")):
            with self.subTest(error=type(error).__name__):
                self.reset_world()
                self.fake_world()
                self.patch("detect_roadmap", side_effect=error)

                with self.assertRaises(type(error)):
                    self.build()

    def test_a_prerequisites_detector_that_raises_also_counts_as_gh_unusable(self):
        self.fake_world()
        self.patch("detect_prerequisites", side_effect=bootstrap_github.CommandError("gh: boom"))

        report = self.build(Scripted(), lambda: ("acme", "widgets"))

        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual("not_checked", steps["prerequisites"]["state"])
        for step_id in self.GITHUB_STEPS:
            self.assertEqual("not_checked", steps[step_id]["state"], step_id)
            self.assertIn("boom", steps[step_id]["title"], step_id)
        self.repository_name.assert_not_called()

    def test_unusable_gh_gives_the_github_steps_the_prerequisites_fix_as_their_action(self):
        # contracts/check-json.md: the reason in `title`, the fix in `action`
        self.fake_world()
        self.patch("detect_prerequisites", return_value=bootstrap_steps.make_step(
            "prerequisites", "todo", "gh is not installed or not logged in: x",
            action=bootstrap_steps.action("Install gh and log in.", "gh auth login",
                                          "docs/bootstrap.md#prerequisites")))

        steps = {s["id"]: s for s in self.build()["steps"]}

        for step_id in self.GITHUB_STEPS:
            self.assertEqual("Install gh and log in.", steps[step_id]["action"]["text"], step_id)
            self.assertEqual("gh auth login", steps[step_id]["action"]["command"], step_id)

    def test_an_unresolvable_repository_keeps_the_generic_rerun_action(self):
        self.fake_world()
        self.repository_name.side_effect = ValueError("cannot determine the repository")

        steps = {s["id"]: s for s in self.build()["steps"]}

        self.assertEqual(bootstrap_steps.RERUN, steps["merge"]["action"]["command"])

    def test_a_not_checked_prerequisites_reason_is_not_prefixed_twice(self):
        self.fake_world()
        self.patch("detect_prerequisites", side_effect=bootstrap_github.CommandError("gh: boom"))

        steps = {s["id"]: s for s in self.build(Scripted())["steps"]}

        for step_id in self.GITHUB_STEPS:
            self.assertEqual("Could not be checked: gh: boom", steps[step_id]["title"], step_id)

    def test_agent_files_that_cannot_be_read_do_not_make_gh_unusable(self):
        real = bootstrap_steps.detect_prerequisites
        self.fake_world()
        self.patch("detect_prerequisites", side_effect=real)
        run = Scripted((["gh", "auth", "status"], "Token scopes: 'repo', 'project'\n"))

        with mock.patch.object(bootstrap_check, "check_agents", side_effect=OSError("denied")):
            steps = {s["id"]: s for s in self.build(run)["steps"]}

        self.assertEqual("not_checked", steps["prerequisites"]["state"])
        for step_id in self.GITHUB_STEPS:
            self.assertEqual("done", steps[step_id]["state"], step_id)
        self.assertEqual(1, len(run.ran("gh", "auth", "status")))
        self.repository_name.assert_called_once()

    def test_a_missing_project_scope_still_lets_the_github_steps_run(self):
        self.fake_world()
        self.patch("detect_prerequisites", return_value=bootstrap_steps.make_step(
            "prerequisites", "todo", "The gh login lacks the project scope",
            details={"project_scope": "missing"}))

        report = self.build()

        steps = {step["id"]: step for step in report["steps"]}
        self.assertEqual("todo", steps["prerequisites"]["state"])
        for step_id in self.GITHUB_STEPS:
            self.assertEqual("done", steps[step_id]["state"], step_id)
        self.repository_name.assert_called_once()

    def test_with_every_command_failing_and_no_project_context_a_report_still_comes_out(self):
        run = Scripted(([], bootstrap_github.CommandError("cannot run: No such file")))

        report = bootstrap_steps.build_report(self.root, run, lambda: None)

        self.assertEqual(list(IDS), [step["id"] for step in report["steps"]])
        self.assertEqual("incomplete", report["result"])
        states = {step["id"]: step["state"] for step in report["steps"]}
        for step_id in ("github_settings", "first_sync", "pull_request", "merge"):
            self.assertEqual("not_checked", states[step_id], step_id)

    def test_every_not_checked_first_sync_carries_null_pending_changes(self):
        run = Scripted(([], bootstrap_github.CommandError("cannot run: No such file")))
        report = bootstrap_steps.build_report(self.root, run, lambda: None)
        step = next(s for s in report["steps"] if s["id"] == "first_sync")
        self.assertEqual("not_checked", step["state"])
        self.assertEqual({"pending_changes": None}, step["details"])

        self.fake_world()
        self.patch("detect_first_sync", side_effect=bootstrap_github.CommandError("boom"))
        report = self.build()
        step = next(s for s in report["steps"] if s["id"] == "first_sync")
        self.assertEqual("not_checked", step["state"])
        self.assertEqual({"pending_changes": None}, step["details"])

    def test_with_gh_answering_nothing_useful_the_report_makes_no_write(self):
        run = Scripted(([], bootstrap_github.CommandError("cannot run: No such file")))

        bootstrap_steps.build_report(self.root, run, lambda: ("acme", "widgets"))

        self.assertTrue(run.calls)
        for call in run.calls:
            self.assertNotIn(call[:3], (["gh", "issue", "create"], ["git", "commit"],
                                        ["git", "push"], ["gh", "pr", "create"]), call)


BIDI, LRM, ZWSP, BOM = chr(0x202E), chr(0x200E), chr(0x200B), chr(0xFEFF)


class UnsafeCommandTest(unittest.TestCase):
    """A command is never escaped (it would not run as shown): one with an unsafe character is
    dropped, and the action text says why."""

    def test_an_action_without_unsafe_characters_is_unchanged(self):
        self.assertEqual({"text": "Do it.", "command": "git switch 'a b\\c'", "doc": "d"},
                         bootstrap_steps.action("Do it.", "git switch 'a b\\c'", "d"))

    def test_an_action_with_an_unsafe_command_has_no_command_and_says_why(self):
        for char in (BIDI, LRM, ZWSP, BOM, "\x1b", "\x85", chr(0x2028)):
            with self.subTest(character=hex(ord(char))):
                made = bootstrap_steps.action("Do it.", f"git switch a{char}b", "d")

                self.assertIsNone(made["command"])
                self.assertTrue(made["text"].startswith("Do it. "))
                self.assertIn("cannot be shown safely", made["text"])
                self.assertIn("by hand", made["text"])
                self.assertEqual("d", made["doc"])

    def test_a_default_branch_with_an_unsafe_character_leaves_the_command_out(self):
        run = IssueWorld([issue(7)], local=["main"]).run()

        step = bootstrap_steps.detect_bootstrap_issue(
            Path("."), run, REPO, context(default=f"rel{BIDI}ease"))

        self.assertIsNone(step.action["command"])
        self.assertIn("cannot be shown safely", step.action["text"])


class EscapeReportTest(unittest.TestCase):
    """`check --json` escapes the text a guide agent would echo, and nothing else."""

    def report(self):
        hostile = f"a{BIDI}b{LRM}{ZWSP}{BOM}\x1b[2K\x85" + chr(0x2028) + "\\x1b"
        steps = [
            dataclasses.asdict(bootstrap_steps.make_step(
                "bootstrap_issue", "todo", hostile, performable=True,
                action={"text": hostile, "command": "git switch 'a\\b'", "doc": "docs/x.md#y"},
                details={"candidates": [{"number": 7, "title": hostile, "branch": hostile}],
                         "agent_tools": [hostile], "ruleset_warning": hostile,
                         "dry_error": hostile, "nested": {"files": (hostile,)},
                         "count": 3, "flag": True, "none": None})),
            dataclasses.asdict(bootstrap_steps.make_step(
                "prerequisites", "done", "fine")),
        ]
        return {"format_version": 1, "result": "incomplete", "next_step": "bootstrap_issue",
                "bootstrap_merged": False, "default_branch": hostile, "steps": steps}

    SHOWN = "a\\u202eb\\u200e\\u200b\\ufeff\\x1b[2K\\x85\\u2028\\\\x1b"

    def test_titles_texts_details_and_the_default_branch_are_escaped(self):
        escaped = bootstrap_steps.escape_report(self.report())

        text = json.dumps(escaped, ensure_ascii=False)
        self.assertFalse(raw_unsafe(text), repr(text))
        first = escaped["steps"][0]
        self.assertEqual(self.SHOWN, first["title"])
        self.assertEqual(self.SHOWN, first["action"]["text"])
        self.assertEqual(self.SHOWN, escaped["default_branch"])
        details = first["details"]
        self.assertEqual([{"number": 7, "title": self.SHOWN, "branch": self.SHOWN}],
                         details["candidates"])
        self.assertEqual([self.SHOWN], details["agent_tools"])
        self.assertEqual(self.SHOWN, details["ruleset_warning"])
        self.assertEqual(self.SHOWN, details["dry_error"])
        self.assertEqual([self.SHOWN], list(details["nested"]["files"]))
        self.assertEqual((3, True, None), (details["count"], details["flag"], details["none"]))

    def test_commands_ids_states_and_docs_are_left_alone_and_the_input_is_not_changed(self):
        report = self.report()
        before = json.dumps(report)

        escaped = bootstrap_steps.escape_report(report)

        self.assertEqual(before, json.dumps(report))
        first = escaped["steps"][0]
        self.assertEqual("git switch 'a\\b'", first["action"]["command"])
        self.assertEqual("docs/x.md#y", first["action"]["doc"])
        self.assertEqual(("bootstrap_issue", "todo"), (first["id"], first["state"]))
        self.assertEqual("fine", escaped["steps"][1]["title"])
        self.assertIsNone(escaped["steps"][1]["action"])

    def test_the_format_version_stays_one(self):
        self.assertEqual(1, bootstrap_steps.FORMAT_VERSION)
        self.assertEqual(1, bootstrap_steps.escape_report(self.report())["format_version"])

    def test_the_cli_prints_the_escaped_report(self):
        out = io.StringIO()
        with mock.patch.object(bootstrap_steps, "build_report",
                               lambda *args, **kwargs: self.report()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            bootstrap.main(["check", "--json"], root=Path("."), run=lambda *a, **k: "",
                           is_tty=False)

        document = json.loads(out.getvalue())
        self.assertEqual(self.SHOWN, document["steps"][0]["title"])
        self.assertFalse(raw_unsafe(out.getvalue()), repr(out.getvalue()))


if __name__ == "__main__":
    unittest.main()
