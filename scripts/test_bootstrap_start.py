"""Tests for bootstrap_start: the preview block and digest, the driver loop and --confirm."""

import contextlib
import dataclasses
import errno
import functools
import hashlib
import io
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import bootstrap_github  # noqa: E402
from display_text_testing import raw_unsafe  # noqa: E402
import project_board  # noqa: E402
import project_context  # noqa: E402
import bootstrap_start  # noqa: E402
import bootstrap_steps  # noqa: E402
import test_bootstrap_github  # noqa: E402
import test_bootstrap_steps  # noqa: E402

ISSUE_STEP = "bootstrap_issue"
DIGEST = re.compile(r"[0-9a-f]{12}")
WRITE_VERBS = (
    ("git", "commit"), ("git", "push"), ("git", "checkout"), ("git", "switch"), ("git", "add"),
    ("gh", "issue", "create"), ("gh", "issue", "develop"), ("gh", "pr", "create"),
    ("gh", "workflow", "run"), ("gh", "run", "watch"),
)


def step(step_id="pull_request", state="todo", performable=True, details=None, title="Title",
         action=None):
    """A step as the report holds it (a dict, the shape of `check --json`)."""
    built = bootstrap_steps.make_step(
        step_id, state, title, performable=performable and state == "todo",
        action=action, details=details)
    return dataclasses.asdict(built)


def report(*steps, default_branch="main"):
    """A report over the given steps; `next_step` is the first blocking one that is not done."""
    next_step = next((s["id"] for s in steps if s["blocking"] and s["state"] != "done"), None)
    manual = any(s["state"] == "manual" for s in steps)
    result = "incomplete" if next_step else (
        "complete_with_manual_settings" if manual else "complete")
    return {"format_version": 1, "result": result,
            "next_step": next_step, "bootstrap_merged": False,
            "default_branch": default_branch, "steps": list(steps)}


# The only commands that may run before a confirmation (SC-003). T038-T047 add the reads their
# previews need here, never a write. `gh api` is allowed only without a method or a field.
READ_ONLY_PREFIXES = (
    ("git", "rev-parse"), ("git", "branch", "--show-current"), ("git", "hash-object"),
    ("git", "status", "--porcelain"), ("git", "merge-base", "--is-ancestor"),
    ("git", "branch", "--format=%(refname:short)"), ("git", "remote", "get-url"),
    ("git", "diff", "--name-only"),  # the `pull_request` preview (T044): <default>..HEAD
    ("git", "check-ignore"),  # the review notes file must be ignored (T133)
    ("gh", "repo", "view"), ("gh", "pr", "list"), ("gh", "pr", "view"), ("gh", "issue", "list"),
    ("gh", "api"),
    # the `github_settings` preview (T042): the existing `github` plan reads these
    ("gh", "label", "list"), ("gh", "secret", "list"), ("gh", "variable", "list"),
    # the `first_sync` preview (T046): the sync's dry run, which test_a_dry_run_needs_no_flag pins
    ("python3", "scripts/sync-roadmap-issues.py"),
    ("git", "config", "--get", "remote.origin.url"),  # the repository that dry run reads
)
API_WRITE_FLAGS = ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")


def is_graphql_read(call):
    """`gh api graphql --input <body file>`: the plan's board reads send their query that way.

    The argv cannot tell a query from a mutation, so a test that relies on this form must also
    check the bodies (`FakeGitHub.writes`).
    """
    return len(call) == 5 and call[:3] == ("gh", "api", "graphql") and call[3] == "--input"


def is_read_only(call, graphql_bodies_checked=False):
    """Whether `call` may run before a confirmation; the graphql body form only when the test
    also checks the bodies it sends."""
    if graphql_bodies_checked and is_graphql_read(call):
        return True
    if not any(call[:len(prefix)] == prefix for prefix in READ_ONLY_PREFIXES):
        return False
    # only the sync's dry run is a read; argparse accepts abbreviations (`--ap`, `--appl`)
    if call[0] == "python3" and any(argument.startswith("--ap") for argument in call):
        return False
    # prefix match, so the joined forms (`-XPOST`, `--method=POST`, `-fkey=v`) are caught too
    return not (call[:2] == ("gh", "api") and any(
        argument.startswith(flag) for argument in call[2:] for flag in API_WRITE_FLAGS))


class FakeRun:
    """Stands in for git and gh: answers by argv prefix, records every call."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = {
            ("git", "rev-parse", "HEAD"): "head000\n",
            ("git", "branch", "--show-current"): "main\n",
            ("gh", "repo", "view"): '{"nameWithOwner": "acme/widgets"}',
            ("gh", "api", "repos/acme/widgets/branches/main"): "tip000\n",
        }
        self.answers.update(answers or {})

    def __call__(self, argv, cwd=None):
        argv = tuple(argv)
        self.calls.append(argv)
        for prefix, answer in sorted(self.answers.items(), key=lambda item: -len(item[0])):
            if argv[:len(prefix)] == prefix:
                if isinstance(answer, Exception):
                    raise answer
                return answer(argv) if callable(answer) else answer
        if argv[:2] == ("git", "hash-object"):
            return f"hash-of-{argv[2]}\n"
        raise bootstrap_github.CommandError(f"unanswered command {argv}", returncode=1)

    def writes(self):
        return [call for call in self.calls
                if any(call[:len(verb)] == verb for verb in WRITE_VERBS)]


class StubAction(bootstrap_start.StepAction):
    """The seam the per-step actions (T038-T047) fill: preview lines, files, perform."""

    def __init__(self, lines=("Will do the thing.",), files=(), flags=(), run=None, fail=None):
        self.lines = list(lines)
        self.files = list(files)
        self.flags = list(flags)
        self.performed = []
        self.run = run
        self.fail = fail
        self.result = None

    def missing_flags(self, env, step_dict):
        return list(self.flags)

    def preview(self, env, step_dict, candidate=None):
        return list(self.lines)

    def commit_files(self, env, step_dict):
        return list(self.files)

    def perform(self, env, step_dict, candidate=None):
        self.performed.append(candidate)
        if self.run:
            self.run(["git", "commit", "-m", "x"])
        if self.fail:
            raise self.fail
        return self.result


def make_env(root=None, run=None, actions=None, is_tty=False, prompt=None, reports=None,
             remote_default=lambda: ("acme", "widgets"), init=bootstrap.init,
             validate_context=bootstrap.validate, toml_string=bootstrap.toml_string,
             push_remote_default=None, flush_input=lambda: None):
    out, err = io.StringIO(), io.StringIO()
    run = run or FakeRun()
    queue = list(reports or [])

    progress = []

    def read_report():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def no_prompt(text):
        raise AssertionError(f"prompt called: {text}")

    env = bootstrap_start.Env(
        root=Path(root or "."), run=run, remote_default=remote_default,
        push_remote_default=push_remote_default, init=init,
        validate_context=validate_context, toml_string=toml_string,
        is_tty=is_tty, prompt=prompt or no_prompt, flush_input=flush_input,
        out=lambda text="": print(text, file=out), err=lambda text="": print(text, file=err),
        progress=progress.append,
        actions=actions if actions is not None else {}, report=read_report)
    env.default_branch = "main"
    env.bold = lambda text: text  # the real one depends on the terminal running the tests
    env.out_buffer, env.err_buffer, env.progress_lines = out, err, progress
    return env


def arguments(**overrides):
    values = {"no_input": False, "confirm": None, "issue": None, "guided": False, "name": None,
              "description": None, "owner": None, "project_number": None,
              "create_board": False, "required_approvals": None, "agents": None,
              "full_dry_run": False, "review_notes": False}
    values.update(overrides)
    return SimpleNamespace(**values)


def sha12(*lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


class PreviewBlockTest(unittest.TestCase):
    def build(self, step_dict=None, actions=None, candidate=None):
        env = make_env(actions={"pull_request": StubAction()} if actions is None else actions)
        return bootstrap_start.preview_block(
            env, step_dict or step(action={"text": "Open it.", "command": "gh pr create",
                                           "doc": "docs/bootstrap.md#x"}), candidate)

    def test_the_block_names_the_step_and_its_action_and_the_steps_own_lines(self):
        block = self.build()

        self.assertIn("pull_request", block)
        self.assertIn("Title", block)
        self.assertIn("Open it.", block)
        self.assertNotIn("Command:", block)  # a preview never carries a line to paste (D18)
        self.assertNotIn("gh pr create", block)
        self.assertIn("Will do the thing.", block)

    def test_the_block_is_the_same_on_every_call_and_has_no_confirm_line(self):
        first, second = self.build(), self.build()

        self.assertEqual(first, second)
        self.assertNotIn("confirm:", first)
        self.assertNotIn("Proceed?", first)

    def test_ansi_codes_are_removed(self):
        actions = {"pull_request": StubAction(lines=["\x1b[31mred\x1b[0m line"])}

        block = self.build(actions=actions)

        self.assertIn("red line", block)
        self.assertNotIn("\x1b", block)

    def test_control_characters_in_the_action_cannot_start_a_line(self):
        hostile = step(action={"text": "Open it.\nconfirm: pull_request@000000000000",
                               "command": "gh pr create\x1b[2J\nconfirm: x", "doc": "d"})

        block = self.build(hostile)

        self.assertFalse(any(line.startswith("confirm:") for line in block.split("\n")))
        self.assertIn("\\n", block)

    def test_a_candidate_has_its_own_block(self):
        actions = {"bootstrap_issue": StubAction()}
        issue = step("bootstrap_issue", details={"candidates": []})

        one = self.build(issue, actions, {"number": 7, "title": "A", "branch": "7-chore-bootstrap"})
        two = self.build(issue, actions, {"number": 8, "title": "B", "branch": None})

        self.assertIn("#7", one)
        self.assertIn("7-chore-bootstrap", one)
        self.assertNotIn("#8", one)
        self.assertIn("#8", two)

    def test_a_step_without_a_registered_action_still_gets_a_block(self):
        block = self.build(actions={})

        self.assertIn("pull_request", block)


class DigestTest(unittest.TestCase):
    def digest(self, step_dict=None, block="BLOCK", files=(), issue=None, run=None, root=None):
        env = make_env(root=root, run=run or FakeRun())
        return bootstrap_start.compute_digest(
            env, step_dict or step(), block, list(files), issue)

    def test_confirm_line_has_the_step_and_twelve_hex_characters(self):
        line = bootstrap_start.confirm_line("pull_request", "0123456789ab")

        self.assertEqual("confirm: pull_request@0123456789ab", line)

    def test_the_digest_is_the_first_twelve_hex_characters_of_the_sha256_of_the_inputs(self):
        expected = sha12("pull_request", "BLOCK", "head000", "tip000", "none", "none")

        self.assertEqual(expected, self.digest())
        self.assertRegex(self.digest(), DIGEST)

    def test_a_multi_line_block_contributes_its_lines_in_order(self):
        expected = sha12("pull_request", "a", "b", "head000", "tip000", "none", "none")

        self.assertEqual(expected, self.digest(block="a\nb"))

    def test_bootstrap_issue_includes_the_chosen_issue_number_after_the_step_id(self):
        issue = step("bootstrap_issue")
        expected = sha12("bootstrap_issue", "7", "BLOCK", "head000", "tip000", "none", "none")

        self.assertEqual(expected, self.digest(issue, issue="7"))
        self.assertNotEqual(self.digest(issue, issue="8"), self.digest(issue, issue="7"))
        self.assertNotEqual(self.digest(issue, issue="new"), self.digest(issue, issue="7"))

    def test_other_steps_ignore_an_issue_number(self):
        self.assertEqual(self.digest(issue="7"), self.digest(issue=None))

    def test_every_file_to_commit_is_hashed_in_path_order_and_a_removal_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "b.txt").write_text("b")
            (Path(directory) / "a.txt").write_text("a")
            run = FakeRun()

            digest = self.digest(files=["b.txt", "gone.txt", "a.txt"], run=run, root=directory)

            expected = sha12(
                "pull_request", "BLOCK", "hash-of-a.txt", "hash-of-b.txt", "deleted",
                "head000", "tip000", "none", "none")
            self.assertEqual(expected, digest)
            self.assertIn(("git", "hash-object", "a.txt"), run.calls)

    def test_a_changed_file_head_or_default_tip_changes_the_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "a.txt").write_text("a")
            base = self.digest(files=["a.txt"], root=directory)
            other_file = FakeRun({("git", "hash-object"): "different\n"})
            other_head = FakeRun({("git", "rev-parse", "HEAD"): "head111\n"})
            other_tip = FakeRun({("gh", "api", "repos/acme/widgets/branches/main"): "tip111\n"})

            for run in (other_file, other_head, other_tip):
                self.assertNotEqual(
                    base, self.digest(files=["a.txt"], run=run, root=directory))

    def test_the_pull_request_head_and_branch_tip_are_read_on_a_bootstrap_branch(self):
        branch = "7-chore-bootstrap"
        with_pr = FakeRun({
            ("git", "branch", "--show-current"): f"{branch}\n",
            ("gh", "pr", "list"): '[{"headRefOid": "pr0001"}]',
            # the branch is pushed too, but an open PR's head is what counts: tip stays none
            ("gh", "api", f"repos/acme/widgets/branches/{branch}"): "pushed1\n",
        })
        without_pr = FakeRun({
            ("git", "branch", "--show-current"): f"{branch}\n",
            ("gh", "pr", "list"): "[]",
            ("gh", "api", f"repos/acme/widgets/branches/{branch}"): "pushed1\n",
        })
        not_pushed = FakeRun({
            ("git", "branch", "--show-current"): f"{branch}\n",
            ("gh", "pr", "list"): "[]",
            ("gh", "api", f"repos/acme/widgets/branches/{branch}"):
                bootstrap_github.CommandError("HTTP 404: Not Found", returncode=1),
        })

        self.assertEqual(
            sha12("pull_request", "BLOCK", "head000", "tip000", "pr0001", "none"),
            self.digest(run=with_pr))
        self.assertEqual(
            sha12("pull_request", "BLOCK", "head000", "tip000", "none", "pushed1"),
            self.digest(run=without_pr))
        self.assertEqual(
            sha12("pull_request", "BLOCK", "head000", "tip000", "none", "none"),
            self.digest(run=not_pushed))

    def test_a_dangling_symlink_is_hashed_by_git_not_reported_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "link").symlink_to(Path(directory, "missing"))
            run = FakeRun()

            digest = self.digest(files=["link"], run=run, root=directory)

            self.assertEqual(sha12("pull_request", "BLOCK", "hash-of-link", "head000", "tip000",
                                   "none", "none"), digest)
            self.assertIn(("git", "hash-object", "link"), run.calls)

    def test_bootstrap_issue_reads_refs_for_the_candidates_own_branch(self):
        issue = step("bootstrap_issue")
        candidate = {"number": 7, "title": "A", "branch": "7-chore-bootstrap"}
        run = FakeRun({  # checked out: main, not the candidate's branch
            ("gh", "pr", "list"): "[]",
            ("gh", "api", "repos/acme/widgets/branches/7-chore-bootstrap"): "pushed1\n",
        })
        env = make_env(run=run)

        digest = bootstrap_start.compute_digest(env, issue, "BLOCK", [], "7", candidate)

        self.assertEqual(sha12("bootstrap_issue", "7", "BLOCK", "head000", "tip000", "none",
                               "pushed1"), digest)

    def test_bootstrap_issue_without_a_branch_has_no_branch_refs(self):
        issue = step("bootstrap_issue")
        candidate = {"number": 7, "title": "A", "branch": None}
        env = make_env()

        digest = bootstrap_start.compute_digest(env, issue, "BLOCK", [], "7", candidate)

        self.assertEqual(sha12("bootstrap_issue", "7", "BLOCK", "head000", "tip000", "none",
                               "none"), digest)

    def test_the_inputs_are_read_without_any_write_command(self):
        run = FakeRun()

        self.digest(run=run)

        self.assertEqual([], run.writes())


def output(env):
    return env.out_buffer.getvalue()


def problems(env):
    return env.err_buffer.getvalue()


def progress(env):
    """The progress lines (FR-010c), as `Env.progress` received them."""
    return env.progress_lines


def prompter(*answers):
    asked = []
    queue = list(answers)

    def prompt(text):
        asked.append(text)
        return queue.pop(0)

    prompt.asked = asked
    return prompt


def done(step_id):
    return step(step_id, "done")


OFFER = step("pull_request", action={"text": "Push and open it.", "command": None,
                                     "doc": "docs/bootstrap.md#one-bootstrap-pull-request"})
ALL_DONE = report(done("prerequisites"), done("pull_request"))


class DriverTest(unittest.TestCase):
    def start(self, reports, args=None, stub=None, **kwargs):
        stub = stub or StubAction()
        env = make_env(reports=reports, actions={"pull_request": stub}, **kwargs)
        return bootstrap_start.run_start(env, args or arguments()), env, stub

    def test_a_complete_report_exits_zero(self):
        code, env, _ = self.start([ALL_DONE])

        self.assertEqual(0, code)
        self.assertIn("BOOTSTRAP IS COMPLETE", output(env))

    def test_non_blocking_steps_that_are_not_done_are_printed_and_passed(self):
        credential = step("board_credential", "not_checked", title="Could not be checked: x",
                          action={"text": "Fix the cause.", "command": None, "doc": None})
        workflows = step("board_workflows", "manual", action={
            "text": "Enable the workflow.", "command": None, "doc": "docs/bootstrap.md#settings"})
        first = report(done("prerequisites"), credential, workflows, done("pull_request"))

        code, env, _ = self.start([first])

        self.assertEqual(0, code)
        self.assertNotIn("board_credential", output(env))  # printed once, after github_settings
        self.assertNotIn("Fix the cause.", output(env))
        self.assertIn("Enable the workflow.", output(env))

    def test_a_plain_start_does_not_print_a_credential_that_is_not_done(self):
        for state in ("optional", "not_checked"):
            with self.subTest(state=state):
                credential = step("board_credential", state, title="Board credential: not set",
                                  action={"text": "Add one.", "command": None,
                                          "doc": "docs/bootstrap.md#board-credentials"})

                code, env, _ = self.start([report(done("prerequisites"), credential,
                                                  done("pull_request"))])

                self.assertEqual(0, code)
                self.assertNotIn("Board credential", output(env))
                self.assertNotIn("board-credentials", output(env))

    def test_a_confirmation_step_prompts_in_a_terminal_and_acts_on_y(self):
        prompt = prompter("y")

        code, env, stub = self.start([report(OFFER), report(OFFER), ALL_DONE], is_tty=True,
                                     prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(["Proceed? [y/N] "], prompt.asked)
        self.assertEqual([None], stub.performed)
        self.assertIn("Will do the thing.", output(env))
        self.assertRegex(output(env), r"confirm: pull_request@[0-9a-f]{12}")

    def test_confirm_label_is_fixed_per_step_and_ignores_the_detector_title(self):
        label = bootstrap_start.confirm_label(
            step("first_sync", title="No planned roadmap entries: nothing to sync yet"))

        self.assertEqual('Confirm "Run the first sync":', label)
        self.assertFalse(label.startswith("confirm: "))

    def test_every_performable_step_of_the_catalogue_has_a_label(self):
        performable = [entry.id for entry in bootstrap_steps.CATALOGUE
                       if entry.needs_confirmation]

        self.assertEqual(5, len(performable))
        for step_id in performable:
            with self.subTest(step=step_id):
                self.assertIn(step_id, bootstrap_start.CONFIRM_LABELS)

    def test_a_candidate_label_names_the_issue_number(self):
        issue = step("bootstrap_issue", title="Several bootstrap issues",
                     details={"candidates": CANDIDATES})

        self.assertEqual('Confirm "Use bootstrap issue #12":',
                         bootstrap_start.confirm_label(issue, "12"))
        self.assertEqual('Confirm "Create or use the bootstrap issue":',
                         bootstrap_start.confirm_label(issue))

    def test_the_label_sits_directly_above_the_confirm_line_without_a_terminal(self):
        code, env, _ = self.start([report(OFFER)])

        lines = output(env).rstrip("\n").split("\n")
        self.assertEqual(3, code)
        self.assertRegex(lines[-1], r"^confirm: pull_request@[0-9a-f]{12}$")
        self.assertEqual(bootstrap_start.confirm_label(OFFER), lines[-2])

    def test_the_label_sits_directly_above_the_confirm_line_in_a_terminal(self):
        prompt = prompter("n")

        _, env, _ = self.start([report(OFFER), report(OFFER)], is_tty=True, prompt=prompt)

        lines = output(env).split("\n")
        index = next(i for i, line in enumerate(lines) if line.startswith("confirm: "))
        self.assertEqual(bootstrap_start.confirm_label(OFFER), lines[index - 1])
        self.assertEqual(["Proceed? [y/N] "], prompt.asked)

    def test_pending_input_is_discarded_immediately_before_the_prompt(self):
        events = []
        prompt = prompter("y")

        def asking(text):
            events.append(("prompt", text))
            return prompt(text)

        code, env, _ = self.start([report(OFFER), report(OFFER), ALL_DONE], is_tty=True,
                                  prompt=asking, flush_input=lambda: events.append("flush"))

        self.assertEqual(0, code)
        self.assertEqual(["flush", ("prompt", "Proceed? [y/N] ")], events)
        # the preview was printed before the input was discarded
        self.assertIn("Will do the thing.", output(env))

    def test_the_flush_comes_after_the_preview_is_printed(self):
        seen = []
        outputs = []

        def flush():
            seen.append("flush")
            outputs.append(output(env))

        env = make_env(reports=[report(OFFER), report(OFFER), ALL_DONE], is_tty=True,
                       prompt=prompter("n"), flush_input=flush,
                       actions={"pull_request": StubAction()})
        bootstrap_start.run_start(env, arguments())

        self.assertEqual(["flush"], seen)
        self.assertIn("confirm: pull_request@", outputs[0])

    def test_no_input_and_no_terminal_never_flush(self):
        calls = []
        for kwargs, args in (({"is_tty": True}, arguments(no_input=True)),
                             ({"is_tty": False}, arguments())):
            with self.subTest(kwargs=kwargs):
                env = make_env(reports=[report(OFFER)], flush_input=lambda: calls.append(1),
                               actions={"pull_request": StubAction()}, **kwargs)

                self.assertEqual(3, bootstrap_start.run_start(env, args))

        self.assertEqual([], calls)

    def test_the_real_flush_discards_terminal_input_and_ignores_failures(self):
        termios_error = type("error", (Exception,), {})  # like `termios.error`: not an OSError
        fake = mock.Mock(TCIFLUSH=7, error=termios_error)
        tty = mock.Mock()
        tty.isatty.return_value = True
        tty.fileno.return_value = 3
        with mock.patch.dict(sys.modules, {"termios": fake}), \
                mock.patch.object(sys, "stdin", tty):
            bootstrap_start.discard_pending_input()
            fake.tcflush.assert_called_once_with(3, 7)

            fake.tcflush.side_effect = OSError("not a tty")
            bootstrap_start.discard_pending_input()  # never fails the run

            fake.tcflush.side_effect = termios_error("bad descriptor")
            bootstrap_start.discard_pending_input()

        tty.isatty.return_value = False
        fake.tcflush.reset_mock()
        with mock.patch.dict(sys.modules, {"termios": fake}), \
                mock.patch.object(sys, "stdin", tty):
            bootstrap_start.discard_pending_input()
        fake.tcflush.assert_not_called()

    def test_without_termios_the_flush_does_nothing(self):
        with mock.patch.dict(sys.modules, {"termios": None}):
            bootstrap_start.discard_pending_input()

    def test_anything_but_y_exits_one_and_changes_nothing(self):
        for answer in ("", "n", "yes", "Y", " y"):
            with self.subTest(answer=answer):
                run = FakeRun()
                stub = StubAction(run=run)

                code, _, _ = self.start(
                    [report(OFFER)], stub=stub, is_tty=True, prompt=prompter(answer), run=run)

                self.assertEqual(1, code)
                self.assertEqual([], stub.performed)
                self.assertEqual([], run.writes())

    def test_without_a_terminal_the_preview_is_printed_and_start_exits_three(self):
        code, env, stub = self.start([report(OFFER)])

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)
        last = output(env).rstrip("\n").split("\n")[-1]
        self.assertRegex(last, r"^confirm: pull_request@[0-9a-f]{12}$")
        self.assertIn("--confirm", output(env))

    def test_no_input_behaves_like_no_terminal_and_never_prompts(self):
        code, env, stub = self.start([report(OFFER)], arguments(no_input=True), is_tty=True)

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)

    def test_the_printed_digest_is_the_one_compute_digest_gives(self):
        _, env, _ = self.start([report(OFFER)])
        block = bootstrap_start.preview_block(env, OFFER)

        expected = bootstrap_start.compute_digest(env, OFFER, block, [])

        self.assertIn(bootstrap_start.confirm_line("pull_request", expected), output(env))

    def test_the_block_is_identical_in_a_terminal_and_before_exit_three(self):
        _, terminal, _ = self.start([report(OFFER)], is_tty=True, prompt=prompter("n"))
        _, piped, _ = self.start([report(OFFER)])

        block = bootstrap_start.preview_block(piped, OFFER)
        self.assertIn(block, output(terminal))
        self.assertIn(block, output(piped))

    def test_a_step_that_stops_being_performable_while_the_prompt_waits_is_not_performed(self):
        blocked = step("pull_request", "todo", False, action={
            "text": "Check out the branch.", "command": None, "doc": "docs/x"})

        code, env, stub = self.start([report(OFFER), report(blocked)], is_tty=True,
                                     prompt=prompter("y"))

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)
        self.assertIn("Check out the branch.", output(env))

    def test_a_different_next_step_after_the_yes_exits_four_and_changes_nothing(self):
        later = step("first_sync", "todo")

        code, env, stub = self.start(
            [report(OFFER, later), report(done("pull_request"), later)], is_tty=True,
            prompt=prompter("y"))

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        self.assertIn("next step is first_sync, not pull_request", problems(env))

    def test_a_non_blocking_step_is_printed_once_across_reports(self):
        manual = step("board_workflows", "manual", title="Workflow is off")
        first = report(manual, OFFER)
        second = report(manual, done("pull_request"))

        code, env, _ = self.start([first, first, second], is_tty=True, prompt=prompter("y"))

        self.assertEqual(0, code)
        self.assertEqual(1, output(env).count("Workflow is off"))
        self.assertIn("BOOTSTRAP IS COMPLETE; MANUAL SETTINGS REMAIN (SEE ABOVE)", output(env))

    def test_end_of_input_at_the_prompt_declines(self):
        def closed(text):
            raise EOFError

        code, env, stub = self.start([report(OFFER)], is_tty=True, prompt=closed)

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)
        self.assertIn("Nothing was changed.", output(env))

    def test_an_interrupt_at_the_prompt_exits_130_without_acting(self):
        def interrupted(text):
            raise KeyboardInterrupt

        stub = StubAction()
        env = make_env(reports=[report(OFFER)], actions={"pull_request": stub}, is_tty=True,
                       prompt=interrupted)

        self.assertEqual(130, bootstrap_start.run_cli(env, []))
        self.assertEqual([], stub.performed)

    def test_the_stop_and_step_output_share_the_action_lines(self):
        stopped = step("constitution", "todo", False, action={
            "text": "Run it.", "command": "cmd --x", "doc": "docs/c"})
        listed = step("board_workflows", "manual", action={
            "text": "Run it.", "command": "cmd --x", "doc": "docs/c"})
        _, stop_env, _ = self.start([report(stopped)])
        _, step_env, _ = self.start([report(listed, done("pull_request"))])

        for env in (stop_env, step_env):
            self.assertIn("  Run it.\n  Command: cmd --x\n  See docs/c\n", output(env))

    def test_each_stop_exits_one_with_the_action_and_the_doc_link(self):
        stops = {
            "prerequisites": step("prerequisites", "todo", False, action={
                "text": "Install gh.", "command": None, "doc": "docs/bootstrap.md#prerequisites"}),
            "constitution": step("constitution", "todo", False, action={
                "text": "Run the skill.", "command": None, "doc": "docs/bootstrap.md#constitution"}),
            "roadmap": step("roadmap", "todo", False, action={
                "text": "Write it.", "command": None, "doc": "docs/bootstrap.md#roadmap"}),
            "merge waiting": step("merge", "waiting", False, action={
                "text": "Merge the PR.", "command": None,
                "doc": "docs/bootstrap.md#one-bootstrap-pull-request"}),
            "blocking not_checked": step("github_settings", "not_checked", False, action={
                "text": "Fix gh.", "command": None, "doc": "docs/bootstrap.md#github-settings"}),
            "not performable": step("project_context", "todo", False, action={
                "text": "Fix the file.", "command": None,
                "doc": "docs/bootstrap.md#project-contexttoml"}),
        }
        for name, blocked in stops.items():
            with self.subTest(stop=name):
                code, env, _ = self.start([report(blocked)])

                self.assertEqual(1, code)
                self.assertIn(blocked["action"]["text"], output(env))
                self.assertIn(blocked["action"]["doc"], output(env))

    def test_steps_after_a_stop_are_not_reached(self):
        stop = step("constitution", "todo", False, action={"text": "Run it.", "command": None,
                                                          "doc": "docs/x"})
        later = step("pull_request", "todo")

        code, env, stub = self.start([report(stop, later)])

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)
        self.assertNotIn("confirm:", output(env))

    def test_a_step_still_open_after_its_action_stops_the_loop_with_exit_one(self):
        stub = StubAction()

        code, env, _ = self.start([report(OFFER)], stub=stub, is_tty=True,
                                  prompt=prompter("y"))

        self.assertEqual(1, code)
        self.assertEqual([None], stub.performed)
        self.assertIn("still not done", problems(env) + output(env))

    def test_the_digest_is_recomputed_after_the_yes_and_a_change_exits_four(self):
        class Shifting(StubAction):
            def preview(self, env, step_dict, candidate=None):
                self.calls = getattr(self, "calls", 0) + 1
                return [f"preview number {self.calls}"]

        stub = Shifting()

        code, env, _ = self.start([report(OFFER)], stub=stub, is_tty=True, prompt=prompter("y"))

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        self.assertIn("preview changed", problems(env))

    def test_a_failed_action_prints_its_message_and_returns_its_code(self):
        stub = StubAction(fail=bootstrap_start.StepFailed("git said no", 2))

        code, env, _ = self.start([report(OFFER)], stub=stub, is_tty=True, prompt=prompter("y"))

        self.assertEqual(2, code)
        self.assertIn("git said no", problems(env))

    def test_a_missing_init_flag_without_a_terminal_stops_with_exit_one_and_no_preview(self):
        init = step("project_context", "todo")
        stub = StubAction(flags=["--description", "--required-approvals"])
        env = make_env(reports=[report(init)], actions={"project_context": stub})

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(1, code)
        self.assertIn("Next step project_context needs your decisions: "
                      "--description, --required-approvals", output(env))
        self.assertEqual("", problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], stub.performed)


CONFIRMABLE = ("bootstrap_issue", "project_context", "github_settings", "pull_request",
               "first_sync")
CANDIDATES = [{"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"},
              {"number": 8, "title": "Bootstrap again", "branch": None}]


def digest_of(env, step_dict, candidate=None, issue=None):
    block = bootstrap_start.preview_block(env, step_dict, candidate)
    files = env.action_for(step_dict["id"]).commit_files(env, step_dict)
    return bootstrap_start.compute_digest(env, step_dict, block, files, issue, candidate)


class ConfirmTest(unittest.TestCase):
    def confirm(self, value, reports, stub=None, issue=None, step_id="pull_request", **kwargs):
        stub = stub or StubAction()
        env = make_env(reports=reports, actions={step_id: stub}, **kwargs)
        code = bootstrap_start.run_start(env, arguments(
            confirm=value, issue=issue, no_input=True))
        return code, env, stub

    def good(self, step_dict=OFFER, candidate=None, issue=None, actions=None):
        """The digest `start` would print for this step in the unchanged state."""
        env = make_env(actions=actions or {step_dict["id"]: StubAction()})
        env.default_branch = "main"
        return digest_of(env, step_dict, candidate, issue)

    def test_a_push_to_the_candidates_branch_after_the_preview_exits_four(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        branch_url = "repos/acme/widgets/branches/7-chore-bootstrap"
        before = FakeRun({("gh", "pr", "list"): "[]", ("gh", "api", branch_url): "pushed1\n"})
        after = FakeRun({("gh", "pr", "list"): "[]", ("gh", "api", branch_url): "pushed2\n"})
        stub = StubAction()
        preview = make_env(run=before, reports=[report(issue)], actions={ISSUE_STEP: stub})
        bootstrap_start.run_start(preview, arguments(no_input=True))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(preview)).group(1)

        code, env, _ = self.confirm(f"bootstrap_issue@{shown}", [report(issue)], stub,
                                    step_id=ISSUE_STEP, run=after)

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        again = self.confirm(f"bootstrap_issue@{shown}", [report(issue), ALL_DONE], stub,
                             step_id=ISSUE_STEP, run=before)
        self.assertEqual(0, again[0])

    def test_an_unknown_next_step_id_exits_two_naming_it(self):
        broken = report(OFFER)
        broken["next_step"] = "mystery"

        code, env, stub = self.confirm("pull_request@0123456789ab", [broken])

        self.assertEqual(2, code)
        self.assertIn("mystery", problems(env))
        self.assertEqual([], stub.performed)

    def test_the_exit_three_hint_keeps_the_issue_flag_and_can_be_followed(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        stub = StubAction()
        first = make_env(reports=[report(issue)], actions={ISSUE_STEP: stub})

        code = bootstrap_start.run_start(first, arguments(no_input=True, issue=8))

        self.assertEqual(3, code)
        lines = output(first).splitlines()
        hint = lines[lines.index("To do it, run:") + 1]
        self.assertIn("--issue 8", hint)
        second = make_env(reports=[report(issue), ALL_DONE], actions={ISSUE_STEP: stub})
        followed = bootstrap_start.parse_arguments(shlex.split(hint)[3:])
        self.assertEqual(0, bootstrap_start.run_start(second, followed))
        self.assertEqual([CANDIDATES[1]], stub.performed)

    def test_the_hint_names_the_issue_for_one_candidate_only_when_it_was_given(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        for given, expected in ((None, False), (7, True)):
            with self.subTest(issue=given):
                env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

                bootstrap_start.run_start(env, arguments(no_input=True, issue=given))

                self.assertEqual(expected, "--issue 7" in output(env))

    def test_the_hint_of_a_new_issue_has_no_issue_flag(self):
        issue = step("bootstrap_issue", details={"candidates": []})
        env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

        bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertNotIn("--issue", output(env))

    def test_the_candidate_listing_has_a_short_header(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

        bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual("2 bootstrap issues found; choose one:", output(env).split("\n")[2])

    def test_each_listed_candidate_has_its_own_hint_with_its_issue_flag(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

        bootstrap_start.run_start(env, arguments(no_input=True))

        for candidate in CANDIDATES:
            digest = self.good(issue, candidate, str(candidate["number"]),
                               {ISSUE_STEP: StubAction()})
            self.assertIn(f"--confirm bootstrap_issue@{digest} --issue {candidate['number']}",
                          output(env))

    def test_a_malformed_or_bare_value_exits_two_before_anything_is_read(self):
        for value in ("pull_request", "pull_request@", "pull_request@zzzzzzzzzzzz",
                      "pull_request@0123456789a", "pull_request@0123456789abc",
                      "pull_request@0123456789AB", "@0123456789ab", "a b@0123456789ab"):
            with self.subTest(value=value):
                code, env, stub = self.confirm(value, [ALL_DONE])

                self.assertEqual(2, code)
                self.assertIn("--confirm", problems(env))
                self.assertEqual([], stub.performed)

    def test_a_complete_bootstrap_exits_zero(self):
        code, env, stub = self.confirm("pull_request@0123456789ab", [ALL_DONE])

        self.assertEqual(0, code)
        self.assertIn("bootstrap is complete", output(env).lower())
        self.assertEqual([], stub.performed)

    def test_another_step_than_the_next_one_exits_four(self):
        code, env, stub = self.confirm("first_sync@0123456789ab", [report(OFFER)])

        self.assertEqual(4, code)
        self.assertIn("next step is pull_request, not first_sync", problems(env))
        self.assertEqual([], stub.performed)

    def test_a_next_step_that_needs_no_confirmation_exits_four(self):
        stop = step("constitution", "todo", False)

        code, env, _ = self.confirm("constitution@0123456789ab", [report(stop)])

        self.assertEqual(4, code)
        self.assertIn("next step constitution needs no confirmation", problems(env))

    def test_a_step_that_is_not_performable_exits_one_with_its_action(self):
        blocked = step("pull_request", "todo", False, action={
            "text": "Check out the branch.", "command": "git checkout x", "doc": None})

        code, env, stub = self.confirm("pull_request@0123456789ab", [report(blocked)])

        self.assertEqual(1, code)
        self.assertIn("Check out the branch.", output(env))
        self.assertEqual([], stub.performed)

    def test_a_missing_init_flag_exits_two_naming_every_flag(self):
        init = step("project_context", "todo")
        stub = StubAction(flags=["--description", "--project-number or --create-board"])

        code, env, _ = self.confirm("project_context@0123456789ab", [report(init)], stub,
                                    step_id="project_context")

        self.assertEqual(2, code)
        self.assertIn("--description", problems(env))
        self.assertIn("--project-number or --create-board", problems(env))
        self.assertEqual([], stub.performed)

    def test_not_performable_is_reported_before_a_missing_init_flag(self):
        blocked = step("project_context", "todo", False, action={
            "text": "Fix the invalid file.", "command": None, "doc": "docs/x"})
        stub = StubAction(flags=["--description"])

        code, env, _ = self.confirm("project_context@0123456789ab", [report(blocked)], stub,
                                    step_id="project_context")

        self.assertEqual(1, code)
        self.assertIn("Fix the invalid file.", output(env))
        self.assertNotIn("--description", problems(env))
        self.assertEqual([], stub.performed)

    def test_an_issue_that_is_not_a_candidate_with_no_candidates_exits_three(self):
        issue = step("bootstrap_issue", details={"candidates": []})
        run = FakeRun()
        stub = StubAction(run=run)

        code, env, _ = self.confirm("bootstrap_issue@0123456789ab", [report(issue)], stub,
                                    issue=5, step_id="bootstrap_issue", run=run)

        self.assertEqual(3, code)
        self.assertIn("#5 is not a bootstrap issue candidate", output(env))
        self.assertIn("without --issue", output(env))
        self.assertEqual([], stub.performed)
        self.assertEqual([], run.writes())

    def test_the_driver_also_stops_for_an_issue_that_is_not_a_candidate_with_none(self):
        issue = step("bootstrap_issue", details={"candidates": []})
        stub = StubAction()
        env = make_env(reports=[report(issue)], actions={"bootstrap_issue": stub},
                       is_tty=True)

        code = bootstrap_start.run_start(env, arguments(issue=5))

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)

    def test_several_candidates_without_a_valid_issue_list_every_preview_and_exit_three(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        for chosen in (None, 99):
            with self.subTest(issue=chosen):
                code, env, stub = self.confirm(
                    "bootstrap_issue@0123456789ab", [report(issue)], issue=chosen,
                    step_id="bootstrap_issue")

                self.assertEqual(3, code)
                self.assertEqual([], stub.performed)
                for candidate in CANDIDATES:
                    expected = self.good(issue, candidate, str(candidate["number"]),
                                         {"bootstrap_issue": StubAction()})
                    self.assertIn(f"#{candidate['number']}", output(env))
                    self.assertIn(
                        bootstrap_start.confirm_line("bootstrap_issue", expected), output(env))

    def test_each_listed_candidate_has_its_own_labelled_confirm_line(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})

        _, env, _ = self.confirm("bootstrap_issue@0123456789ab", [report(issue)], issue=None,
                                 step_id="bootstrap_issue")

        for candidate in CANDIDATES:
            self.assertIn(f'Confirm "Use bootstrap issue #{candidate["number"]}":\nconfirm: ',
                          output(env))

    def test_one_candidate_that_is_not_the_given_issue_is_listed_too(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})

        code, _, stub = self.confirm("bootstrap_issue@0123456789ab", [report(issue)], issue=8,
                                     step_id="bootstrap_issue")

        self.assertEqual(3, code)
        self.assertEqual([], stub.performed)

    def test_the_chosen_candidate_is_acted_on_when_its_digest_matches(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        digest = self.good(issue, CANDIDATES[1], "8", {"bootstrap_issue": StubAction()})

        code, _, stub = self.confirm(f"bootstrap_issue@{digest}", [report(issue), ALL_DONE],
                                     issue=8, step_id="bootstrap_issue")

        self.assertEqual(0, code)
        self.assertEqual([CANDIDATES[1]], stub.performed)

    def test_the_digest_of_another_candidate_exits_four(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        digest = self.good(issue, CANDIDATES[0], "7", {"bootstrap_issue": StubAction()})

        code, _, stub = self.confirm(f"bootstrap_issue@{digest}", [report(issue)], issue=8,
                                     step_id="bootstrap_issue")

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)

    def test_a_single_candidate_or_none_needs_no_issue_flag(self):
        for found, candidate, issue_id in (([], None, "new"), (CANDIDATES[:1], CANDIDATES[0], "7")):
            with self.subTest(candidates=len(found)):
                issue = step("bootstrap_issue", details={"candidates": found})
                digest = self.good(issue, candidate, issue_id, {"bootstrap_issue": StubAction()})

                code, _, stub = self.confirm(
                    f"bootstrap_issue@{digest}", [report(issue), ALL_DONE],
                    step_id="bootstrap_issue")

                self.assertEqual(0, code)
                self.assertEqual([candidate], stub.performed)

    def test_a_digest_mismatch_exits_four_and_changes_nothing(self):
        run = FakeRun()
        stub = StubAction(run=run)

        code, env, _ = self.confirm("pull_request@0123456789ab", [report(OFFER)], stub, run=run)

        self.assertEqual(4, code)
        self.assertIn("preview changed", problems(env))
        self.assertEqual([], stub.performed)
        self.assertEqual([], run.writes())

    def test_a_matching_digest_acts_and_then_continues_to_the_next_confirmation(self):
        later = step("first_sync", "todo")
        stub = StubAction()
        env = make_env(reports=[report(OFFER, later), report(done("pull_request"), later)],
                       actions={"pull_request": stub, "first_sync": StubAction()})
        env.default_branch = "main"
        digest = digest_of(env, OFFER)

        code = bootstrap_start.run_start(env, arguments(
            confirm=f"pull_request@{digest}", no_input=True))

        self.assertEqual(3, code)  # the next confirmation is not given
        self.assertEqual([None], stub.performed)
        self.assertRegex(output(env), r"confirm: first_sync@[0-9a-f]{12}")

    def test_a_confirmation_in_a_terminal_still_never_prompts_for_the_confirmed_step(self):
        stub = StubAction()
        env = make_env(reports=[report(OFFER), ALL_DONE], actions={"pull_request": stub},
                       is_tty=True)
        env.default_branch = "main"
        digest = digest_of(env, OFFER)

        code = bootstrap_start.run_start(env, arguments(confirm=f"pull_request@{digest}"))

        self.assertEqual(0, code)
        self.assertEqual([None], stub.performed)

    def test_a_stray_issue_does_not_change_other_steps(self):
        env = make_env(reports=[report(OFFER), ALL_DONE], actions={"pull_request": StubAction()})
        env.default_branch = "main"
        digest = digest_of(env, OFFER)

        code = bootstrap_start.run_start(env, arguments(
            confirm=f"pull_request@{digest}", issue=42, no_input=True))

        self.assertEqual(0, code)

    def test_the_digest_printed_with_exit_three_is_accepted_for_every_confirmable_step(self):
        # The per-step actions come in T038-T047; this proves the round trip through the seam.
        variants = [(step_id, step_id) for step_id in CONFIRMABLE]
        variants.append(("first_sync", "first_sync without board access"))
        for step_id, label in variants:
            with self.subTest(step=label):
                lines = ["python3 scripts/sync-roadmap-issues.py --skip-project"] if (
                    "without" in label) else ["python3 scripts/sync-roadmap-issues.py"]
                target = step(step_id, details={"candidates": []} if step_id == ISSUE_STEP
                              else None)
                stub = StubAction(lines=lines, files=["a.txt"])
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / "a.txt").write_text("a")
                    first = make_env(root=directory, reports=[report(target)],
                                     actions={step_id: stub})
                    self.assertEqual(3, bootstrap_start.run_start(
                        first, arguments(no_input=True)))
                    shown = re.search(r"confirm: (\w+)@([0-9a-f]{12})", output(first))
                    self.assertEqual(step_id, shown.group(1))

                    second = make_env(root=directory, reports=[report(target), ALL_DONE],
                                      actions={step_id: stub})
                    code = bootstrap_start.run_start(second, arguments(
                        confirm=shown.group(0).removeprefix("confirm: "), no_input=True))

                    self.assertEqual(0, code)
                    self.assertEqual(1, len(stub.performed))

    def test_a_changed_file_between_preview_and_confirmation_exits_four(self):
        stub = StubAction(files=["a.txt"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.txt"
            path.write_text("a")
            run = FakeRun({("git", "hash-object"): lambda argv: Path(directory, argv[2]).read_text()})
            first = make_env(root=directory, run=run, reports=[report(OFFER)],
                             actions={"pull_request": stub})
            bootstrap_start.run_start(first, arguments(no_input=True))
            shown = re.search(r"confirm: pull_request@([0-9a-f]{12})", output(first)).group(1)
            path.write_text("changed")
            second = make_env(root=directory, run=run, reports=[report(OFFER)],
                              actions={"pull_request": stub})

            code = bootstrap_start.run_start(second, arguments(
                confirm=f"pull_request@{shown}", no_input=True))

            self.assertEqual(4, code)
            self.assertEqual([], stub.performed)


class NoWriteBeforeConfirmationTest(unittest.TestCase):
    """SC-003: nothing is written until a confirmation has been given."""

    def test_no_write_command_runs_before_a_confirmation(self):
        for step_id in CONFIRMABLE:
            for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed"):
                with self.subTest(step=step_id, how=how):
                    run = FakeRun()
                    stub = StubAction(run=run)
                    target = step(step_id, details={"candidates": []} if step_id == ISSUE_STEP
                                  else None)
                    env = make_env(run=run, reports=[report(target)], actions={step_id: stub},
                                   is_tty=how == "declined", prompt=prompter("n"))
                    args = {
                        "exit three": arguments(no_input=True),
                        "declined": arguments(),
                        "wrong digest": arguments(
                            confirm=f"{step_id}@0123456789ab", no_input=True),
                        "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                        "malformed": arguments(confirm=step_id, no_input=True),
                    }[how]

                    code = bootstrap_start.run_start(env, args)

                    self.assertNotEqual(0, code)
                    self.assertEqual([], stub.performed)
                    self.assertEqual([], run.writes())
                    self.assertEqual([], [call for call in run.calls if not is_read_only(call)])

    def test_the_allowlist_rejects_writes_and_api_methods(self):
        for call in (("git", "commit", "-m", "x"), ("gh", "api", "-X", "POST", "repos/a/b"),
                     ("gh", "api", "--method", "PATCH", "repos/a/b"),
                     ("gh", "api", "repos/a/b", "-f", "k=v"), ("gh", "pr", "create"),
                     ("gh", "api", "-XPOST", "repos/a/b"),
                     ("gh", "api", "--method=POST", "repos/a/b"),
                     ("gh", "api", "repos/a/b", "-fkey=v"), ("gh", "api", "repos/a/b", "-Fkey=v"),
                     ("gh", "api", "repos/a/b", "--field=key=v"),
                     ("gh", "api", "repos/a/b", "--raw-field=key=v"),
                     ("gh", "api", "repos/a/b", "--input=body.json"),
                     ("git", "stash")):
            with self.subTest(call=call):
                self.assertFalse(is_read_only(call))
        self.assertTrue(is_read_only(("gh", "api", "repos/a/b/branches/main", "--jq", ".x")))

    def test_the_action_runs_only_after_the_confirmation(self):
        run = FakeRun()
        stub = StubAction(run=run)
        env = make_env(run=run, reports=[report(OFFER), ALL_DONE],
                       actions={"pull_request": stub})
        env.default_branch = "main"
        digest = digest_of(env, OFFER)
        before = list(run.calls)

        bootstrap_start.run_start(env, arguments(confirm=f"pull_request@{digest}", no_input=True))

        self.assertEqual([], [call for call in before if call in run.writes()])
        self.assertEqual([("git", "commit", "-m", "x")], run.writes())


class ArgumentsTest(unittest.TestCase):
    def test_the_start_options_and_the_init_flags_are_parsed(self):
        args = bootstrap_start.parse_arguments([
            "--no-input", "--confirm", "pull_request@0123456789ab", "--issue", "7",
            "--name", "Widgets", "--description", "About", "--owner", "acme",
            "--required-approvals", "2", "--project-number", "3"])

        self.assertTrue(args.no_input)
        self.assertEqual("pull_request@0123456789ab", args.confirm)
        self.assertEqual(7, args.issue)
        self.assertEqual(("Widgets", "About", "acme", 2, 3), (
            args.name, args.description, args.owner, args.required_approvals,
            args.project_number))

    def test_the_defaults_are_no_confirmation_and_no_issue(self):
        args = bootstrap_start.parse_arguments([])

        self.assertEqual((False, None, None, False), (
            args.no_input, args.confirm, args.issue, args.create_board))

    def test_a_board_number_and_create_board_exclude_each_other(self):
        with self.assertRaises(ValueError):
            bootstrap_start.parse_arguments(["--project-number", "3", "--create-board"])

    def test_an_abbreviated_option_is_a_usage_error_not_a_guess(self):
        for abbreviated in ("--guide", "--no-in", "--conf"):
            with self.subTest(option=abbreviated):
                with self.assertRaises(ValueError):
                    bootstrap_start.parse_arguments([abbreviated])

        env = make_env(reports=[ALL_DONE])
        code = bootstrap_start.run_cli(
            env, ["--guide", "--confirm", "pull_request@0123456789ab"])

        self.assertEqual(2, code)
        self.assertIn("--guide", problems(env))
        self.assertEqual("", output(env))  # no state was read, so no guided ending either

    def test_a_bad_issue_number_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            bootstrap_start.parse_arguments(["--issue", "seven"])

    def test_main_exits_two_for_a_usage_error_and_130_when_interrupted(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(2, bootstrap_start.main(["--issue", "x"], root=Path(".")))
        self.assertIn("--issue", stderr.getvalue())

        def interrupted():
            raise KeyboardInterrupt

        env = make_env(reports=[ALL_DONE])
        env.report = interrupted
        self.assertEqual(130, bootstrap_start.run_cli(env, ["--no-input"]))

    def test_main_turns_an_unexpected_error_into_exit_two_with_a_message(self):
        stderr = io.StringIO()
        with mock.patch.object(bootstrap_start, "run_cli", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(stderr):
            code = bootstrap_start.main([], root=Path("."), is_tty=False)

        self.assertEqual(2, code)
        self.assertIn("boom", stderr.getvalue())

    def test_main_ends_an_unexpected_error_with_the_guided_line_only_when_guided(self):
        for argv, expected in ((["--guided", "--confirm", "x@0123456789ab"], True), ([], False)):
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                with mock.patch.object(bootstrap_start, "run_cli",
                                       side_effect=RuntimeError("boom")), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(io.StringIO()):
                    code = bootstrap_start.main(argv, root=Path("."), is_tty=False)

                self.assertEqual(2, code)
                self.assertEqual("Go back to your coding agent and tell it what happened.\n"
                                 if expected else "", stdout.getvalue())

    def test_run_parses_then_starts(self):
        env = make_env(reports=[ALL_DONE])

        self.assertEqual(0, bootstrap_start.run_cli(env, ["--no-input"]))
        self.assertEqual(2, bootstrap_start.run_cli(env, ["--confirm", "bare"]))


class TerminalChoiceTest(unittest.TestCase):
    def test_a_terminal_asks_which_of_several_candidates_to_use(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        stub = StubAction()
        env = make_env(reports=[report(issue), report(issue), ALL_DONE],
                       actions={"bootstrap_issue": stub}, is_tty=True,
                       prompt=prompter("#8", "y"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(0, code)
        self.assertEqual([CANDIDATES[1]], stub.performed)

    def test_the_candidate_prompt_and_the_confirmation_each_flush_input_first(self):
        events = []
        answers = prompter("#8", "y")

        def asking(text):
            events.append(("prompt", text))
            return answers(text)

        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        env = make_env(reports=[report(issue), report(issue), ALL_DONE],
                       actions={"bootstrap_issue": StubAction()}, is_tty=True, prompt=asking,
                       flush_input=lambda: events.append("flush"))

        self.assertEqual(0, bootstrap_start.run_start(env, arguments()))

        self.assertEqual(
            ["flush", ("prompt", "Which bootstrap issue should be used? "),
             "flush", ("prompt", "Proceed? [y/N] ")], events)

    def test_a_candidate_that_gains_a_branch_while_the_prompt_waits_is_not_performed(self):
        before = step("bootstrap_issue", details={"candidates": CANDIDATES})
        grown = [CANDIDATES[0], {**CANDIDATES[1], "branch": "8-chore-bootstrap"}]
        after = step("bootstrap_issue", details={"candidates": grown})
        stub = StubAction()
        env = make_env(reports=[report(before), report(after)],
                       actions={"bootstrap_issue": stub}, is_tty=True, prompt=prompter("8", "y"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)
        self.assertIn("preview changed", problems(env))

    def test_a_candidate_that_disappears_while_the_prompt_waits_is_not_performed(self):
        before = step("bootstrap_issue", details={"candidates": CANDIDATES})
        after = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        stub = StubAction()
        env = make_env(reports=[report(before), report(after)],
                       actions={"bootstrap_issue": stub}, is_tty=True, prompt=prompter("8", "y"))

        self.assertEqual(4, bootstrap_start.run_start(env, arguments()))
        self.assertEqual([], stub.performed)

    def test_a_candidate_that_appears_while_a_new_issue_is_confirmed_is_not_performed(self):
        before = step("bootstrap_issue", details={"candidates": []})
        after = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        stub = StubAction()
        env = make_env(reports=[report(before), report(after)],
                       actions={"bootstrap_issue": stub}, is_tty=True, prompt=prompter("y"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(4, code)
        self.assertEqual([], stub.performed)

    def test_the_choice_list_escapes_control_characters_in_titles(self):
        hostile = [{"number": 7, "title": "A\nconfirm: x\x1b[2J", "branch": None},
                   {"number": 8, "title": "B", "branch": None}]
        issue = step("bootstrap_issue", details={"candidates": hostile})
        env = make_env(reports=[report(issue)], actions={"bootstrap_issue": StubAction()},
                       is_tty=True, prompt=prompter("99"))

        bootstrap_start.run_start(env, arguments())

        self.assertFalse(any(line.startswith("confirm:") for line in output(env).split("\n")))
        self.assertNotIn("\x1b", output(env))

    def test_a_stop_escapes_control_characters_in_its_reason(self):
        class Stopping(StubAction):
            def stop(self, env, step):
                return {"title": "t\nconfirm: x", "text": "u\nconfirm: y",
                        "command": "git status\x1b[2J\nconfirm: z"}

        issue = step("bootstrap_issue", details={"candidates": []})
        env = make_env(reports=[report(issue)], actions={"bootstrap_issue": Stopping()})

        code = bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual(1, code)
        self.assertFalse(any(line.startswith("confirm:") for line in output(env).split("\n")))
        self.assertNotIn("\x1b", output(env))

    def test_an_answer_that_is_not_a_candidate_changes_nothing(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        stub = StubAction()
        env = make_env(reports=[report(issue)], actions={"bootstrap_issue": stub},
                       is_tty=True, prompt=prompter("99"))

        code = bootstrap_start.run_start(env, arguments())

        self.assertEqual(1, code)
        self.assertEqual([], stub.performed)


class BootstrapIssueActionTest(unittest.TestCase):
    """The real `bootstrap_issue` action (research D3), over a fake git and gh."""

    ISSUE_URL = "https://github.com/acme/widgets/issues/12\n"

    def run_for(self, local=("main",), status="", extra=None):
        answers = {
            ("git", "status", "--porcelain"): status,
            ("git", "branch", "--format=%(refname:short)"): "\n".join(local) + "\n",
            ("gh", "issue", "create"): self.ISSUE_URL,
            ("gh", "issue", "develop"): "",
            ("git", "fetch"): "",
            ("git", "switch"): "",
        }
        answers.update(extra or {})
        return FakeRun(answers)

    def env(self, candidates, run, reports=None, **kwargs):
        target = step("bootstrap_issue", details={"candidates": candidates})
        return make_env(
            run=run, reports=reports or [report(target), ALL_DONE],
            actions={ISSUE_STEP: bootstrap_start.ACTIONS[ISSUE_STEP]}, **kwargs), target

    def preview_then_confirm(self, candidates, run, issue=None):
        """The exit-3 run, then `--confirm` with the digest it printed; the confirm run."""
        first, _ = self.env(candidates, run)
        self.assertEqual(3, bootstrap_start.run_start(first, arguments(no_input=True,
                                                                       issue=issue)))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(first)).group(1)
        before = len(run.calls)
        second, _ = self.env(candidates, run)
        code = bootstrap_start.run_start(second, arguments(
            confirm=f"bootstrap_issue@{shown}", issue=issue, no_input=True))
        return code, second, first, run.calls[before:]

    def writes(self, calls):
        return [call for call in calls if call[:2] in (
            ("gh", "issue"), ("git", "switch"), ("git", "fetch"), ("git", "stash"))]

    def test_the_action_is_registered(self):
        self.assertIsInstance(bootstrap_start.ACTIONS[ISSUE_STEP], bootstrap_start.StepAction)

    def test_no_candidate_creates_a_neutral_issue_then_develops_its_branch(self):
        run = self.run_for()

        code, env, first, calls = self.preview_then_confirm([], run)

        self.assertEqual(0, code)
        created, developed = self.writes(calls)
        self.assertEqual(("gh", "issue", "create"), created[:3])
        title = created[created.index("--title") + 1]
        body = created[created.index("--body") + 1]
        self.assertEqual("CHORE: bootstrap the project", title)  # the title docs/bootstrap.md uses
        self.assertIn(bootstrap_steps.BOOTSTRAP_MARKER, body)
        for text in (title, body):  # neutral: nothing names the repository or its people
            for word in ("acme", "widgets"):
                self.assertNotIn(word, text.lower())
        self.assertEqual(("gh", "issue", "develop", "12", "--base", "main", "--name",
                          "12-chore-bootstrap", "--checkout", "--repo", "acme/widgets"),
                         developed)

    def test_the_branch_is_developed_from_the_default_branch_the_report_names(self):
        run = self.run_for(local=("develop",))
        target = step("bootstrap_issue", details={"candidates": []})
        reports = [report(target, default_branch="develop"), ALL_DONE]

        def env_for():
            return make_env(run=run, reports=list(reports),
                            actions={ISSUE_STEP: bootstrap_start.ACTIONS[ISSUE_STEP]})

        first = env_for()
        self.assertEqual(3, bootstrap_start.run_start(first, arguments(no_input=True)))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(first)).group(1)
        before = len(run.calls)
        second = env_for()
        code = bootstrap_start.run_start(
            second, arguments(confirm=f"bootstrap_issue@{shown}", no_input=True))

        self.assertEqual(0, code, problems(second))
        (developed,) = [call for call in run.calls[before:] if call[:3] == (
            "gh", "issue", "develop")]
        self.assertEqual("develop", developed[developed.index("--base") + 1])

    def test_progress_names_the_issue_and_the_branch(self):
        run = self.run_for()

        code, env, first, _ = self.preview_then_confirm([], run)

        self.assertEqual(0, code)
        self.assertEqual(["Creating the bootstrap issue…",
                          "Creating and checking out its branch…"],
                         [line for line in progress(env) if not line.startswith("Reading ")])
        self.assertEqual(["Reading the repository and GitHub state…"], progress(first))

    def test_progress_names_the_checkout_of_a_candidates_branch(self):
        run = self.run_for(local=("main",))
        candidates = [{"number": 7, "title": "CHORE: bootstrap the project", "branch": "7-x"}]

        code, env, _, _ = self.preview_then_confirm(candidates, run)

        self.assertEqual(0, code)
        self.assertEqual(["Fetching 7-x…", "Checking out 7-x…"],
                         [line for line in progress(env) if not line.startswith("Reading ")])

    def test_the_issue_is_written_to_the_repository_that_detection_reads(self):
        run = self.run_for()

        code, env, _, calls = self.preview_then_confirm([], run)

        detected = bootstrap_github.repository_name(run, env.root, env.remote_default)
        self.assertEqual(0, code)
        created, developed = self.writes(calls)
        for call in (created, developed):
            self.assertEqual(detected, call[call.index("--repo") + 1])

    def test_an_origin_naming_another_repository_stops_before_any_write(self):
        for flags in ({"no_input": True},
                      {"no_input": True, "confirm": "bootstrap_issue@000000000000"}):
            with self.subTest(flags=flags):
                run = self.run_for()
                env, _ = self.env([], run, remote_default=lambda: ("other", "fork"))

                code = bootstrap_start.run_start(env, arguments(**flags))

                self.assertEqual(1, code)
                self.assertIn("Stopped at bootstrap_issue: git would use other/fork but gh "
                              "would use acme/widgets", output(env))
                self.assertIn("gh repo set-default other/fork", output(env))
                self.assertEqual([], self.writes(run.calls))

    def test_an_origin_that_is_not_a_github_remote_stops_before_any_write(self):
        run = self.run_for()
        env, _ = self.env([], run, remote_default=lambda: None)

        code = bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual(1, code)
        self.assertIn("the origin remote is not a GitHub repository", output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_the_preview_names_the_repository_and_the_digest_binds_it(self):
        first, _ = self.env([], self.run_for())
        bootstrap_start.run_start(first, arguments(no_input=True))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(first)).group(1)
        self.assertIn("Repository: acme/widgets", output(first))
        moved = self.run_for(extra={("gh", "repo", "view"): '{"nameWithOwner": "other/fork"}',
                                    ("gh", "api", "repos/other/fork/branches/main"): "tip000\n"})
        second, _ = self.env([], moved, remote_default=lambda: ("other", "fork"))

        code = bootstrap_start.run_start(
            second, arguments(confirm=f"bootstrap_issue@{shown}", no_input=True))

        self.assertEqual(4, code)
        self.assertEqual([], self.writes(moved.calls))

    def test_the_repository_line_is_followed_by_a_blank_line(self):
        env, _ = self.env([], self.run_for())
        bootstrap_start.run_start(env, arguments(no_input=True))

        lines = output(env).splitlines()
        at = lines.index("Repository: acme/widgets")
        self.assertEqual("", lines[at + 1])
        self.assertTrue(lines[at + 2].startswith("Will "), lines[at + 2])

    def test_a_failed_develop_after_create_exits_two_and_never_creates_a_second_issue(self):
        run = self.run_for(extra={("gh", "issue", "develop"): bootstrap_github.CommandError(
            "boom", returncode=1)})

        code, env, _, calls = self.preview_then_confirm([], run)

        self.assertEqual(2, code)
        self.assertIn("bootstrap_issue failed: boom", problems(env))
        self.assertEqual(1, len([c for c in calls if c[:3] == ("gh", "issue", "create")]))

    def test_carried_over_files_that_change_after_the_preview_exit_four_with_no_writes(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        run = self.run_for(local=("main", "7-chore-bootstrap"), status=" M ROADMAP.md\0")
        first, _ = self.env([candidate], run)
        bootstrap_start.run_start(first, arguments(no_input=True))
        shown = re.search(r"confirm: bootstrap_issue@([0-9a-f]{12})", output(first)).group(1)
        self.assertIn("ROADMAP.md", output(first))
        run.answers[("git", "status", "--porcelain")] = " M ROADMAP.md\0 M project-context.toml\0"
        before = len(run.calls)
        second, _ = self.env([candidate], run)

        code = bootstrap_start.run_start(second, arguments(
            confirm=f"bootstrap_issue@{shown}", no_input=True))

        self.assertEqual(4, code)
        self.assertEqual([], self.writes(run.calls[before:]))

    def test_the_preview_names_the_fetch_only_for_a_branch_that_is_not_local(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        remote_only = self.run_for(local=("main",))
        local = self.run_for(local=("main", "7-chore-bootstrap"))
        first, _ = self.env([candidate], remote_only)
        second, _ = self.env([candidate], local)

        bootstrap_start.run_start(first, arguments(no_input=True))
        bootstrap_start.run_start(second, arguments(no_input=True))

        self.assertIn("git fetch origin 7-chore-bootstrap", output(first))
        self.assertNotIn("git fetch", output(second))

    def test_the_preview_names_git_switch_never_git_checkout(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        env, _ = self.env([candidate], self.run_for(local=("main", "7-chore-bootstrap")))

        bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertIn("`git switch 7-chore-bootstrap`", output(env))
        self.assertNotIn("git checkout", output(env))

    def test_an_issue_without_a_branch_is_only_developed(self):
        run = self.run_for()
        candidate = {"number": 7, "title": "Bootstrap", "branch": None}

        code, _, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(0, code)
        self.assertEqual([("gh", "issue", "develop", "7", "--base", "main", "--name",
                           "7-chore-bootstrap", "--checkout", "--repo", "acme/widgets")],
                         self.writes(calls))

    def test_an_existing_local_branch_is_checked_out_without_a_fetch(self):
        run = self.run_for(local=("main", "7-chore-bootstrap"))
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, _, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(0, code)
        self.assertEqual([("git", "switch", "7-chore-bootstrap")], self.writes(calls))

    def test_a_branch_only_on_github_is_fetched_then_checked_out(self):
        run = self.run_for(local=("main",))
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, _, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(0, code)
        self.assertEqual([("git", "fetch", "origin", "7-chore-bootstrap"),
                          ("git", "switch", "7-chore-bootstrap")], self.writes(calls))

    def test_the_chosen_candidate_of_several_is_the_one_acted_on(self):
        run = self.run_for(local=("main", "7-chore-bootstrap"))

        code, _, _, calls = self.preview_then_confirm(CANDIDATES, run, issue=8)

        self.assertEqual(0, code)
        self.assertEqual([("gh", "issue", "develop", "8", "--base", "main", "--name",
                           "8-chore-bootstrap", "--checkout", "--repo", "acme/widgets")],
                         self.writes(calls))

    def test_the_preview_names_untracked_files_as_carried_over_and_not_committed(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        run = self.run_for(local=("main", "7-chore-bootstrap"),
                           status="?? notes/a.md\0?? scratch.txt\0?? project-context.toml\0")
        first, _ = self.env([candidate], run)

        bootstrap_start.run_start(first, arguments(no_input=True))

        self.assertIn("Untracked files, carried over and not committed: notes/a.md, scratch.txt",
                      output(first))

    def test_the_preview_warns_about_carried_over_files_only_when_there_are_some(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        dirty = self.run_for(local=("main", "7-chore-bootstrap"),
                             status=" M ROADMAP.md\0?? project-context.toml\0")
        clean = self.run_for(local=("main", "7-chore-bootstrap"))
        first, _ = self.env([candidate], dirty)
        second, _ = self.env([candidate], clean)

        bootstrap_start.run_start(first, arguments(no_input=True))
        bootstrap_start.run_start(second, arguments(no_input=True))

        self.assertIn("ROADMAP.md", output(first))
        self.assertIn("project-context.toml", output(first))
        self.assertIn("already commits", output(first))
        self.assertIn("never stashes", output(first))
        self.assertNotIn("carried over", output(second).lower())

    def test_the_preview_says_what_will_be_done(self):
        for candidates, words in (
                ([], ("create", "CHORE:", "bootstrap marker", "develop")),
                ([{"number": 7, "title": "T", "branch": None}], ("develop", "7-chore-bootstrap")),
                ([{"number": 7, "title": "T", "branch": "7-chore-bootstrap"}], ("check out",))):
            with self.subTest(candidates=len(candidates), words=words):
                env, _ = self.env(candidates, self.run_for(local=("main", "7-chore-bootstrap")))

                bootstrap_start.run_start(env, arguments(no_input=True))

                for word in words:
                    self.assertIn(word.lower(), output(env).lower())

    def test_a_failed_checkout_exits_two_with_gits_message_and_never_stashes(self):
        message = "error: Your local changes would be overwritten by checkout"
        run = self.run_for(local=("main", "7-chore-bootstrap"), extra={
            ("git", "switch"): bootstrap_github.CommandError(message, returncode=1)})
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, env, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(2, code)
        self.assertIn(f"bootstrap_issue failed: {message}", problems(env))
        self.assertEqual([], [call for call in calls if call[:2] == ("git", "stash")])

    def test_a_failed_fetch_exits_two_and_does_not_check_out(self):
        run = self.run_for(local=("main",), extra={
            ("git", "fetch"): bootstrap_github.CommandError("fatal: no such ref", returncode=1)})
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}

        code, env, _, calls = self.preview_then_confirm([candidate], run)

        self.assertEqual(2, code)
        self.assertIn("bootstrap_issue failed: fatal: no such ref", problems(env))
        self.assertEqual([], [call for call in calls if call[:2] == ("git", "switch")])

    def test_an_issue_url_without_a_number_stops_before_developing(self):
        run = self.run_for(extra={("gh", "issue", "create"): "created\n"})

        code, env, _, calls = self.preview_then_confirm([], run)

        self.assertEqual(2, code)
        self.assertIn("issue number", problems(env))
        self.assertIn("bootstrap_issue failed:", problems(env))
        self.assertIn("probably created", problems(env))
        self.assertIn("finds it by its marker", problems(env))
        self.assertEqual([], [call for call in calls if call[:3] == ("gh", "issue", "develop")])

    def test_nothing_is_written_before_a_confirmation(self):
        candidate = {"number": 7, "title": "Bootstrap", "branch": "7-chore-bootstrap"}
        for candidates in ([], [candidate]):
            for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed"):
                with self.subTest(candidates=len(candidates), how=how):
                    run = self.run_for(status=" M ROADMAP.md\0")
                    env, _ = self.env(candidates, run, is_tty=how == "declined",
                                      prompt=prompter("n"))
                    args = {
                        "exit three": arguments(no_input=True),
                        "declined": arguments(),
                        "wrong digest": arguments(
                            confirm="bootstrap_issue@0123456789ab", no_input=True),
                        "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                        "malformed": arguments(confirm="bootstrap_issue", no_input=True),
                    }[how]

                    self.assertNotEqual(0, bootstrap_start.run_start(env, args))

                    self.assertEqual([], [call for call in run.calls if not is_read_only(call)])


PROJECT_CONTEXT = "project_context"
WARNING = "this plan has no rulesets for private repositories"
FLAGS = dict(description="A project", required_approvals=1, project_number=3)


class ProjectContextActionTest(unittest.TestCase):
    """The real `project_context` action (research D13, D14), over a temporary repository."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / "project-context.toml"

    def run_for(self, extra=None):
        answers = {
            ("git", "remote", "get-url"): "https://github.com/acme/widgets.git\n",
            ("gh", "project", "create"): '{"number": 5}',
        }
        answers.update(extra or {})
        return FakeRun(answers)

    def env(self, run=None, state="todo", performable=True, warning=WARNING, **kwargs):
        target = step(PROJECT_CONTEXT, state, performable, details={"ruleset_warning": warning},
                      action={"text": "Fix the file.", "command": None, "doc": "docs/x"})
        # a terminal run reads the report once more, after the yes
        reports = ([report(target), report(target), ALL_DONE] if kwargs.get("is_tty")
                   else [report(target), ALL_DONE])
        return make_env(
            root=self.root, run=run or self.run_for(), reports=reports,
            actions={PROJECT_CONTEXT: bootstrap_start.ACTIONS[PROJECT_CONTEXT]}, **kwargs)

    def start(self, args, **kwargs):
        env = self.env(**kwargs)
        return bootstrap_start.run_start(env, args), env

    def preview_then_confirm(self, flags, run=None):
        run = run or self.run_for()
        code, first = self.start(arguments(no_input=True, **flags), run=run)
        self.assertEqual(3, code)
        shown = re.search(r"confirm: project_context@([0-9a-f]{12})", output(first)).group(1)
        code, second = self.start(arguments(
            confirm=f"project_context@{shown}", no_input=True, **flags), run=run)
        return code, second, first, run

    def test_the_exit_three_command_carries_the_init_flags_and_the_digest(self):
        code, first = self.start(arguments(no_input=True, **FLAGS))

        self.assertEqual(3, code)
        lines = output(first).splitlines()
        command = lines[lines.index("To do it, run:") + 1]
        self.assertRegex(command, r"^python3 scripts/bootstrap\.py start --no-input "
                                  r"--confirm project_context@[0-9a-f]{12} ")
        for flag in ("--description 'A project'", "--project-number 3",
                     "--required-approvals 1"):
            self.assertIn(flag, command)

    def test_the_action_is_registered(self):
        self.assertIsInstance(bootstrap_start.ACTIONS[PROJECT_CONTEXT],
                              bootstrap_start.StepAction)

    def test_missing_flags_without_a_terminal_stop_with_exit_one_naming_every_one(self):
        names = {"description": "--description", "required_approvals": "--required-approvals",
                 "project_number": "--project-number"}
        for size in range(len(FLAGS)):  # every proper subset of the three flags
            for given in itertools.combinations(FLAGS, size):
                values = {key: FLAGS[key] for key in given}
                with self.subTest(given=given):
                    run = self.run_for()
                    code, env = self.start(arguments(no_input=True, **values), run=run)

                    self.assertEqual(1, code)
                    needs = next(line for line in output(env).splitlines()
                                 if line.startswith("Next step project_context needs "))
                    for key, name in names.items():
                        self.assertEqual(key not in values, name in needs)
                    if "project_number" not in values:
                        self.assertIn("--create-board", needs)
                    self.assertEqual("", problems(env))
                    self.assertNotIn("confirm:", output(env))
                    self.assertFalse(self.target.exists())
                    self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_a_terminal_with_no_input_never_prompts_and_stops_with_exit_one_for_missing_flags(
            self):
        code, env = self.start(arguments(no_input=True), is_tty=True)  # a prompt would raise

        self.assertEqual(1, code)
        self.assertIn("--description", output(env))
        self.assertFalse(self.target.exists())

    def test_the_preview_shows_every_value_and_the_ruleset_warning_and_writes_nothing(self):
        code, env = self.start(arguments(no_input=True, **FLAGS))

        self.assertEqual(3, code)
        text = output(env)
        for value in ("A project", "widgets", "acme", "required_approvals = 1", "#3", WARNING):
            self.assertIn(value, text)
        self.assertFalse(self.target.exists())

    def test_an_owner_that_cannot_be_derived_is_a_missing_flag_without_a_terminal(self):
        run = self.run_for()

        code, env = self.start(arguments(no_input=True, **FLAGS), run=run,
                               remote_default=lambda: None)

        self.assertEqual(1, code)
        self.assertIn("--owner", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertFalse(self.target.exists())
        self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_a_terminal_asks_for_an_owner_it_cannot_derive_and_names_the_project_after_the_folder(
            self):
        run = self.run_for({("git", "remote", "get-url"): bootstrap_github.CommandError(
            "no remote", returncode=2)})
        prompt = prompter("A project", "acme", "3", "1", "y")

        code, _ = self.start(arguments(), run=run, is_tty=True, prompt=prompt,
                             remote_default=lambda: None)

        self.assertEqual(0, code)
        self.assertIn("owner", prompt.asked[1].lower())
        context = project_context.load_context(self.target)
        self.assertEqual("acme", context["github_project"]["owner"])
        self.assertEqual(self.root.name, context["project"]["name"])

    def test_a_board_to_create_is_named_in_the_preview(self):
        code, env = self.start(arguments(no_input=True, description="A", required_approvals=0,
                                         create_board=True))

        self.assertEqual(3, code)
        self.assertIn("create", output(env).lower())
        self.assertIn("new board", output(env).lower())

    def test_without_a_warning_the_preview_has_none(self):
        _, env = self.start(arguments(no_input=True, **FLAGS), warning=None)

        self.assertNotIn("warning", output(env).lower())

    def test_the_digest_from_exit_three_is_accepted_and_the_file_is_written(self):
        code, env, _, run = self.preview_then_confirm(FLAGS)

        self.assertEqual(0, code)
        self.assertEqual(f"Done: project_context: Wrote {self.target}.",
                         output(env).splitlines()[0])
        context = project_context.load_context(self.target)
        self.assertEqual("A project", context["project"]["description"])
        self.assertEqual(1, context["review"]["required_approvals"])
        self.assertEqual(3, context["github_project"]["number"])
        self.assertEqual("acme", context["github_project"]["owner"])

    def test_a_created_board_number_is_written(self):
        flags = dict(description="A", required_approvals=0, create_board=True)

        code, _, _, run = self.preview_then_confirm(flags)

        self.assertEqual(0, code)
        self.assertEqual(5, project_context.load_context(self.target)["github_project"]["number"])
        self.assertEqual(1, len([c for c in run.calls if c[:3] == ("gh", "project", "create")]))

    def test_changed_flag_values_between_preview_and_confirmation_exit_four(self):
        run = self.run_for()
        _, first = self.start(arguments(no_input=True, **FLAGS), run=run)
        shown = re.search(r"confirm: project_context@([0-9a-f]{12})", output(first)).group(1)
        changed = dict(FLAGS, required_approvals=2)

        code, _ = self.start(arguments(confirm=f"project_context@{shown}", no_input=True,
                                       **changed), run=run)

        self.assertEqual(4, code)
        self.assertFalse(self.target.exists())

    def test_the_action_never_passes_force_and_refuses_an_existing_file(self):
        self.target.write_text("[project]\n")
        env = self.env()
        env.args = arguments(no_input=True, **FLAGS)

        with self.assertRaises(bootstrap_start.StepFailed) as raised:
            bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

        self.assertIn("already exists", str(raised.exception))
        self.assertEqual("[project]\n", self.target.read_text())

    def test_init_is_the_injected_one_and_is_called_without_force(self):
        calls = []
        env = self.env(init=lambda *args: calls.append(args))
        env.args = arguments(no_input=True, **FLAGS)

        bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

        (namespace, root, run, tty, _), = calls
        self.assertFalse(namespace.force)
        self.assertTrue(namespace.yes)
        self.assertEqual((self.root, env.run, False), (root, run, tty))

    def test_only_the_wrote_line_of_inits_output_is_the_result(self):
        def noisy(args, root, run, is_tty, prompt):
            print("Debug noise")
            print(f"Wrote {root / 'project-context.toml'}")
            print()
            print("Next steps:\n  1. something else")

        env = self.env(init=noisy)
        env.args = arguments(no_input=True, **FLAGS)

        result = bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

        self.assertEqual(f"Wrote {self.target}", result)
        self.assertEqual("", output(env))

    def test_perform_never_lets_init_fill_in_a_decision_value(self):
        calls = []
        cases = (
            ({}, None, ("--description", "--required-approvals", "--project-number")),
            (dict(description="A", required_approvals=1), None, ("--project-number",)),
            (dict(description="A", project_number=3), None, ("--required-approvals",)),
            (dict(required_approvals=1, project_number=3), None, ("--description",)),
            (dict(FLAGS), (lambda: None), ("--owner",)),
        )
        for flags, remote, expected in cases:
            with self.subTest(flags=sorted(flags), remote=remote is None):
                kwargs = {} if remote is None else {"remote_default": remote}
                env = self.env(init=lambda *args: calls.append(args), **kwargs)
                env.args = arguments(no_input=True, **flags)

                with self.assertRaises(bootstrap_start.StepFailed) as raised:
                    bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))

                self.assertEqual(2, raised.exception.code)
                self.assertIn("project_context failed:", str(raised.exception))
                for flag in expected:
                    self.assertIn(flag, str(raised.exception))
        self.assertEqual([], calls)

    def test_a_missing_flag_is_reported_before_an_invalid_value(self):
        for args in (arguments(no_input=True, required_approvals=-1, project_number=3),
                     arguments(confirm="project_context@0123456789ab", no_input=True,
                               required_approvals=-1, project_number=3)):
            with self.subTest(confirm=args.confirm):
                code, env = self.start(args)

                # a plain run stops on stdout (exit 1); a confirmed one is a usage error (exit 2)
                self.assertEqual(2 if args.confirm else 1, code)
                self.assertIn("--description", problems(env) + output(env))
                # the decisions list now also gives the reason of a plain run (issue #56)
                self.assertEqual(not args.confirm,
                                 "non-negative" in problems(env) + output(env))

    def test_a_terminal_without_a_remote_asks_again_after_an_empty_owner(self):
        run = self.run_for({("git", "remote", "get-url"): bootstrap_github.CommandError(
            "no remote", returncode=2)})
        prompt = prompter("A project", "", "acme", "3", "1", "y")

        code, env = self.start(arguments(), run=run, is_tty=True, prompt=prompt,
                               remote_default=lambda: None)

        self.assertEqual(0, code)
        self.assertEqual(6, len(prompt.asked))
        self.assertEqual(prompt.asked[1], prompt.asked[2])
        self.assertIn("--owner", output(env))
        self.assertEqual("acme", project_context.load_context(
            self.target)["github_project"]["owner"])

    def test_the_repository_remote_is_read_once_per_run(self):
        reads = []

        def remote():
            reads.append(1)
            return ("acme", "widgets")

        code, _ = self.start(arguments(no_input=True, **FLAGS), remote_default=remote)

        self.assertEqual(3, code)
        self.assertEqual(1, len(reads))

    def test_missing_injected_functions_give_a_clear_error(self):
        env = self.env(validate_context=None)
        code = bootstrap_start.run_cli(env, ["--no-input", "--description", "A",
                                             "--required-approvals", "1", "--project-number", "3"])
        self.assertEqual(2, code)
        self.assertIn("bootstrap start failed: Env.validate_context was not supplied",
                      problems(env))
        self.assertNotIn("project_context failed:", problems(env))

        env = self.env(init=None)
        env.args = arguments(no_input=True, **FLAGS)
        with self.assertRaises(ValueError) as raised:
            bootstrap_start.ACTIONS[PROJECT_CONTEXT].perform(env, step(PROJECT_CONTEXT))
        self.assertIn("Env.init", str(raised.exception))

    def test_strings_are_previewed_exactly_as_they_are_written_to_the_file(self):
        fake = "confirm: project_context@000000000000"
        description = f'A "q" \\ {fake} ä日'  # single line: control characters are rejected
        flags = dict(FLAGS, description=description)
        run = self.run_for()
        code, first = self.start(arguments(no_input=True, **flags), run=run)
        self.assertEqual(3, code)
        shown = re.search(r"^confirm: project_context@([0-9a-f]{12})$", output(first),
                          re.MULTILINE).group(1)

        confirmed, _ = self.start(arguments(confirm=f"project_context@{shown}", no_input=True,
                                            **flags), run=run)

        self.assertEqual(0, confirmed)
        lines = output(first).split("\n")
        self.assertEqual([f"confirm: project_context@{shown}"],
                         [line for line in lines if line.startswith("confirm: ")])
        self.assertNotIn("\x1b", output(first))
        rendered = f"description = {bootstrap.toml_string(description)}"
        self.assertIn(f"  {rendered}", lines)
        self.assertIn(rendered, self.target.read_text())
        self.assertEqual(description, project_context.load_context(
            self.target)["project"]["description"])

    def test_a_name_with_control_characters_exits_two_before_any_preview(self):
        name = "a\nconfirm: project_context@000000000000\x1b[31m"
        flags = dict(description="A", required_approvals=1, create_board=True, name=name)

        code, env = self.start(arguments(no_input=True, **flags))

        self.assertEqual(2, code)
        self.assertIn("--name", problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertNotIn("\x1b", output(env) + problems(env))

    def test_a_description_with_a_c1_or_separator_character_exits_two_naming_the_flag(self):
        for character in ("\x85", "\u2028", "\u2029", "\x7f", "\t", "\n"):
            for label, extra in (("preview", {}),
                                 ("confirmation", {"confirm": "project_context@0123456789ab"})):
                with self.subTest(character=repr(character), run=label):
                    run = self.run_for()
                    flags = dict(FLAGS, description=f"a{character}b")

                    code, env = self.start(
                        arguments(no_input=True, **flags, **extra), run=run)

                    self.assertEqual(2, code)
                    self.assertIn("--description", problems(env))
                    self.assertNotIn("confirm:", output(env))
                    self.assertFalse(self.target.exists())
                    self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_an_invalid_value_with_a_flag_missing_is_one_more_decision_never_echoed(self):
        for flag, key in (("--description", "description"), ("--name", "name")):
            for character in ("\n", "\x85"):
                with self.subTest(flag=flag, character=repr(character)):
                    flags = {"description": "A", "required_approvals": 1,
                             key: f"a{character}b"}

                    code, env = self.start(arguments(no_input=True, **flags))

                    shown = output(env) + problems(env)
                    self.assertEqual(1, code)
                    self.assertIn("needs your decisions:", output(env))
                    self.assertRegex(output(env), rf"decisions: .*{flag}")
                    self.assertIn(f"{flag} '<{key}>'", output(env))
                    self.assertNotIn("$'", shown)
                    self.assertNotIn("confirm:", shown)
                    self.assertNotIn("a" + character + "b", shown)
                    self.assertIsNone(re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029]", shown))

    INVALID_FLAGS = (
        ("--name", dict(name="a\U0000202eb"), project_context.SINGLE_LINE_MESSAGE),
        ("--description", dict(description="a\U00002066b"), project_context.SINGLE_LINE_MESSAGE),
        ("--owner", dict(owner="a\x85b"), "must be a GitHub owner login"),
        ("--required-approvals", dict(required_approvals=-1), "must be a non-negative integer"),
        ("--project-number", dict(project_number=0), "must be a positive integer"),
    )

    def test_each_invalid_flag_with_a_missing_one_is_listed_with_its_message_and_no_value(self):
        for flag, bad, message in self.INVALID_FLAGS:
            with self.subTest(flag=flag):
                key = next(iter(bad))
                # leave one other decision missing, so the run stops at the decisions list
                flags = {name: value for name, value in dict(FLAGS, owner="acme").items()
                         if name not in (key, "description" if key != "description"
                                         else "required_approvals")}
                missing = "--description" if key != "description" else "--required-approvals"
                run = self.run_for()

                code, env = self.start(arguments(no_input=True, **flags, **bad), run=run)

                lines = output(env).splitlines()
                shown = output(env) + problems(env)
                self.assertEqual(1, code, shown)
                decisions = lines.index(
                    f"Next step project_context needs your decisions: {missing}, {flag}")
                self.assertEqual(f"{flag}: {message}", lines[decisions + 1])
                self.assertEqual("To give them, run:", lines[decisions + 2])
                command = lines[decisions + 3]
                self.assertIn(bootstrap_start.INIT_PLACEHOLDERS[flag], command)
                value = str(next(iter(bad.values())))
                self.assertNotIn(value, shown)
                self.assertNotIn("confirm:", shown)
                self.assertEqual("", problems(env))
                self.assertIsNone(re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029"
                                            r"\u202a-\u202e\u2066-\u2069]", shown))
                self.assertFalse(self.target.exists())
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_reason_uses_the_requirement_the_error_carries(self):
        error = project_context.InvalidValue("--name", "'a'", "must be one line")

        self.assertEqual("must be one line",
                         bootstrap_start.ProjectContextAction._reason(error, "--name", "a"))

    def test_reason_for_a_plain_value_error_drops_the_leading_key(self):
        reason = bootstrap_start.ProjectContextAction._reason
        for message, expected in (("project.name must be one line", "must be one line"),
                                  ("review.required_approvals must be a non-negative integer",
                                   "must be a non-negative integer"),
                                  ("nospaces", "is not valid"), ("", "is not valid")):
            with self.subTest(message=message):
                self.assertEqual(expected, reason(ValueError(message), "--name", "a"))

    def test_reason_for_an_error_with_an_empty_requirement_falls_back(self):
        error = project_context.InvalidValue("--name", "'a'", "")

        self.assertEqual("is not valid",
                         bootstrap_start.ProjectContextAction._reason(error, "--name", "a"))

    def test_several_invalid_flags_get_one_line_each_in_flag_order(self):
        flags = dict(name="a\U0000202eb", required_approvals=-1, project_number=0)

        code, env = self.start(arguments(no_input=True, **flags))

        lines = output(env).splitlines()
        self.assertEqual(1, code)
        decisions = lines.index("Next step project_context needs your decisions: "
                                "--description, --name, --project-number, --required-approvals")
        self.assertEqual(
            ["--name: " + project_context.SINGLE_LINE_MESSAGE,
             "--project-number: must be a positive integer",
             "--required-approvals: must be a non-negative integer"],
            lines[decisions + 1:decisions + 4])

    def test_a_bidi_control_exits_two_naming_the_flag_when_nothing_is_missing(self):
        for flag, key in (("--name", "name"), ("--description", "description"),
                          ("--owner", "owner")):
            for character in ("\U0000202a", "\U0000202e", "\U00002066", "\U00002069"):
                with self.subTest(flag=flag, character=hex(ord(character))):
                    flags = dict(FLAGS, **{key: f"a{character}b"})

                    code, env = self.start(arguments(no_input=True, **flags))

                    self.assertEqual(2, code)
                    self.assertIn(flag, problems(env))
                    self.assertNotIn("confirm:", output(env))
                    self.assertFalse(self.target.exists())

    def test_an_invalid_value_is_never_printed_nor_its_repr_on_either_path(self):
        bidi = chr(0x202E)
        cases = (
            ("--name", "name", f"secret{bidi}Name", "secret"),
            ("--required-approvals", "required_approvals", -1, "-1"),
        )
        for flag, key, value, secret in cases:
            for confirm in (None, "project_context@000000000000"):
                with self.subTest(flag=flag, confirm=confirm):
                    flags = dict(FLAGS, **{key: value})

                    code, env = self.start(arguments(no_input=True, confirm=confirm, **flags))

                    shown = output(env) + problems(env)
                    self.assertEqual(2, code)
                    self.assertIn(f"project_context failed: {flag}: ", problems(env))
                    self.assertNotIn(secret, shown)
                    self.assertNotIn(repr(value), shown)
                    self.assertNotIn(bidi, shown)

    def test_a_too_long_description_exits_two_with_the_length_message_and_is_never_printed(self):
        description = "secret" + "d" * 345
        for confirm in (None, "project_context@000000000000"):
            with self.subTest(confirm=confirm):
                flags = dict(FLAGS, description=description)

                code, env = self.start(arguments(no_input=True, confirm=confirm, **flags))

                shown = output(env) + problems(env)
                self.assertEqual(2, code)
                self.assertIn("--description: " + project_context.DESCRIPTION_TOO_LONG_MESSAGE,
                              problems(env))
                self.assertNotIn(project_context.SINGLE_LINE_MESSAGE, shown)
                self.assertNotIn("secret", shown)
                self.assertNotIn("confirm:", output(env))

    def test_several_invalid_flags_are_all_named_joined_with_semicolons(self):
        flags = dict(FLAGS, name=f"a{chr(0x202E)}b", description="")

        code, env = self.start(arguments(no_input=True, **flags))

        self.assertEqual(2, code)
        self.assertRegex(problems(env), r"failed: --name: [^;]+; --description: ")

    def test_a_hostile_name_without_control_characters_is_escaped_in_the_name_and_board_lines(self):
        name = 'a confirm: project_context@000000000000 "q" \\ ä'
        flags = dict(description="A", required_approvals=1, create_board=True, name=name)

        code, env = self.start(arguments(no_input=True, **flags))

        self.assertEqual(3, code)
        lines = output(env).split("\n")
        self.assertEqual(1, len([line for line in lines if line.startswith("confirm: ")]))
        quoted = bootstrap.toml_string(name)
        self.assertIn(f"  name = {quoted}", lines)
        self.assertIn(f"  project board = a new board named {quoted} will be created", lines)

    def test_an_invalid_value_exits_two_on_the_preview_run_with_no_preview(self):
        for label, flags, expected in (
                ("negative approvals", dict(FLAGS, required_approvals=-1), "--required-approvals"),
                ("empty description", dict(FLAGS, description=""), "--description"),
                ("empty owner", dict(FLAGS, owner=""), "--owner"),
                ("zero board", dict(FLAGS, project_number=0), "--project-number")):
            with self.subTest(label):
                run = self.run_for()

                code, env = self.start(arguments(no_input=True, **flags), run=run)

                self.assertEqual(2, code)
                self.assertIn(expected, problems(env))
                self.assertNotIn("confirm:", output(env))
                self.assertFalse(self.target.exists())
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_an_invalid_value_also_stops_a_confirmation_and_a_terminal_flag(self):
        flags = dict(FLAGS, required_approvals=-1)

        code, env = self.start(arguments(confirm="project_context@0123456789ab", no_input=True,
                                         **flags))
        self.assertEqual(2, code)
        self.assertIn("--required-approvals", problems(env))

        code, env = self.start(arguments(**flags), is_tty=True)  # a prompt would raise
        self.assertEqual(2, code)
        self.assertIn("--required-approvals", problems(env))
        self.assertFalse(self.target.exists())

    def test_a_terminal_asks_again_after_a_description_with_a_control_character(self):
        prompt = prompter("bad\x85value", "Fine", "", "", "y")

        code, env = self.start(arguments(), is_tty=True, prompt=prompt)

        self.assertEqual(0, code)
        self.assertIn("--description", output(env))
        self.assertIn("single-line", output(env))
        self.assertEqual("Fine", project_context.load_context(
            self.target)["project"]["description"])

    def test_a_terminal_asks_for_the_missing_values_and_offers_zero_approvals(self):
        prompt = prompter("A project", "", "", "y")

        code, env = self.start(arguments(), is_tty=True, prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(4, len(prompt.asked))
        self.assertIn("approvals", prompt.asked[2].lower())
        self.assertIn("[0]", prompt.asked[2])
        self.assertEqual("Proceed? [y/N] ", prompt.asked[3])
        context = project_context.load_context(self.target)
        self.assertEqual(0, context["review"]["required_approvals"])
        self.assertEqual(5, context["github_project"]["number"])  # empty answer: create a board

    def test_a_terminal_preview_shows_the_answers_before_the_yes(self):
        prompt = prompter("A project", "7", "2", "n")

        code, env = self.start(arguments(), is_tty=True, prompt=prompt)

        self.assertEqual(1, code)
        self.assertIn("A project", output(env))
        self.assertIn("required_approvals = 2", output(env))
        self.assertIn("#7", output(env))
        self.assertFalse(self.target.exists())

    def test_each_value_prompt_flushes_input_first(self):
        events = []
        answers = prompter("A project", "", "", "y")

        def asking(text):
            events.append("prompt")
            return answers(text)

        code, _ = self.start(arguments(), is_tty=True, prompt=asking,
                             flush_input=lambda: events.append("flush"))

        self.assertEqual(0, code)
        self.assertEqual(["flush", "prompt"] * 4, events)

    def test_a_terminal_does_not_ask_for_values_given_as_flags(self):
        prompt = prompter("1", "y")

        code, _ = self.start(arguments(description="A", project_number=3), is_tty=True,
                             prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(2, len(prompt.asked))
        self.assertIn("approvals", prompt.asked[0].lower())

    def test_a_terminal_asks_again_after_an_empty_or_invalid_answer(self):
        # description: empty, then valid; board: "x", "0", then 3; approvals: "many", "-1", then 2
        prompt = prompter("", "A project", "x", "0", "3", "many", "-1", "2", "y")

        code, env = self.start(arguments(), is_tty=True, prompt=prompt)

        self.assertEqual(0, code)
        self.assertEqual(9, len(prompt.asked))
        self.assertEqual(prompt.asked[0], prompt.asked[1])  # the same question again
        self.assertIn("whole number", output(env))
        self.assertIn("--required-approvals", output(env))
        context = project_context.load_context(self.target)
        self.assertEqual((2, 3, "A project"), (
            context["review"]["required_approvals"], context["github_project"]["number"],
            context["project"]["description"]))

    def test_an_empty_approvals_answer_still_accepts_the_offered_zero(self):
        code, env = self.start(arguments(description="A", project_number=3), is_tty=True,
                               prompt=prompter("", "y"))

        self.assertEqual(0, code)
        self.assertIn("required_approvals = 0", output(env))  # shown, not left to init's default
        self.assertEqual(0, project_context.load_context(
            self.target)["review"]["required_approvals"])

    def test_end_of_input_at_a_prompt_exits_two_with_the_flag_to_pass(self):
        def closed(text):
            raise EOFError

        code, env = self.start(arguments(), is_tty=True, prompt=closed)

        self.assertEqual(2, code)
        self.assertIn("project_context failed: input ended: pass --description", problems(env))
        self.assertFalse(self.target.exists())

    def test_an_invalid_existing_file_stops_with_exit_one_before_any_missing_flag(self):
        for args in (arguments(no_input=True), arguments(confirm="project_context@0123456789ab",
                                                        no_input=True)):
            with self.subTest(confirm=args.confirm):
                code, env = self.start(args, performable=False)

                self.assertEqual(1, code)
                self.assertIn("Fix the file.", output(env))
                self.assertEqual("", problems(env))
                self.assertFalse(self.target.exists())

    def test_nothing_is_written_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed",
                    "missing flags"):
            with self.subTest(how=how):
                run = self.run_for()
                args = {
                    "exit three": arguments(no_input=True, **FLAGS),
                    "declined": arguments(**FLAGS),
                    "wrong digest": arguments(confirm="project_context@0123456789ab",
                                              no_input=True, **FLAGS),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True, **FLAGS),
                    "malformed": arguments(confirm="project_context", no_input=True, **FLAGS),
                    "missing flags": arguments(no_input=True),
                }[how]

                code, _ = self.start(args, run=run, is_tty=how == "declined",
                                     prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertFalse(self.target.exists())
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])


GITHUB_SETTINGS = "github_settings"
DENIED = "gh: Resource not accessible by personal access token (HTTP 403)"
PLAN_DENIED = ("gh: Upgrade to GitHub Pro or make this repository public to enable this "
               "feature. (HTTP 403)")


class SettingsRun:
    """git answers and the branch tip for the digest; every other call goes to FakeGitHub.

    `deny` makes the ruleset write fail with that text and counts each attempt; `default_context`
    is the default branch's project-context.toml (None: not there); `fail` maps an argv prefix to
    the error text a read of it raises.
    """

    def __init__(self, github, deny=None, default_context=None, fail=None):
        self.github, self.deny, self.default_context = github, deny, default_context
        self.fail = fail or {}
        self.calls, self.ruleset_attempts = [], 0

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append(tuple(argv))
        for prefix, text in self.fail.items():
            if tuple(argv[:len(prefix)]) == prefix:
                raise bootstrap_github.CommandError(text)
        if argv[:2] == ["gh", "api"] and any("/contents/" in part for part in argv):
            if self.default_context is None:
                raise bootstrap_github.CommandError("gh: Not Found (HTTP 404)")
            return self.default_context
        if argv[0] == "git":
            return "head000\n" if argv[1:3] == ["rev-parse", "HEAD"] else "main\n"
        if argv[:2] == ["gh", "api"] and "/branches/" in argv[2]:
            return "tip000\n"
        if argv[:2] == ["gh", "api"] and argv[2:4] == ["-X", "POST"] and "/rulesets" in argv[4]:
            self.ruleset_attempts += 1
            if self.deny:
                raise bootstrap_github.CommandError(self.deny)
        return self.github(argv, cwd=cwd)


class GitHubSettingsActionTest(unittest.TestCase):
    """The real `github_settings` action (T042, T043): the `github` plan, then `github --apply`."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=1))
        self.github = test_bootstrap_github.FakeGitHub()
        self.run = SettingsRun(self.github)

    def start(self, args, run=None, reports=None, merged=False, **kwargs):
        target = step(GITHUB_SETTINGS, action={
            "text": "Preview the changes, then apply them.",
            "command": "python3 scripts/bootstrap.py github", "doc": "docs/x"})
        reports = reports or [report(target), ALL_DONE]
        if merged is not None:  # None: the reports already say whether bootstrap is merged
            reports = [{**each, "bootstrap_merged": merged} for each in reports]
        env = make_env(
            root=self.root, run=run or self.run, reports=reports,
            actions={GITHUB_SETTINGS: bootstrap_start.ACTIONS[GITHUB_SETTINGS]}, **kwargs)
        return bootstrap_start.run_start(env, args), env

    def preview(self, **kwargs):
        code, env = self.start(arguments(no_input=True), **kwargs)
        self.assertEqual(3, code)
        digest = re.search(r"confirm: github_settings@([0-9a-f]{12})", output(env)).group(1)
        return digest, env

    def confirm(self, digest, **kwargs):
        return self.start(arguments(confirm=f"github_settings@{digest}", no_input=True), **kwargs)

    def github_output(self):
        """What `bootstrap.py github` prints for the same state, and nothing else."""
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            bootstrap.main(["github"], root=self.root, run=self.github, is_tty=False)
        return text.getvalue()

    def test_the_action_is_registered(self):
        self.assertIsInstance(bootstrap_start.ACTIONS[GITHUB_SETTINGS],
                              bootstrap_start.StepAction)
        self.assertIsNot(bootstrap_start.ACTIONS[GITHUB_SETTINGS], bootstrap_start._DEFAULT_ACTION)

    def test_the_preview_is_the_github_plan_without_its_apply_hint(self):
        plan = self.github_output()
        self.assertIn("Labels: create", plan)

        _, env = self.preview()

        # the plan without its apply hint and its credential line (shown after the apply)
        wanted = plan.split("\nBoard credential:")[0].rstrip("\n")
        self.assertIn(wanted, output(env))
        self.assertNotIn("Preview only", output(env))
        self.assertIn("Action: Preview the changes, then apply them.", output(env))

    def test_the_preview_has_no_apply_hint_and_ends_with_no_blank_line(self):
        env = make_env(root=self.root, run=self.run)
        action = bootstrap_start.ACTIONS[GITHUB_SETTINGS]

        lines = action.preview(env, step(GITHUB_SETTINGS))

        self.assertNotEqual("", lines[-1])
        self.assertFalse(any("run with --apply" in line for line in lines))
        self.assertTrue(any(line.startswith("Labels: create") for line in lines))

    def test_the_preview_has_no_manual_step_and_no_command_line(self):
        _, env = self.preview()

        self.assertNotIn("Manual step", output(env))
        self.assertNotIn("Command:", output(env))

    def test_a_preview_with_nothing_to_change_has_no_manual_step_either(self):
        with contextlib.redirect_stdout(io.StringIO()):
            bootstrap.main(["github", "--apply"], root=self.root, run=self.github, is_tty=False)

        _, env = self.preview()

        self.assertIn("Nothing to change", output(env))
        self.assertNotIn("Manual step", output(env))

    def test_a_confirmed_step_prints_the_manual_step_after_done_in_guided_mode_too(self):
        digest, _ = self.preview()

        code, env = self.start(arguments(
            guided=True, confirm=f"github_settings@{digest}", no_input=True))

        self.assertEqual(0, code)
        lines = output(env).splitlines()
        self.assertRegex(lines[0], r"^Done: github_settings: ")
        self.assertEqual("", lines[1])
        self.assertTrue(lines[2].startswith("Manual step (the API cannot read or set it)"))
        self.assertEqual(GUIDED_DONE, lines[-1])

    CREDENTIAL_TITLE = (
        "Board credential not configured (optional; organization secrets not checked)")
    CREDENTIAL_DOC = "docs/bootstrap.md#board-credentials"

    def with_credential(self, state="optional"):
        target = step(GITHUB_SETTINGS, action={
            "text": "Preview the changes, then apply them.",
            "command": "python3 scripts/bootstrap.py github", "doc": "docs/x"})
        credential = step("board_credential", state, title=self.CREDENTIAL_TITLE,
                          action={"text": "Add one.", "command": None,
                                  "doc": self.CREDENTIAL_DOC})
        return [report(target, credential), ALL_DONE]

    def test_a_confirmed_step_prints_the_credential_note_once_after_the_manual_line(self):
        reports = self.with_credential()
        digest, _ = self.preview(reports=reports)

        code, env = self.start(arguments(
            guided=True, confirm=f"github_settings@{digest}", no_input=True), reports=reports)

        self.assertEqual(0, code)
        lines = output(env).splitlines()
        self.assertEqual(1, output(env).count(self.CREDENTIAL_TITLE))
        self.assertEqual(1, output(env).count(self.CREDENTIAL_DOC))
        manual = next(i for i, line in enumerate(lines) if line.startswith("Manual step"))
        note = lines.index(self.CREDENTIAL_TITLE)
        self.assertLess(manual, note)
        self.assertLess(note, lines.index(GUIDED_DONE))
        self.assertIn(self.CREDENTIAL_DOC, lines[note + 1])

    def test_a_done_credential_prints_no_note(self):
        reports = self.with_credential("done")
        digest, _ = self.preview(reports=reports)

        _, env = self.start(arguments(
            confirm=f"github_settings@{digest}", no_input=True), reports=reports)

        self.assertIn("Done: github_settings", output(env))
        self.assertNotIn(self.CREDENTIAL_TITLE, output(env))
        self.assertNotIn(self.CREDENTIAL_DOC, output(env))

    def test_the_preview_run_does_not_print_the_credential_note(self):
        _, env = self.preview(reports=self.with_credential())

        self.assertNotIn(self.CREDENTIAL_TITLE, output(env))

    def test_the_github_subcommand_still_prints_its_hint_by_default(self):
        self.assertIn("Preview only; run with --apply", self.github_output())

    def test_a_real_digest_is_accepted_when_nothing_changed(self):
        digest, _ = self.preview()

        code, env = self.confirm(digest)

        self.assertEqual(0, code)
        lines = output(env).splitlines()  # a confirm run prints no preview: only the action
        self.assertEqual(5, len(lines), lines)
        self.assertRegex(lines[0], r"^Done: github_settings: Applied \d+ change\(s\)\.$")
        self.assertEqual("", lines[1])
        self.assertTrue(lines[2].startswith("Manual step (the API cannot read or set it)"))
        self.assertEqual(["", "BOOTSTRAP IS COMPLETE"], lines[3:])
        self.assertEqual([bootstrap_github.RULESET_NAME],
                         [ruleset["name"] for ruleset in self.github.rulesets])
        self.assertIsNotNone(self.github.find_label(bootstrap_github.LABELS[0][0]))

    def test_a_plan_that_changed_since_the_preview_exits_four_and_applies_nothing(self):
        digest, _ = self.preview()
        name, color, description = bootstrap_github.LABELS[0]
        self.github.labels[name] = (color, description)  # one label line disappears from the plan

        code, env = self.confirm(digest)

        self.assertEqual(4, code)
        self.assertIn("changed", problems(env))
        self.assertEqual([], self.github.writes)

    def test_progress_says_how_many_settings_are_being_applied(self):
        digest, first = self.preview()
        changes = len([line for line in output(first).splitlines()
                       if line.split(":")[0] in bootstrap_github.AREAS])
        self.assertGreater(changes, 1)

        code, env = self.confirm(digest)

        self.assertEqual(0, code)
        self.assertEqual([f"Applying {changes} GitHub settings…"],
                         [line for line in progress(env) if not line.startswith("Reading ")])
        self.assertIn(f"Applied {changes} change(s)", output(env))

    def test_the_count_comes_from_the_plan_not_from_printed_lines(self):
        real = bootstrap_github.plan_settings
        planned = []

        def with_warning(*args, **kwargs):
            plan, url = real(*args, **kwargs)
            plan.warnings.append("Ruleset: weakening refused")  # looks like a change line
            planned.append(len(plan.lines))
            return plan, url

        with mock.patch.object(bootstrap_github, "plan_settings", side_effect=with_warning):
            digest, _ = self.preview()
            code, env = self.confirm(digest)

        self.assertEqual(0, code, problems(env))
        self.assertEqual([f"Applying {planned[0]} GitHub settings…"],
                         [line for line in progress(env) if not line.startswith("Reading ")])

    def test_one_preview_reads_the_plan_once_and_the_count_matches_the_shown_lines(self):
        real = bootstrap_github.plan_settings
        with mock.patch.object(bootstrap_github, "plan_settings", wraps=real) as spy:
            digest, first = self.preview()

        self.assertEqual(1, spy.call_count)
        reads = [c for c in self.run.calls if c[:3] == ("gh", "label", "list")]
        self.assertEqual(1, len(reads))
        shown = len([line for line in output(first).splitlines()
                     if line.split(":")[0] in bootstrap_github.AREAS])
        code, env = self.confirm(digest)
        self.assertEqual(0, code, problems(env))
        self.assertEqual([f"Applying {shown} GitHub settings…"],
                         [line for line in progress(env) if not line.startswith("Reading ")])

    def test_the_preview_leaves_out_the_credential_line_the_note_after_done_has_it(self):
        self.assertIn("Board credential:", self.github_output())

        _, env = self.preview()

        self.assertNotIn("Board credential", output(env))
        self.assertNotIn("Manual step", output(env))

    def test_one_setting_is_singular_and_none_prints_no_applying_line(self):
        action = bootstrap_start.ACTIONS[GITHUB_SETTINGS]
        for count, expected in ((1, ["Applying 1 GitHub setting…"]), (0, []),
                                (2, ["Applying 2 GitHub settings…"])):
            with self.subTest(count=count):
                env = make_env(root=self.root, run=self.run)
                env.session[GITHUB_SETTINGS] = count

                action.perform(env, step(GITHUB_SETTINGS))

                self.assertEqual(expected, progress(env))

    def test_progress_has_no_count_when_the_plan_is_not_known(self):
        env = make_env(root=self.root, run=self.run)  # perform without a preview in this run

        bootstrap_start.ACTIONS[GITHUB_SETTINGS].perform(env, step(GITHUB_SETTINGS))

        self.assertEqual(["Applying GitHub settings…"], progress(env))

    def test_the_confirmed_action_is_apply_and_never_allow_weaken(self):
        digest, _ = self.preview()
        real = bootstrap_github.run_github

        with mock.patch.object(bootstrap_github, "run_github", wraps=real) as spy:
            code, _ = self.confirm(digest)

        self.assertEqual(0, code)
        modes = [(c.args[0].check, c.args[0].apply, c.args[0].allow_weaken)
                 for c in spy.call_args_list]
        self.assertEqual(1, modes.count((False, True, False)))
        self.assertEqual(modes[-1], (False, True, False))  # the apply comes last
        self.assertTrue(all(allow_weaken is False for _, _, allow_weaken in modes))
        self.assertTrue(all(not check for check, _, _ in modes))

    def test_a_ruleset_denial_reports_the_likely_cause_and_is_not_retried(self):
        for text, cause in ((DENIED, "repository admin rights"),
                            (PLAN_DENIED, "GitHub plan that supports them")):
            with self.subTest(text=text):
                self.github = test_bootstrap_github.FakeGitHub()
                run = SettingsRun(self.github, deny=text)
                digest, _ = self.preview(run=run)

                code, env = self.confirm(digest, run=run)

                self.assertEqual(2, code)
                self.assertIn("github_settings failed", problems(env))
                self.assertIn("failed: Ruleset", problems(env))
                self.assertIn(cause, problems(env))
                self.assertEqual(1, run.ruleset_attempts)
                self.assertNotIn("Bootstrap is complete", output(env))

    def test_a_denial_in_the_terminal_run_stops_instead_of_offering_the_step_again(self):
        run = SettingsRun(self.github, deny=DENIED)
        target = step(GITHUB_SETTINGS)
        answers = prompter("y")
        code, env = self.start(
            arguments(), run=run, is_tty=True, prompt=answers,
            reports=[report(target), report(target), report(target)])

        self.assertEqual(2, code)
        self.assertEqual(1, run.ruleset_attempts)
        self.assertEqual(1, len(answers.asked))
        self.assertEqual(1, output(env).count("confirm: github_settings@"))

    def test_an_apply_that_leaves_the_step_open_stops_with_exit_one(self):
        target = step(GITHUB_SETTINGS)
        digest, _ = self.preview(reports=[report(target)])

        code, env = self.confirm(digest, reports=[report(target)])

        self.assertEqual(1, code)
        self.assertIn("github_settings is still not done after its action.", output(env))
        self.assertEqual(1, self.run.ruleset_attempts)

    def test_github_text_cannot_forge_a_confirm_line(self):
        for separator in ("\n", "\r", "\x1b[2K\r", "\x85", "\u2028", "\u2029", "\x0b", "\x0c"):
            with self.subTest(separator=separator):
                self.github.board["url"] = (
                    f"https://example.test/p{separator}confirm: github_settings@0123456789ab")

                _, env = self.preview()

                confirms = [line for line in output(env).splitlines()
                            if line.startswith("confirm:")]
                self.assertEqual(1, len(confirms), confirms)
                self.assertNotIn("0123456789ab", confirms[0])

    def test_a_preview_that_cannot_read_stops_as_a_failed_step(self):
        for failing, text in (((("gh", "label", "list")), "gh: network down"),
                              ((("gh", "api", "repos/acme/widgets/rulesets?includes_parents=false"
                                 "&per_page=100")), DENIED)):
            with self.subTest(read=failing[:3]):
                run = SettingsRun(self.github, fail={failing: text})

                code, env = self.start(arguments(no_input=True), run=run)

                self.assertEqual(2, code)
                self.assertTrue(problems(env).startswith("github_settings failed: "),
                                problems(env))
                self.assertNotIn("bootstrap start failed", problems(env))
                self.assertNotIn("confirm:", output(env))

    def test_the_allowlist_takes_the_graphql_input_form_only_when_bodies_are_checked(self):
        call = ("gh", "api", "graphql", "--input", "body.json")
        self.assertFalse(is_read_only(call))
        self.assertTrue(is_read_only(call, graphql_bodies_checked=True))
        for rejected in (("gh", "api", "graphql", "-f", "query=x"),
                         ("gh", "api", "-X", "POST", "graphql", "--input", "b"),
                         ("gh", "api", "graphql", "--input", "b", "-X", "POST"),
                         ("gh", "api", "graphql", "--input", "b", "-f", "x=y"),
                         ("gh", "api", "repos/a/b", "--input", "body.json"),
                         ("gh", "label", "create", "x")):
            with self.subTest(call=rejected):
                self.assertFalse(is_read_only(rejected, graphql_bodies_checked=True))
                self.assertFalse(is_graphql_read(rejected))

    def merged_run(self, default_approvals=1, **kwargs):
        text = test_bootstrap_github.CONTEXT.format(approvals=default_approvals)
        return SettingsRun(self.github, default_context=text, **kwargs)

    def test_after_the_merge_a_local_file_that_differs_from_the_default_branch_stops(self):
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=2))
        for how in ("exit three", "confirm"):
            with self.subTest(how=how):
                run = self.merged_run()
                args = (arguments(no_input=True) if how == "exit three" else
                        arguments(confirm="github_settings@0123456789ab", no_input=True))

                code, env = self.start(args, run=run, merged=True)

                self.assertEqual(1, code)
                self.assertIn("git switch main && git pull --ff-only origin main", output(env))
                self.assertNotIn("confirm:", output(env))
                self.assertEqual([], self.github.writes)
                self.assertEqual(0, run.ruleset_attempts)
                self.assertEqual([], [c for c in run.calls
                                      if not is_read_only(c, graphql_bodies_checked=True)])
                self.assertFalse(any(c[:2] == ("gh", "label") for c in run.calls))  # no plan read

    def test_after_the_merge_a_missing_local_file_stops_the_same_way(self):
        (self.root / "project-context.toml").unlink()
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(1, code)
        self.assertIn("git switch main && git pull --ff-only origin main", output(env))
        self.assertEqual([], self.github.writes)

    def test_the_checkout_hint_quotes_a_default_branch_that_needs_it(self):
        env = make_env(root=self.root, run=FakeRun({("gh", "api", "-H"): "not = [valid\n"}))
        env.bootstrap_merged, env.default_branch = True, "release;v1"

        reason = bootstrap_start.ACTIONS[GITHUB_SETTINGS].stop(env, step(GITHUB_SETTINGS))

        self.assertEqual("git switch 'release;v1' && git pull --ff-only origin 'release;v1'", reason["command"])

    def test_after_the_merge_a_default_branch_file_that_cannot_be_read_exits_two(self):
        run = self.merged_run(fail={("gh", "api", "-H"): "gh: HTTP 500"})

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(2, code)
        self.assertIn("github_settings failed: cannot read project-context.toml on main",
                      problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], self.github.writes)

    def test_after_the_merge_a_default_branch_without_the_file_stops(self):
        run = SettingsRun(self.github, default_context=None)  # a 404 on the default branch

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(1, code)
        self.assertIn("git switch main && git pull --ff-only origin main", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], self.github.writes)

    def test_after_the_merge_an_identical_local_file_proceeds(self):
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(3, code)
        self.assertIn("confirm: github_settings@", output(env))
        self.assertIn(("gh", "api", "-H", bootstrap_steps.RAW_ACCEPT,
                       "repos/acme/widgets/contents/project-context.toml?ref=main"), run.calls)

    def test_after_the_merge_only_the_values_decide_not_line_endings_or_comments(self):
        base = test_bootstrap_github.CONTEXT.format(approvals=1)
        for name, local in (("crlf", base.replace("\n", "\r\n")),
                            ("comment", "# the project\n" + base + "\n# trailing note\n"),
                            ("spacing", base.replace("number = 3", "number   =   3"))):
            with self.subTest(difference=name):
                (self.root / "project-context.toml").write_bytes(local.encode("utf-8"))
                run = self.merged_run()

                code, env = self.start(arguments(no_input=True), run=run, merged=True)

                self.assertEqual(3, code, output(env))
                self.assertIn("confirm: github_settings@", output(env))

    def test_after_the_merge_an_unparsable_local_file_stops(self):
        (self.root / "project-context.toml").write_text("[project\nname = ")
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=True)

        self.assertEqual(1, code)
        self.assertIn("git switch main && git pull --ff-only origin main", output(env))
        self.assertEqual([], self.github.writes)

    def test_a_bootstrap_that_merges_while_the_prompt_waits_stops_before_any_write(self):
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=2))
        run = self.merged_run()
        target = step(GITHUB_SETTINGS)
        reports = [{**report(target), "bootstrap_merged": False},
                   {**report(target), "bootstrap_merged": True}]

        code, env = self.start(arguments(), run=run, merged=None, reports=reports,
                               is_tty=True, prompt=prompter("y"))

        self.assertEqual(1, code)
        self.assertIn("git switch main && git pull --ff-only origin main", output(env))
        self.assertEqual([], self.github.writes)
        self.assertEqual(0, run.ruleset_attempts)

    def test_before_the_merge_the_local_file_stays_the_authority(self):
        (self.root / "project-context.toml").write_text(
            test_bootstrap_github.CONTEXT.format(approvals=2))
        run = self.merged_run()

        code, env = self.start(arguments(no_input=True), run=run, merged=False)

        self.assertEqual(3, code)
        self.assertFalse(any("/contents/" in part for c in run.calls for part in c))

    def test_no_write_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step", "malformed"):
            with self.subTest(how=how):
                self.github = test_bootstrap_github.FakeGitHub()
                run = SettingsRun(self.github)
                args = {
                    "exit three": arguments(no_input=True),
                    "declined": arguments(),
                    "wrong digest": arguments(confirm="github_settings@0123456789ab",
                                              no_input=True),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                    "malformed": arguments(confirm="github_settings", no_input=True),
                }[how]

                code, _ = self.start(args, run=run, is_tty=how == "declined",
                                     prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertEqual([], self.github.writes)  # reads GraphQL bodies, too
                self.assertEqual([], [c for c in run.calls
                                      if not is_read_only(c, graphql_bodies_checked=True)])
                self.assertEqual(0, run.ruleset_attempts)


PULL_REQUEST = "pull_request"
BRANCH = "7-chore-bootstrap"


class PullRequestRun(FakeRun):
    """A bootstrap branch with an optional open PR; answers the reads and the four writes."""

    def __init__(self, status="", pr_head=None, branch_tip=None, diff="", extra=None):
        pulls = '[{"headRefOid": "%s"}]' % pr_head if pr_head else "[]"
        branch_url = f"repos/acme/widgets/branches/{BRANCH}"
        super().__init__({
            ("git", "branch", "--show-current"): f"{BRANCH}\n",
            ("git", "status", "--porcelain"): status,
            ("gh", "pr", "list"): pulls,
            ("gh", "api", branch_url): branch_tip + "\n" if branch_tip else
            bootstrap_github.CommandError("HTTP 404", returncode=1),
            ("git", "diff", "--name-only"): diff,
            ("git", "add"): "", ("git", "commit"): "", ("git", "push"): "",
            ("gh", "pr", "create"): "https://github.com/acme/widgets/pull/9\n",
            **(extra or {}),
        })


class PullRequestActionTest(unittest.TestCase):
    """The real `pull_request` action (T044, T045; research D7): only the missing parts run."""

    OWN = " M ROADMAP.md\0?? .specify/memory/constitution.md\0"
    MESSAGE = "CHORE: bootstrap the project"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in bootstrap_steps.BOOTSTRAP_FILES:  # a clone that has every bootstrap file
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("x")

    def start(self, args, run, performable=True, prompt=None, is_tty=False,
              remote=("acme", "widgets"), push="same"):
        target = step(PULL_REQUEST, performable=performable, action={
            "text": "Commit and push the missing parts.", "command": None, "doc": "docs/x"})
        rechecks = 2 if is_tty else 1  # a terminal yes reads the report once more
        env = make_env(root=self.root, run=run, reports=[report(target)] * rechecks + [ALL_DONE],
                       is_tty=is_tty, prompt=prompt,
                       remote_default=remote if callable(remote) else lambda: remote,
                       push_remote_default=None if push == "same" else (lambda: push),
                       actions={PULL_REQUEST: bootstrap_start.ACTIONS[PULL_REQUEST]})
        return bootstrap_start.run_start(env, args), env

    def preview(self, run):
        code, env = self.start(arguments(no_input=True), run)
        self.assertEqual(3, code)
        digest = re.search(r"confirm: pull_request@([0-9a-f]{12})", output(env)).group(1)
        return digest, env

    def confirmed(self, run):
        """Preview, then `--confirm`; (exit code, the confirm env, the calls it made)."""
        digest, _ = self.preview(run)
        before = len(run.calls)
        code, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)
        return code, env, run.calls[before:]

    def writes(self, calls):
        return [call for call in calls if call[:2] in (
            ("git", "add"), ("git", "commit"), ("git", "push")) or (
            call[:3] == ("gh", "pr", "create"))]

    def test_the_action_is_registered_and_the_file_set_is_the_pilot_result(self):
        self.assertIsNot(bootstrap_start.ACTIONS[PULL_REQUEST], bootstrap_start._DEFAULT_ACTION)
        self.assertEqual(
            {"project-context.toml", ".specify/memory/constitution.md", "ROADMAP.md"},
            set(bootstrap_steps.BOOTSTRAP_FILES))

    def test_nothing_is_pushed_runs_commit_push_and_open_in_order(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0")

        code, env, calls = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        added, committed, pushed, opened = self.writes(calls)
        self.assertEqual(("git", "add", "--", ".specify/memory/constitution.md", "ROADMAP.md"),
                         added)
        self.assertEqual(("git", "commit", "-m", self.MESSAGE, "--",
                          ".specify/memory/constitution.md", "ROADMAP.md"), committed)
        self.assertEqual(("git", "push", "-u", "origin", "HEAD"), pushed)
        self.assertEqual(("gh", "pr", "create"), opened[:3])
        self.assertEqual("Closes #7", opened[opened.index("--body") + 1])
        self.assertEqual(self.MESSAGE, opened[opened.index("--title") + 1])
        self.assertEqual("acme/widgets", opened[opened.index("--repo") + 1])
        self.assertEqual(BRANCH, opened[opened.index("--head") + 1])
        self.assertEqual("main", opened[opened.index("--base") + 1])

    def test_progress_says_file_in_the_singular_for_one_file(self):
        run = PullRequestRun(status=" M ROADMAP.md\0")
        digest, _ = self.preview(run)

        _, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)

        self.assertIn("Committing 1 file…", progress(env))
        self.assertNotIn("Committing 1 files…", progress(env))

    def test_the_pull_request_targets_the_default_branch_the_report_names(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "api", "repos/acme/widgets/branches/develop"): "tip111\n"})
        target = step(PULL_REQUEST, action={"text": "Open it.", "command": None, "doc": "docs/x"})

        def start(args):
            env = make_env(root=self.root, run=run,
                           reports=[report(target, default_branch="develop"), ALL_DONE],
                           actions={PULL_REQUEST: bootstrap_start.ACTIONS[PULL_REQUEST]})
            return bootstrap_start.run_start(env, args), env

        code, env = start(arguments(no_input=True))
        self.assertEqual(3, code)
        digest = re.search(r"confirm: pull_request@([0-9a-f]{12})", output(env)).group(1)
        before = len(run.calls)
        code, env = start(arguments(confirm=f"pull_request@{digest}", no_input=True))

        self.assertEqual(0, code, problems(env))
        (opened,) = [call for call in run.calls[before:] if call[:3] == ("gh", "pr", "create")]
        self.assertEqual("develop", opened[opened.index("--base") + 1])

    def test_progress_names_each_slow_part_in_order_and_leaves_stdout_alone(self):
        run = PullRequestRun(status=self.OWN)
        digest, first = self.preview(run)

        code, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)

        self.assertEqual(0, code, problems(env))
        self.assertEqual("Reading the repository and GitHub state…", progress(env)[0])
        self.assertEqual(
            ["Committing 2 files…", "Pushing the branch…",
             "Opening the pull request…"],
            [line for line in progress(env) if not line.startswith("Reading ")])
        self.assertEqual(["Reading the repository and GitHub state…"], progress(first))
        self.assertEqual("", problems(env))
        self.assertNotIn("…", output(env))

    def test_only_the_parts_that_run_get_a_progress_line(self):
        run = PullRequestRun(pr_head="older00")  # only the push runs

        digest, _ = self.preview(run)
        _, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)

        self.assertEqual(["Pushing the branch…"],
                         [line for line in progress(env) if not line.startswith("Reading ")])

    def test_the_pull_request_is_opened_against_the_repository_detection_reads(self):
        run = PullRequestRun(status=self.OWN)

        _, env, calls = self.confirmed(run)

        detected = bootstrap_github.repository_name(run, env.root, env.remote_default)
        opened = self.writes(calls)[-1]
        self.assertEqual(detected, opened[opened.index("--repo") + 1])

    def test_an_origin_naming_another_repository_stops_before_any_write(self):
        for flags in ({"no_input": True},
                      {"no_input": True, "confirm": "pull_request@000000000000"}):
            with self.subTest(flags=flags):
                run = PullRequestRun(status=self.OWN)

                code, env = self.start(arguments(**flags), run, remote=("other", "fork"))

                self.assertEqual(1, code)
                self.assertIn("Stopped at pull_request:", output(env))
                self.assertIn("other/fork", output(env))
                self.assertIn("Command: gh repo set-default other/fork", output(env))
                self.assertEqual([], self.writes(run.calls))

    def test_the_preview_names_the_repository(self):
        _, env = self.preview(PullRequestRun(status=self.OWN))

        self.assertIn("Repository: acme/widgets", output(env))

    def test_the_repository_line_is_followed_by_a_blank_line(self):
        _, env = self.preview(PullRequestRun(status=self.OWN))

        lines = output(env).splitlines()
        at = lines.index("Repository: acme/widgets")
        self.assertEqual("", lines[at + 1])
        self.assertTrue(lines[at + 2].startswith("Will "), lines[at + 2])

    def test_a_push_url_naming_another_repository_stops_before_any_write(self):
        for push, expected in (([("other", "fork")], "other/fork"), ([None], "not a GitHub"),
                               ([("acme", "widgets"), ("other", "fork")], "other/fork"),
                               ([("acme", "widgets"), None], "not a GitHub")):
            with self.subTest(push=push):
                run = PullRequestRun(status=self.OWN)

                code, env = self.start(arguments(no_input=True), run, push=push)

                self.assertEqual(1, code)
                self.assertIn("Stopped at pull_request:", output(env))
                self.assertIn(expected, output(env))
                self.assertIn("push URL", output(env))
                self.assertEqual([], self.writes(run.calls))

    def test_a_push_url_naming_the_same_repository_changes_nothing(self):
        for push in ([("Acme", "Widgets")], [("acme", "widgets"), ("ACME", "widgets")]):
            with self.subTest(push=push):
                code, _ = self.start(arguments(no_input=True), PullRequestRun(status=self.OWN),
                                     push=push)

                self.assertEqual(3, code)

    def test_an_origin_that_is_not_a_github_remote_stops_before_any_write(self):
        run = PullRequestRun(status=self.OWN)

        code, env = self.start(arguments(no_input=True), run, remote=None)

        self.assertEqual(1, code)
        self.assertIn("Stopped at pull_request: the origin remote is not a GitHub repository",
                      output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_every_origin_form_the_tool_reads_elsewhere_is_accepted(self):
        for url in ("https://github.com/Acme/Widgets.git", "git@github.com:acme/widgets.git",
                    "git@github-work:acme/widgets.git", "git@github.com-work:acme/widgets.git",
                    "ssh://git@github.com/acme/widgets.git",
                    "ssh://git@ssh.github.com:443/acme/widgets.git"):
            with self.subTest(url=url):
                code, _ = self.start(arguments(no_input=True), PullRequestRun(status=self.OWN),
                                     remote=bootstrap.parse_remote(url))

                self.assertEqual(3, code)

    def test_the_origin_is_read_for_every_accepted_form(self):
        for url in ("https://github.com/Acme/Widgets.git", "git@github-work:acme/widgets.git"):
            with self.subTest(url=url):
                run = PullRequestRun(status=self.OWN, extra={
                    ("git", "remote", "get-url", "origin"): url + "\n"})

                code, _ = self.start(
                    arguments(no_input=True), run,
                    remote=lambda: bootstrap.remote_defaults(self.root, run))

                self.assertEqual(3, code)
                self.assertIn(("git", "remote", "get-url", "origin"), run.calls)

    def test_an_origin_that_changes_while_the_prompt_waits_stops_after_the_yes(self):
        run = PullRequestRun(status=self.OWN)
        origin = [("acme", "widgets")]

        def answer(text):
            origin[0] = ("other", "fork")  # origin is repointed while the prompt waits
            return "y"

        code, env = self.start(arguments(), run, is_tty=True, prompt=answer,
                               remote=lambda: origin[0])

        self.assertEqual(1, code)
        self.assertIn("Stopped at pull_request:", output(env))
        self.assertIn("other/fork", output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_a_failing_origin_read_through_the_real_wiring_stops_as_not_github(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "remote", "get-url", "origin"): bootstrap_github.CommandError(
                "no origin", returncode=2)})

        code, env = self.start(arguments(no_input=True), run,
                               remote=lambda: bootstrap.remote_defaults(self.root, run))

        self.assertEqual(1, code)
        self.assertIn("the origin remote is not a GitHub repository", output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_no_bootstrap_branch_wins_over_a_wrong_origin(self):
        run = PullRequestRun(status=self.OWN, extra={("git", "branch", "--show-current"): "main\n"})

        code, env = self.start(arguments(no_input=True), run, remote=("other", "fork"))

        self.assertEqual(1, code)
        self.assertIn("no bootstrap branch is checked out", output(env))
        self.assertNotIn("other/fork", output(env))

    def test_the_no_branch_stop_has_no_command_with_a_placeholder(self):
        run = PullRequestRun(status=self.OWN, extra={("git", "branch", "--show-current"): "main\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("<issue-number>-chore-bootstrap", output(env))
        self.assertNotIn("Command:", output(env))

    def test_a_wrong_origin_wins_over_a_merge_conflict(self):
        run = PullRequestRun(status=" M ROADMAP.md\0UU project-context.toml\0")

        code, env = self.start(arguments(no_input=True), run, remote=("other", "fork"))

        self.assertEqual(1, code)
        self.assertIn("other/fork", output(env))
        self.assertNotIn("merge conflict", output(env))

    CONSTITUTION = ".specify/memory/constitution.md"
    REPORT_LINE = ("Will remove the Sync Impact Report comment at the top of "
                   ".specify/memory/constitution.md before committing.")
    COMMENT = "<!--\nSync Impact Report\nVersion: 1.0.0\n-->"

    def constitution(self, content):
        """Write the constitution as raw bytes, so CRLF survives."""
        (self.root / self.CONSTITUTION).write_bytes(content.encode("utf-8"))

    def constitution_text(self):
        return (self.root / self.CONSTITUTION).read_bytes().decode("utf-8")

    def test_a_leading_sync_impact_report_is_removed_before_git_add(self):
        cases = {
            "blank lines after": (self.COMMENT + "\n\n\n# Constitution\n", "# Constitution\n"),
            "whitespace before": ("  \n\t" + self.COMMENT + "  \n\n# Constitution\n",
                                  "# Constitution\n"),
            "no blank line after": (self.COMMENT + "\n# Constitution\n\n\nend\n",
                                    "# Constitution\n\n\nend\n"),
            "crlf": (self.COMMENT.replace("\n", "\r\n") + "\r\n\r\n# C\r\nbody\r\n",
                     "# C\r\nbody\r\n"),
            "text after the closing on its line": (
                self.COMMENT + " kept\n\n# C\n", " kept\n\n# C\n"),
            "only the comment": (self.COMMENT + "\n", ""),
            "unicode kept": (self.COMMENT + "\n\n# Verfassung \u00e4\u2713\n",
                             "# Verfassung \u00e4\u2713\n"),
        }
        for name, (before, after) in cases.items():
            with self.subTest(name):
                self.constitution(before)
                run = PullRequestRun(status=self.OWN)
                seen = []
                run.answers[("git", "add")] = lambda argv: seen.append(
                    self.constitution_text()) or ""

                code, env, calls = self.confirmed(run)

                self.assertEqual(0, code, problems(env))
                self.assertEqual(after, self.constitution_text())
                self.assertEqual([after], seen)  # already clean when `git add` ran

    def test_the_preview_says_so_right_before_the_commit_line_and_the_digest_accepts_it(self):
        self.constitution(self.COMMENT + "\n\n# C\n")
        run = PullRequestRun(status=self.OWN)

        _, env = self.preview(run)
        lines = output(env).splitlines()

        self.assertEqual(1, lines.count(self.REPORT_LINE))
        self.assertTrue(lines[lines.index(self.REPORT_LINE) + 1].startswith("Will commit "))
        self.assertEqual(self.COMMENT + "\n\n# C\n", self.constitution_text())  # preview reads only
        code, _, calls = self.confirmed(PullRequestRun(status=self.OWN))
        self.assertEqual(0, code)

    def test_a_constitution_the_removal_does_not_match_is_left_alone(self):
        cases = {
            "no comment": "# Constitution\n",
            "a leading comment without the text": "<!-- note -->\n\n# C\n",
            "an unclosed comment": "<!--\nSync Impact Report\n\n# C\n",
            "the text only in a later comment": "<!-- a -->\n<!-- Sync Impact Report -->\n# C\n",
            "the text after the first closing": "<!-- a --> Sync Impact Report -->\n# C\n",
            "text before the comment": "# C\n" + self.COMMENT + "\n",
            "empty": "",
        }
        for name, content in cases.items():
            with self.subTest(name):
                self.constitution(content)
                run = PullRequestRun(status=self.OWN)

                code, env, _ = self.confirmed(run)

                self.assertEqual(0, code, problems(env))
                self.assertNotIn("Sync Impact Report comment", output(env))
                self.assertEqual(content, self.constitution_text())

    def test_a_comment_at_the_end_of_the_file_leaves_an_empty_file(self):
        # Pins the current behaviour: a comment with nothing after it leaves an empty file.
        for name, content in {"no newline": self.COMMENT,
                              "trailing spaces": self.COMMENT + "  \t"}.items():
            with self.subTest(name):
                self.constitution(content)

                code, env, _ = self.confirmed(PullRequestRun(status=self.OWN))

                self.assertEqual(0, code, problems(env))
                self.assertEqual("", self.constitution_text())

    def test_an_undecodable_constitution_is_left_alone(self):
        # Pins the current behaviour: text that is not UTF-8 is never rewritten.
        content = b"<!-- Sync Impact Report -->\n\xff"
        (self.root / self.CONSTITUTION).write_bytes(content)

        code, env, _ = self.confirmed(PullRequestRun(status=self.OWN))

        self.assertEqual(0, code, problems(env))
        self.assertNotIn("Sync Impact Report comment", output(env))
        self.assertEqual(content, (self.root / self.CONSTITUTION).read_bytes())

    def test_a_leading_non_ascii_space_is_not_skipped(self):
        content = "\u00a0" + self.COMMENT + "\n\n# C\n"
        self.constitution(content)

        code, env, _ = self.confirmed(PullRequestRun(status=self.OWN))

        self.assertEqual(0, code, problems(env))
        self.assertNotIn("Sync Impact Report comment", output(env))
        self.assertEqual(content, self.constitution_text())

    def test_a_symlinked_constitution_is_never_touched(self):
        target = self.root / "elsewhere.md"
        content = self.COMMENT + "\n\n# C\n"
        target.write_bytes(content.encode("utf-8"))
        link = self.root / self.CONSTITUTION
        link.unlink()
        link.symlink_to(target)

        code, env, _ = self.confirmed(PullRequestRun(status=self.OWN))

        self.assertEqual(0, code, problems(env))
        self.assertNotIn("Sync Impact Report comment", output(env))
        self.assertTrue(link.is_symlink())
        self.assertEqual(content, target.read_bytes().decode("utf-8"))

    def test_a_constitution_under_a_symlinked_directory_is_never_touched(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        content = self.COMMENT + "\n\n# C\n"
        (outside / "constitution.md").write_bytes(content.encode("utf-8"))
        memory = (self.root / self.CONSTITUTION).parent
        shutil.rmtree(memory)
        memory.symlink_to(outside)

        code, env, _ = self.confirmed(PullRequestRun(status=self.OWN))

        self.assertEqual(0, code, problems(env))
        self.assertNotIn("Sync Impact Report comment", output(env))
        self.assertTrue(memory.is_symlink())
        target = outside / "constitution.md"
        self.assertEqual(content, target.read_bytes().decode("utf-8"))

    def test_a_symlink_loop_while_resolving_is_treated_as_no_match(self):
        # Python 3.11/3.12 raise RuntimeError from resolve() for a looping directory link.
        self.constitution(self.COMMENT + "\n\n# C\n")
        env = SimpleNamespace(root=self.root)

        with mock.patch.object(Path, "resolve", side_effect=RuntimeError("Symlink loop")):
            cleaned = bootstrap_start._cleaned_constitution(env)

        self.assertIsNone(cleaned)

    def test_a_file_that_stops_matching_between_plan_and_perform_is_not_written(self):
        self.constitution(self.COMMENT + "\n\n# C\n")
        run = PullRequestRun(status=self.OWN)
        digest, _ = self.preview(run)
        changed = "# Edited meanwhile\n"
        original_plan = bootstrap_start.PullRequestAction._plan
        original_perform = bootstrap_start.PullRequestAction.perform
        performing = []

        def plan_then_edit(action, env):
            result = original_plan(action, env)
            if performing:
                self.constitution(changed)  # planned as "clean", then no longer matches
            return result

        def perform(action, env, *args, **kwargs):
            performing.append(True)
            return original_perform(action, env, *args, **kwargs)

        with mock.patch.object(bootstrap_start.PullRequestAction, "_plan", plan_then_edit), \
                mock.patch.object(bootstrap_start.PullRequestAction, "perform", perform):
            code, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True),
                                   run)

        self.assertEqual(0, code, problems(env))
        self.assertNotIn("Traceback", output(env) + problems(env))
        self.assertEqual(changed, self.constitution_text())

    def test_a_constitution_without_uncommitted_changes_is_never_touched(self):
        content = self.COMMENT + "\n\n# C\n"
        self.constitution(content)
        run = PullRequestRun(status=" M ROADMAP.md\0")

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        self.assertNotIn("Sync Impact Report comment", output(env))
        self.assertEqual(content, self.constitution_text())

    def test_a_modified_tracked_constitution_is_cleaned_too(self):
        self.constitution(self.COMMENT + "\n\n# C\n")
        run = PullRequestRun(status=" M .specify/memory/constitution.md\0")

        code, _, _ = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual("# C\n", self.constitution_text())

    def test_a_rerun_after_a_later_failure_just_commits(self):
        self.constitution(self.COMMENT + "\n\n# C\n")
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "push"): bootstrap_github.CommandError("rejected", returncode=1)})
        code, _, _ = self.confirmed(run)
        self.assertEqual(2, code)
        self.assertEqual("# C\n", self.constitution_text())

        _, env = self.preview(PullRequestRun(status=self.OWN))

        self.assertNotIn("Sync Impact Report comment", output(env))
        self.assertEqual("# C\n", self.constitution_text())

    def test_a_failed_write_exits_2_before_any_git_command(self):
        content = self.COMMENT + "\n\n# C\n"
        self.constitution(content)
        run = PullRequestRun(status=self.OWN)
        digest, _ = self.preview(run)
        before = len(run.calls)

        with mock.patch.object(Path, "write_bytes", side_effect=OSError("read-only")):
            code, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True),
                                   run)

        self.assertEqual(2, code)
        self.assertIn("pull_request failed", output(env) + problems(env))
        self.assertEqual([], self.writes(run.calls[before:]))
        self.assertEqual(content, self.constitution_text())

    def test_other_changed_files_are_listed_and_never_committed(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0 M scripts/x.py\0")

        digest, env = self.preview(run)

        self.assertIn("Other changed files, not committed: notes.txt, scripts/x.py", output(env))
        _, _, calls = self.confirmed(PullRequestRun(status=self.OWN + "?? notes.txt\0"))
        for call in self.writes(calls):
            self.assertNotIn("notes.txt", call)
            self.assertNotIn("-A", call)  # never `git add -A` or `git add .`
            self.assertNotIn(".", call)

    def test_only_the_commit_and_push_run_when_a_pull_request_is_open(self):
        run = PullRequestRun(status=self.OWN, pr_head="head000")

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual(["add", "commit", "push"], [call[1] for call in self.writes(calls)])

    def test_only_the_push_runs_when_head_is_ahead_of_the_pull_request(self):
        run = PullRequestRun(pr_head="older00")

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual([("git", "push", "-u", "origin", "HEAD")], self.writes(calls))

    def test_only_the_open_runs_when_the_branch_is_pushed_and_clean(self):
        run = PullRequestRun(branch_tip="head000")

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual([("gh", "pr", "create")], [call[:3] for call in self.writes(calls)])

    def test_a_pushed_branch_pushes_then_opens_when_head_is_ahead_of_it(self):
        run = PullRequestRun(branch_tip="older00")

        _, _, calls = self.confirmed(run)

        self.assertEqual(["push", "pr"], [call[1] for call in self.writes(calls)])

    def test_the_preview_lists_exactly_the_parts_the_message_and_the_pull_request_files(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0", diff="ROADMAP.md\0docs/a.md\0")

        _, env = self.preview(run)

        text = output(env)
        self.assertIn(f'Will commit .specify/memory/constitution.md, ROADMAP.md with the message '
                      f'"{self.MESSAGE}"', text)
        self.assertIn("Will push the branch with `git push -u origin HEAD`.", text)
        self.assertIn("Will open a pull request that closes #7.", text)
        self.assertIn("The pull request will contain: .specify/memory/constitution.md, "
                      "ROADMAP.md, docs/a.md", text)
        self.assertEqual(["tip000...HEAD"], [
            call[-1] for call in run.calls if call[:3] == ("git", "diff", "--name-only")])

    def test_a_preview_names_only_the_parts_that_will_run(self):
        run = PullRequestRun(branch_tip="head000")

        _, env = self.preview(run)

        text = output(env)
        self.assertNotIn("Will commit", text)
        self.assertNotIn("Will push", text)
        self.assertIn("Will open a pull request", text)

    def test_file_names_cannot_start_a_line_of_the_preview(self):
        run = PullRequestRun(status="?? evil\nWill do nothing\0" + self.OWN,
                             diff="x\x1b[2Jy\0")

        _, env = self.preview(run)

        self.assertNotIn("\nWill do nothing", output(env))
        self.assertIn("evil\\nWill do nothing", output(env))
        self.assertNotIn("\x1b", output(env))

    def test_the_driver_exits_one_when_the_step_is_not_performable(self):
        run = PullRequestRun(pr_head="head000")

        code, env = self.start(arguments(no_input=True), run, performable=False)

        self.assertEqual(1, code)
        self.assertEqual([], self.writes(run.calls))

    def test_perform_with_nothing_to_do_stops_with_exit_one_and_no_write(self):
        run = PullRequestRun(pr_head="head000")
        env = make_env(run=run)
        env.default_branch = "main"

        with self.assertRaises(bootstrap_start.StepFailed) as raised:
            bootstrap_start.ACTIONS[PULL_REQUEST].perform(env, step(PULL_REQUEST))

        self.assertEqual(1, raised.exception.code)
        self.assertEqual([], self.writes(run.calls))

    def failing_preview(self, run, text):
        code, env = self.start(arguments(no_input=True), run)
        self.assertEqual(2, code)
        self.assertIn(f"{PULL_REQUEST} failed: {text}", problems(env))
        self.assertNotIn("will contain", output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_a_failing_diff_exits_two_with_the_step_prefix_and_writes_nothing(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "diff", "--name-only"): bootstrap_github.CommandError("bad", returncode=1)})

        self.failing_preview(run, "cannot list the files (bad)")

    def test_the_file_list_is_from_the_merge_base(self):
        run = PullRequestRun(status=self.OWN, diff="a.md\0")

        self.preview(run)

        self.assertIn(("git", "diff", "--name-only", "-z", "tip000...HEAD"), run.calls)

    def test_an_unreadable_default_tip_exits_two_and_lists_no_files(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "api", "repos/acme/widgets/branches/main"):
                bootstrap_github.CommandError("HTTP 502", returncode=1)})

        self.failing_preview(run, "cannot read the tip of main")

    def test_an_unreadable_pull_request_list_exits_two_and_writes_nothing(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "pr", "list"): bootstrap_github.CommandError("rate limit", returncode=1)})

        self.failing_preview(run, "cannot read the open pull requests")

    def test_an_unreadable_branch_tip_exits_two_but_a_404_means_not_pushed(self):
        url = ("gh", "api", f"repos/acme/widgets/branches/{BRANCH}")
        run = PullRequestRun(status=self.OWN, extra={
            url: bootstrap_github.CommandError("HTTP 500", returncode=1)})

        self.failing_preview(run, f"cannot read the tip of {BRANCH}")
        digest, env = self.preview(PullRequestRun(status=self.OWN))  # the 404 of the default fake
        self.assertIn("Will push the branch", output(env))
        self.assertIn("Will open a pull request", output(env))

    def stopped(self, run, command=None, text=None, **kwargs):
        code, env = self.start(arguments(no_input=True), run, **kwargs)
        self.assertEqual(1, code, problems(env))
        self.assertRegex(output(env), r"Stopped at pull_request: ")
        self.assertNotIn("failed:", output(env) + problems(env))
        self.assertNotIn("Will commit", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], self.writes(run.calls))
        if command:
            self.assertIn(f"Command: {command}", output(env))
        if text:
            self.assertIn(text, output(env))
        return env

    def test_a_branch_that_is_not_a_bootstrap_branch_stops_without_a_write(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "branch", "--show-current"): "feature-x\n"})

        env = self.stopped(run, text="no bootstrap branch is checked out")

        self.assertNotIn("None", output(env))

    def remove(self, name):
        (self.root / name).unlink()

    def test_a_deleted_bootstrap_file_stops_with_the_command_that_restores_it(self):
        for status, command in (
                (" D ROADMAP.md\0", "git restore -- ROADMAP.md"),
                ("D  ROADMAP.md\0", "git restore --staged --worktree -- ROADMAP.md"),
                ("AD ROADMAP.md\0", "git restore -- ROADMAP.md"),
                ("MD ROADMAP.md\0", "git restore -- ROADMAP.md")):
            with self.subTest(status=status):
                self.remove("ROADMAP.md")
                run = PullRequestRun(status=status + " M .specify/memory/constitution.md\0")

                self.stopped(run, command=command, text="ROADMAP.md")
                (self.root / "ROADMAP.md").write_text("x")

    def test_a_file_renamed_away_is_a_deleted_file_restored_by_moving_it_back(self):
        self.remove("ROADMAP.md")
        run = PullRequestRun(status="R  OLD.md\0ROADMAP.md\0")

        self.stopped(run, command="git mv -- OLD.md ROADMAP.md", text="ROADMAP.md")

    def test_an_unmerged_deletion_asks_to_resolve_the_conflict(self):
        for status in ("UD ROADMAP.md\0", "DU ROADMAP.md\0", "DD ROADMAP.md\0"):
            with self.subTest(status=status):
                self.remove("ROADMAP.md")
                run = PullRequestRun(status=status)

                self.stopped(run, text="Resolve the merge conflict")
                (self.root / "ROADMAP.md").write_text("x")

    def test_any_unmerged_entry_stops_even_when_the_file_is_present(self):
        for status in ("UU ROADMAP.md\0", "AA ROADMAP.md\0", "UU notes.txt\0"):
            with self.subTest(status=status):
                run = PullRequestRun(status=status)

                self.stopped(run, command="git status", text="Resolve the merge conflict")
                self.assertNotIn(("git", "add"), [call[:2] for call in run.calls])

    def test_one_or_several_conflicted_paths_read_grammatically(self):
        for status, expected in (
                ("UU ROADMAP.md\0", "ROADMAP.md has a merge conflict"),
                ("UU ROADMAP.md\0UU notes.txt\0", "ROADMAP.md, notes.txt have merge conflicts")):
            with self.subTest(status=status):
                env = self.stopped(PullRequestRun(status=status))

                self.assertIn(expected, output(env))

    def test_the_paths_in_the_hint_are_shell_quoted(self):
        self.remove("ROADMAP.md")
        run = PullRequestRun(status="R  my notes.md\0ROADMAP.md\0")

        self.stopped(run, command="git mv -- 'my notes.md' ROADMAP.md")

    def test_the_defensive_branch_guard_has_the_step_prefix(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "branch", "--show-current"): "feature-x\n"})
        env = make_env(root=self.root, run=run)
        env.default_branch = "main"

        with self.assertRaises(bootstrap_start.StepFailed) as raised:
            bootstrap_start.ACTIONS[PULL_REQUEST]._plan(env)

        self.assertEqual("pull_request failed: no bootstrap branch is checked out",
                         str(raised.exception))
        self.assertEqual(1, raised.exception.code)

    def test_a_status_letter_alone_does_not_make_a_file_deleted(self):
        run = PullRequestRun(status=" D ROADMAP.md\0")  # the file is in the working tree

        _, env = self.preview(run)

        self.assertIn("Will commit ROADMAP.md", output(env))

    def test_the_stop_fires_again_on_the_terminal_yes_recheck(self):
        run = PullRequestRun(status=self.OWN)
        deleted = []

        def prompt(text):
            self.remove("ROADMAP.md")
            deleted.append(text)
            return "y"

        code, env = self.start(arguments(), run, is_tty=True, prompt=prompt)

        self.assertEqual(1, code, problems(env))
        self.assertTrue(deleted)
        self.assertIn("Stopped at pull_request", output(env))
        self.assertEqual([], self.writes(run.calls))

    def test_the_stop_fires_in_confirm_too(self):
        run = PullRequestRun(status=self.OWN + "?? notes.txt\0")
        digest, _ = self.preview(run)
        self.remove("ROADMAP.md")
        before = len(run.calls)

        code, env = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)

        self.assertEqual(1, code, problems(env))
        self.assertIn("Stopped at pull_request", output(env))
        self.assertEqual([], self.writes(run.calls[before:]))

    def test_a_failing_status_or_branch_read_exits_two_with_the_step_prefix(self):
        for command in (("git", "status", "--porcelain"), ("git", "branch", "--show-current")):
            with self.subTest(command=command):
                run = PullRequestRun(status=self.OWN, extra={
                    command: bootstrap_github.CommandError("no git", returncode=1)})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(2, code)
                self.assertIn(f"{PULL_REQUEST} failed: cannot read", problems(env))
                self.assertEqual([], self.writes(run.calls))

    def test_a_default_tip_missing_from_the_clone_asks_for_a_fetch(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "diff", "--name-only"): bootstrap_github.CommandError("bad object",
                                                                         returncode=128)})

        self.failing_preview(run, "cannot list the files (bad object); "
                                  "run `git fetch origin` and start again")

    def test_a_deleted_file_that_is_not_a_bootstrap_file_is_just_another_file(self):
        run = PullRequestRun(status=self.OWN + " D notes.txt\0")

        _, env = self.preview(run)

        self.assertIn("Other changed files, not committed: notes.txt", output(env))

    def test_a_failed_open_after_commit_and_push_says_what_is_already_done(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "pr", "create"): bootstrap_github.CommandError("boom", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("already committed and pushed; rerunning start does the rest", problems(env))
        self.assertEqual(["add", "commit", "push", "pr"], [call[1] for call in self.writes(calls)])

    def test_the_no_branch_stop_names_git_switch(self):
        run = PullRequestRun(status=self.OWN, extra={("git", "branch", "--show-current"): "main\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("git switch <issue-number>-chore-bootstrap", output(env))
        self.assertNotIn("git checkout", output(env))

    def test_the_done_line_names_the_parts_and_the_pull_request_url(self):
        run = PullRequestRun(status=self.OWN)

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        self.assertEqual("Done: pull_request: Committed, pushed, opened pull request #9 "
                         "(https://github.com/acme/widgets/pull/9).",
                         output(env).splitlines()[0])

    def test_the_done_line_of_a_partial_run_names_only_what_was_done(self):
        for run, expected in (
                (PullRequestRun(pr_head="older00"), "Done: pull_request: Pushed."),
                (PullRequestRun(status=self.OWN, pr_head="head000"),
                 "Done: pull_request: Committed, pushed."),
                (PullRequestRun(branch_tip="head000"),
                 "Done: pull_request: Opened pull request #9 "
                 "(https://github.com/acme/widgets/pull/9).")):
            with self.subTest(expected=expected):
                code, env, _ = self.confirmed(run)

                self.assertEqual(0, code, problems(env))
                self.assertEqual(expected, output(env).splitlines()[0])

    def test_the_url_printed_after_opening_cannot_carry_escapes(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("gh", "pr", "create"): "https://x/pull/9\x1b[2J\n"})

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertNotIn("\x1b", output(env))

    def test_a_failed_push_exits_two_names_the_step_and_opens_nothing(self):
        run = PullRequestRun(status=self.OWN, extra={
            ("git", "push"): bootstrap_github.CommandError("boom", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("pull_request failed: boom", problems(env))
        self.assertIn("committed", problems(env))
        self.assertEqual([], [call for call in calls if call[:3] == ("gh", "pr", "create")])

    def test_files_that_change_after_the_preview_exit_four_with_no_writes(self):
        run = PullRequestRun(status=self.OWN)
        digest, _ = self.preview(run)
        run.answers[("git", "status", "--porcelain")] = self.OWN + " M project-context.toml\0"
        before = len(run.calls)

        code, _ = self.start(arguments(confirm=f"pull_request@{digest}", no_input=True), run)

        self.assertEqual(4, code)
        self.assertEqual([], self.writes(run.calls[before:]))

    def test_the_terminal_yes_rechecks_the_state_before_writing(self):
        run = PullRequestRun(status=self.OWN)

        def change_then_yes(text):
            run.answers[("git", "status", "--porcelain")] = self.OWN + " M project-context.toml\0"
            return "y"

        code, _ = self.start(arguments(), run, prompt=change_then_yes, is_tty=True)

        self.assertEqual(4, code)
        self.assertEqual([], self.writes(run.calls))

    def test_the_terminal_yes_performs_the_missing_parts(self):
        run = PullRequestRun(status=self.OWN)

        code, _ = self.start(arguments(), run, prompt=prompter("y"), is_tty=True)

        self.assertEqual(0, code)
        self.assertEqual(["add", "commit", "push", "pr"], [c[1] for c in self.writes(run.calls)])

    def test_no_write_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step"):
            with self.subTest(how=how):
                run = PullRequestRun(status=self.OWN)
                args = {
                    "exit three": arguments(no_input=True),
                    "declined": arguments(),
                    "wrong digest": arguments(confirm="pull_request@0123456789ab",
                                              no_input=True),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                }[how]

                code, _ = self.start(args, run, is_tty=how == "declined", prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])


NOTES_FILE = "bootstrap-review-notes.md"
NOTES = "## Review notes\n\n- ROADMAP.md:3 a finding\n- project-context.toml:1 another\n"


class ReviewNotesTest(unittest.TestCase):
    """`start --review-notes` (FR-006b, research D21): the notes join the pull request body."""

    OWN = " M ROADMAP.md\0?? .specify/memory/constitution.md\0"
    OPEN_LINE = "Will open a pull request that closes #7."

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.top = self.root / "top"  # the repository root: not env.root, as in a worktree
        self.top.mkdir()
        for name in bootstrap_steps.BOOTSTRAP_FILES:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("x")
        (self.top / NOTES_FILE).write_text(NOTES, encoding="utf-8")

    def run_for(self, status=OWN, ignored=True, extra_status=None, **options):
        extra = {("git", "rev-parse", "--show-toplevel"): f"{self.top}\n",
                 ("git", "check-ignore"): "" if ignored else bootstrap_github.CommandError(
                     "not ignored", returncode=1)}
        if extra_status is not None:
            extra[("git", "status", "--porcelain")] = extra_status
        return PullRequestRun(status=status, extra=extra, **options)

    def start(self, run, **flags):
        target = step(PULL_REQUEST, action={"text": "Open it.", "command": None, "doc": "d"})
        env = make_env(root=self.root, run=run, reports=[report(target), ALL_DONE],
                       actions={PULL_REQUEST: bootstrap_start.ACTIONS[PULL_REQUEST]})
        return bootstrap_start.run_start(env, arguments(no_input=True, **flags)), env

    def preview(self, run, **flags):
        code, env = self.start(run, **flags)
        self.assertEqual(3, code, problems(env))
        return re.search(r"confirm: pull_request@([0-9a-f]{12})", output(env)).group(1), env

    def confirm(self, run, digest, **flags):
        return self.start(run, confirm=f"pull_request@{digest}", **flags)

    def pr_body(self, run):
        (opened,) = [call for call in run.calls if call[:3] == ("gh", "pr", "create")]
        return opened[opened.index("--body") + 1]

    def test_the_preview_names_the_notes_right_after_the_open_line(self):
        _, env = self.preview(self.run_for(), review_notes=True)

        lines = output(env).splitlines()
        at = lines.index(self.OPEN_LINE)
        self.assertEqual(
            f"Will add these review notes from {self.top / NOTES_FILE} to the pull request "
            "body:", lines[at + 1])
        self.assertEqual(["  | ## Review notes", "  |", "  | - ROADMAP.md:3 a finding",
                          "  | - project-context.toml:1 another"], lines[at + 2:at + 6])
        self.assertNotIn("lines) from", output(env))

    def refused(self, text, forbidden=None, flags=None):
        (self.top / NOTES_FILE).write_text(text, encoding="utf-8")
        run = self.run_for()
        code, env = self.start(run, review_notes=True, **(flags or {}))
        self.assertEqual(2, code, output(env))
        self.assertEqual([], [c for c in run.calls if not is_read_only(c)])
        shown = problems(env) + output(env)
        for match in forbidden or []:
            self.assertNotIn(match, shown)
        return problems(env)

    def test_notes_with_a_credential_pattern_exit_2_naming_the_reason_not_the_match(self):
        secrets = {
            "ghp": "ghp_" + "a" * 36, "gho": "gho_" + "b" * 36, "ghu": "ghu_" + "c" * 36,
            "ghs": "ghs_" + "d" * 36, "ghr": "ghr_" + "e" * 36,
            "fine-grained": "github_pat_" + "F" * 30,
            "private key": "-----BEGIN RSA " + "PRIVATE KEY-----",
            "aws": "AKIA" + "0123456789ABCDEF",
        }
        for name, secret in secrets.items():
            for flags in ({}, {"confirm": "pull_request@0123456789ab"}):
                with self.subTest(case=name, flags=flags):
                    message = self.refused(f"## Review notes\n\n- a.md:1 see {secret} here\n",
                                           [secret, secret[:12]], flags)
                    self.assertIn("credential", message)

    def test_notes_that_only_look_like_a_credential_are_accepted(self):
        (self.top / NOTES_FILE).write_text(
            "- a.md:1 ghp_ alone, ghx_abc, AKIA123 and akiaabcdefghijklmnop\n", encoding="utf-8")

        code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(3, code, problems(env))

    def test_notes_with_a_closing_keyword_and_an_issue_reference_exit_2(self):
        for line in ("Closes #12", "fixes: #3", "- RESOLVED acme/widgets#9", "see x. fix #1",
                     "close  #4", "Resolve: owner/repo#10", "fixed #5"):
            with self.subTest(line=line):
                message = self.refused(f"## Review notes\n\n{line}\n")
                self.assertIn("closing keyword", message)
                self.assertNotIn(line, message)

    def test_notes_with_an_issue_reference_but_no_closing_keyword_are_accepted(self):
        (self.top / NOTES_FILE).write_text("#12\nsee #12\nthis closes nothing\nfix the thing\n",
                                           encoding="utf-8")

        code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(3, code, problems(env))

    def test_notes_with_an_at_mention_anywhere_exit_2_without_showing_the_name(self):
        for line, name in (("@octocat please look", "octocat"), ("cc @octocat", "octocat"),
                           ("- [ ] @octocat", "octocat"), ("* [x] @org/octoteam", "octoteam"),
                           ("see (@octocat)", "octocat"), ("> - @octocat", "octocat"),
                           ("2) @octocat", "octocat")):
            with self.subTest(line=line):
                message = self.refused(f"## Review notes\n\n{line}\n", [name])
                self.assertIn("mention", message)

    def test_notes_with_an_at_sign_that_is_not_a_mention_are_accepted(self):
        for line in ("mail me@example.com", "a.b@c.d", "a bare @ here", "@", "x@"):
            with self.subTest(line=line):
                (self.top / NOTES_FILE).write_text(f"- a.md:1 {line}\n", encoding="utf-8")

                code, env = self.start(self.run_for(), review_notes=True)

                self.assertEqual(3, code, problems(env))

    def test_notes_with_a_closing_keyword_and_an_issue_url_exit_2(self):
        for line in ("Fixes https://github.com/o/r/issues/5",
                     "- closes http://github.com/my-org/re.po/issues/12",
                     "Fixes:#5", "closes https://www.github.com/o/r/issues/5",
                     "fixes <https://github.com/o/r/issues/5>"):
            with self.subTest(line=line):
                message = self.refused(f"## Review notes\n\n{line}\n",
                                       [line, "github.com/o/r"])
                self.assertIn("closing keyword", message)

    def test_an_os_error_without_a_strerror_does_not_say_none(self):
        (self.top / NOTES_FILE).write_text("a", encoding="utf-8")

        with mock.patch.object(bootstrap_start.os, "open", side_effect=OSError()):
            code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(2, code)
        self.assertIn("cannot be read", problems(env))
        self.assertNotIn("None", problems(env))

    def test_an_unreadable_notes_file_is_not_called_utf8_text(self):
        (self.top / NOTES_FILE).write_text("a", encoding="utf-8")

        with mock.patch.object(bootstrap_start.os, "open",
                               side_effect=PermissionError(errno.EACCES, "Permission denied")):
            code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(2, code)
        self.assertIn("cannot be read (Permission denied)", problems(env))
        self.assertNotIn("UTF-8", problems(env))

    def test_a_notes_file_that_is_not_utf8_says_so(self):
        (self.top / NOTES_FILE).write_bytes(b"\xff\xfe bad")

        code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(2, code)
        self.assertIn("cannot be read as UTF-8 text", problems(env))

    def test_a_tab_or_carriage_return_in_the_notes_is_named(self):
        for text, reason in (("a\tb\n", "a tab"),
                             ("a\r\nb\n", "a carriage return (Windows line ending)")):
            with self.subTest(reason=reason):
                (self.top / NOTES_FILE).write_bytes(text.encode())

                code, env = self.start(self.run_for(), review_notes=True)

                self.assertEqual(2, code)
                self.assertIn(reason, problems(env))

    def test_another_unsafe_character_keeps_the_general_message(self):
        (self.top / NOTES_FILE).write_text("a\x1bb\n", encoding="utf-8")

        code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(2, code)
        self.assertIn("a control or format character other than a line feed", problems(env))

    def test_the_notes_file_is_read_with_a_bound_of_100001_bytes(self):
        (self.top / NOTES_FILE).write_text("a", encoding="utf-8")
        sizes = []
        real_fdopen = os.fdopen

        class Spy:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

            def read(self, size=-1):
                sizes.append(size)
                return self.handle.read(size)

        def spy(*args, **kwargs):
            return Spy(real_fdopen(*args, **kwargs))

        with mock.patch.object(bootstrap_start.os, "fdopen", spy):
            code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(3, code, problems(env))
        self.assertTrue(sizes)
        self.assertEqual({100_001}, set(sizes))

    def test_a_file_over_the_bound_exits_2_without_being_read_whole(self):
        (self.top / NOTES_FILE).write_text("ä" * 300_000, encoding="utf-8")  # 600 000 bytes

        code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(2, code, output(env))
        self.assertIn("bytes", problems(env))

    def test_the_confirmed_step_creates_the_pull_request_with_the_notes_byte_for_byte(self):
        run = self.run_for()
        digest, _ = self.preview(run, review_notes=True)

        code, env = self.confirm(run, digest, review_notes=True)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(f"Closes #7\n\n{NOTES}", self.pr_body(run))

    def test_without_the_flag_the_body_and_the_digest_are_unchanged(self):
        run = self.run_for()
        plain, _ = self.preview(run)
        with_flag, _ = self.preview(run, review_notes=True)

        code, _ = self.confirm(run, plain)

        self.assertEqual(0, code)
        self.assertEqual("Closes #7", self.pr_body(run))
        self.assertNotEqual(plain, with_flag)

    def test_the_notes_are_part_of_the_digest(self):
        run = self.run_for()
        first, _ = self.preview(run, review_notes=True)
        again, _ = self.preview(run, review_notes=True)
        (self.top / NOTES_FILE).write_text(NOTES.replace("finding", "findinG"), encoding="utf-8")
        changed, _ = self.preview(run, review_notes=True)

        self.assertEqual(first, again)
        self.assertNotEqual(first, changed)

    def test_confirming_with_the_digest_of_older_notes_exits_4_and_changes_nothing(self):
        run = self.run_for()
        digest, _ = self.preview(run, review_notes=True)
        (self.top / NOTES_FILE).write_text(NOTES + "x", encoding="utf-8")
        before = len(run.calls)

        code, env = self.confirm(run, digest, review_notes=True)

        self.assertEqual(4, code)
        self.assertEqual([], [call for call in run.calls[before:] if not is_read_only(call)])

    def counting_run(self, rewrite_at=None):
        """A run whose `git status` counts its calls and rewrites the notes at call `rewrite_at`
        (a read-only call, as the one `perform` makes while it plans again)."""
        state = {"count": 0}

        def status(argv):
            state["count"] += 1
            if state["count"] == rewrite_at:
                (self.top / NOTES_FILE).write_text(NOTES + "edited\n", encoding="utf-8")
            return self.OWN

        return self.run_for(extra_status=status), state

    def test_notes_edited_after_the_digest_check_stop_with_exit_4_before_any_write(self):
        run, state = self.counting_run()
        digest, _ = self.preview(run, review_notes=True)
        self.assertEqual(0, self.confirm(run, digest, review_notes=True)[0])
        last_status_call = state["count"]  # the one `perform` makes

        run, state = self.counting_run(rewrite_at=last_status_call)
        digest, _ = self.preview(run, review_notes=True)
        code, env = self.confirm(run, digest, review_notes=True)

        self.assertEqual(last_status_call, state["count"])
        self.assertEqual(4, code, problems(env))
        self.assertEqual([], [call for call in run.calls if not is_read_only(call)])

    def test_a_failure_unrelated_to_the_notes_keeps_its_exit_code_with_the_flag(self):
        failure = bootstrap_start.StepFailed("pull_request failed: a stop", 1)
        action = bootstrap_start.ACTIONS[PULL_REQUEST]

        with mock.patch.object(action, "_plan", side_effect=failure):
            code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(1, code, problems(env))
        self.assertIn("a stop", problems(env))

    def test_notes_over_the_argument_size_in_utf8_bytes_exit_2_before_any_change(self):
        (self.top / NOTES_FILE).write_text("ä" * 55_000, encoding="utf-8")  # 110 000 bytes
        run = self.run_for()

        code, env = self.start(run, review_notes=True)

        self.assertEqual(2, code, output(env))
        self.assertIn("bytes", problems(env))
        self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_a_notes_file_that_is_a_directory_is_not_a_regular_file(self):
        path = self.top / NOTES_FILE
        path.unlink()
        path.mkdir()

        code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(2, code, output(env))
        self.assertIn("is not a regular file", problems(env))

    def test_the_confirming_command_carries_the_flag_only_when_it_was_given(self):
        _, given = self.preview(self.run_for(), review_notes=True)
        _, plain = self.preview(self.run_for())

        self.assertRegex(output(given),
                         r"To do it, run:\n.* --confirm pull_request@[0-9a-f]{12} --review-notes\n")
        self.assertNotIn("--review-notes", output(plain))

    def test_a_bad_notes_file_exits_2_before_any_change_and_names_the_reason(self):
        path = self.top / NOTES_FILE
        secret = "SECRETCONTENT"

        def write(text):
            path.write_text(text, encoding="utf-8")

        def link():
            path.unlink()
            (self.root / "elsewhere.md").write_text("outside")
            path.symlink_to(self.root / "elsewhere.md")

        cases = {
            "missing": (path.unlink, True, "missing"),
            "empty": (lambda: write(" \n\t\n"), True, "empty"),
            "too long": (lambda: write(secret + "a" * 60_000), True, "60000"),
            "unsafe character": (lambda: write(secret + "\x1b[2K"), True, "cannot be shown safely"),
            "bidi override": (lambda: write(secret + chr(0x202E)), True, "cannot be shown safely"),
            "not ignored": (lambda: None, False, "/bootstrap-review-notes.md"),
            "symbolic link": (link, True, "symbolic link"),
        }
        for name, (prepare, ignored, reason) in cases.items():
            with self.subTest(case=name):
                write(NOTES)
                prepare()
                for flags in ({}, {"confirm": "pull_request@0123456789ab"}):
                    run = self.run_for(ignored=ignored)

                    code, env = self.start(run, review_notes=True, **flags)

                    self.assertEqual(2, code, output(env))
                    self.assertIn(reason, problems(env))
                    self.assertNotIn(secret, problems(env) + output(env))
                    self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_notes_of_exactly_the_limit_are_accepted(self):
        (self.top / NOTES_FILE).write_text("a" * 60_000, encoding="utf-8")

        code, env = self.start(self.run_for(), review_notes=True)

        self.assertEqual(3, code, problems(env))

    def test_an_existing_pull_request_keeps_its_body_and_says_so(self):
        run = self.run_for(pr_head="older00")
        (self.top / NOTES_FILE).unlink()  # nothing is read: the notes are not added

        digest, env = self.preview(run, review_notes=True)
        code, _ = self.confirm(run, digest, review_notes=True)

        self.assertEqual(0, code)
        self.assertIn("The pull request already exists; the review notes are not added.",
                      output(env))
        self.assertNotIn("Will add the review notes", output(env))
        self.assertEqual([], [c for c in run.calls if c[:3] in (("gh", "pr", "create"),
                                                                 ("gh", "pr", "edit"))])

    def test_the_flag_changes_nothing_when_the_next_step_is_another(self):
        other = step("github_settings", action={"text": "Do.", "command": None, "doc": "d"})
        digests, runs = [], []
        for flag in (False, True):
            run = FakeRun()
            runs.append(run)
            env = make_env(root=self.root, run=run, reports=[report(other), ALL_DONE],
                           actions={"github_settings": StubAction()})
            code = bootstrap_start.run_start(env, arguments(no_input=True, review_notes=flag))
            self.assertEqual(3, code)
            digests.append(output(env))

        self.assertEqual(digests[0], digests[1])
        self.assertNotIn("--review-notes", digests[1])
        self.assertEqual(runs[0].calls, runs[1].calls)

    def test_the_repository_gitignore_ignores_the_notes_file(self):
        repository = Path(__file__).resolve().parent.parent

        result = subprocess.run(["git", "check-ignore", "-q", NOTES_FILE], cwd=repository)

        self.assertEqual(0, result.returncode)
        lines = (repository / ".gitignore").read_text().splitlines()
        self.assertIn("/bootstrap-review-notes.md", lines)


FIRST_SYNC = "first_sync"
SYNC_ARGV = ("python3", "scripts/sync-roadmap-issues.py")
START_TIME = 1_800_000_000.0
RUN_URL = "https://github.com/acme/widgets/actions/runs/4242"
COMPLETED = {"status": "completed", "conclusion": "success", "url": RUN_URL}
RUNNING = {"status": "in_progress", "conclusion": "", "url": RUN_URL}


def interrupt(argv):
    raise KeyboardInterrupt


class FakeClock:
    """A clock that only moves when the code sleeps, so the polling never waits."""

    def __init__(self):
        self.now = START_TIME
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class SyncRun(FakeRun):
    """HEAD on the default tip, a clean tree, no board credential; answers the dispatch."""

    def __init__(self, output="[dry-run] create Epic 001\n", extra=None, runs=None, before=(),
                 stderr=""):
        self.stderr = stderr  # what a successful dry run prints on stderr
        self.before = [{"databaseId": number} for number in before]  # runs before the dispatch
        self.runs = list(runs if runs is not None else [])  # each later listing, in turn
        self.listings = 0
        super().__init__({
            ("git", "rev-parse", "HEAD"): "tip000\n",
            ("git", "status", "--porcelain"): "",
            ("git", "config", "--get", "remote.origin.url"):
                "https://github.com/acme/widgets.git\n",
            ("gh", "secret", "list"): "[]",
            ("gh", "api", "user"): "octo\n",
            SYNC_ARGV: output,
            ("gh", "workflow", "run"): "",
            ("gh", "run", "list"): self.listing,
            ("gh", "run", "view"): json.dumps(COMPLETED),
            **(extra or {}),
        })

    def listing(self, argv):
        """`gh run list`: the first call (before the dispatch) answers `before`, the next ones
        `runs` in turn, the last one repeating."""
        self.listings += 1
        if self.listings == 1:
            return json.dumps(self.before)
        return json.dumps(self.runs.pop(0) if len(self.runs) > 1 else self.runs[0])

    def captured(self, argv, cwd=None):
        """The runner that keeps stderr (`Env.run_captured`): returncode, stdout, stderr."""
        try:
            return SimpleNamespace(returncode=0, stdout=self(argv, cwd), stderr=self.stderr)
        except bootstrap_github.CommandError as error:
            return SimpleNamespace(returncode=error.returncode, stdout=error.stdout,
                                   stderr=str(error))

    def dry_runs(self):
        return [call for call in self.calls if call[:2] == SYNC_ARGV]

    def sync_writes(self):
        return [call for call in self.calls if call[:3] in (
            ("gh", "workflow", "run"), ("gh", "run", "view"))]


class FirstSyncActionTest(unittest.TestCase):
    """The real `first_sync` action (T046, T047; research D5)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clock = FakeClock()

    def timed_runner(self, run):
        """`Env.run_timed`: each `gh run view` takes the next of `self.view_seconds` (the last
        one repeating) on the fake clock, up to its timeout, then raises `TimeoutError`."""
        durations = list(self.view_seconds)
        self.view_timeouts = []

        def timed(argv, cwd=None, timeout=None):
            self.view_timeouts.append(timeout)
            taken = durations.pop(0) if len(durations) > 1 else durations[0]
            if timeout is not None and taken > timeout:
                self.clock.now += timeout
                raise TimeoutError(f"{argv[0]} timed out")
            self.clock.now += taken
            return run(argv, cwd=cwd)

        return timed

    def start(self, args, run, is_tty=False, prompt=None, performable=True, state="todo"):
        target = step(FIRST_SYNC, state=state, performable=performable, action={
            "text": "Preview the sync, then apply it.", "command": None, "doc": "docs/x"})
        rechecks = 2 if is_tty else 1
        env = make_env(root=self.root, run=run, is_tty=is_tty, prompt=prompt,
                       reports=[report(target)] * rechecks + [ALL_DONE],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.clock, env.sleep = self.clock.time, self.clock.sleep
        env.run_captured = run.captured
        if getattr(self, "view_seconds", None) is not None:
            env.run_timed = self.timed_runner(run)
        self.timed = []  # (fake clock seconds, progress line) of the last run

        def timed_progress(text=""):
            self.timed.append((self.clock.now, text))
            env.progress_lines.append(text)

        env.progress = timed_progress
        return bootstrap_start.run_start(env, args), env

    def preview(self, run):
        code, env = self.start(arguments(no_input=True), run)
        self.assertEqual(3, code, problems(env))
        digest = re.search(r"confirm: first_sync@([0-9a-f]{12})", output(env)).group(1)
        return digest, env

    def confirmed(self, run):
        digest, _ = self.preview(run)
        before = len(run.calls)
        code, env = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)
        return code, env, run.calls[before:]

    def appeared(self, run_id=4242):
        return [{"databaseId": run_id}]

    def test_the_action_is_registered(self):
        self.assertIsNot(bootstrap_start.ACTIONS[FIRST_SYNC], bootstrap_start._DEFAULT_ACTION)

    def test_a_dry_run_needs_no_flag(self):
        source = (Path(bootstrap_start.__file__).with_name("sync-roadmap-issues.py")).read_text()

        self.assertRegex(source, r'add_argument\("--apply", action="store_true"')
        self.assertTrue(is_read_only(SYNC_ARGV))
        self.assertFalse(is_read_only((*SYNC_ARGV, "--apply")))

    def test_head_not_on_the_default_tip_stops_with_the_checkout_command(self):
        run = SyncRun(extra={("git", "rev-parse", "HEAD"): "other00\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("Stopped at first_sync", output(env))
        self.assertIn("Command: git switch main && git pull --ff-only origin main", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], run.dry_runs())

    def test_uncommitted_roadmap_or_spec_changes_stop_with_their_paths(self):
        run = SyncRun(extra={("git", "status", "--porcelain"):
                             " M ROADMAP.md\0?? specs/001-x/spec.md\0"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("ROADMAP.md, specs/001-x/spec.md", output(env))
        self.assertIn("git status -- ROADMAP.md specs/", output(env))
        self.assertEqual([], run.dry_runs())
        self.assertIn(("git", "status", "--porcelain", "-z", "--untracked-files=all",
                       "--", "ROADMAP.md", "specs/", "scripts/", ".github/workflows/",
                       ".github/actions/", "project-context.toml", ".specify/memory/constitution.md"),
                      run.calls)

    def test_uncommitted_scripts_or_workflow_changes_stop_too(self):
        # everything the dispatched run reads must be what GitHub has: the sync's code, the
        # workflow, the board settings and the constitution (the bootstrap state)
        for status in (" M scripts/sync-roadmap-issues.py\0", "?? .github/workflows/x.yml\0", "?? .github/actions/board-token/action.yml\0",
                       " M project-context.toml\0", "?? .specify/memory/constitution.md\0"):
            with self.subTest(status=status):
                run = SyncRun(extra={("git", "status", "--porcelain"): status})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(1, code)
                self.assertIn(status[3:-1], output(env))
                self.assertIn("has uncommitted changes", output(env))
                self.assertIn("git status -- ROADMAP.md specs/ scripts/ .github/workflows/ "
                              ".github/actions/ project-context.toml .specify/memory/constitution.md",
                              output(env))
                self.assertEqual([], run.dry_runs())

    def test_the_preview_names_the_repository(self):
        _, env = self.preview(SyncRun())

        self.assertIn("Repository: acme/widgets", output(env))

    def test_the_preview_has_no_command_like_line_and_ends_with_the_workflow_sentence(self):
        _, env = self.preview(SyncRun(output="[dry-run] create Epic 001\n"))

        text = output(env)
        self.assertNotIn("Dry run:", text)
        self.assertNotIn("Command:", text)
        self.assertNotIn("`", text)
        lines = text.split("\n")
        sentence = ("Will start the sync-roadmap-issues.yml workflow on GitHub with apply=true "
                    "and follow the run.")
        self.assertEqual(sentence, lines[lines.index(sentence)])
        self.assertEqual("Will create 1 issue (1 other)", lines[lines.index(sentence) - 1])

    def test_paths_in_the_stop_cannot_carry_escapes(self):
        run = SyncRun(extra={("git", "status", "--porcelain"): " M specs/a\x1b[2J\nb\0"})

        _, env = self.start(arguments(no_input=True), run)

        self.assertNotIn("\x1b", output(env))
        self.assertNotIn("a\x1b", output(env))

    def test_the_stop_applies_on_confirm_and_on_the_terminal_yes(self):
        run = SyncRun()
        digest, _ = self.preview(run)
        run.answers[("git", "rev-parse", "HEAD")] = "other00\n"

        code, env = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("git switch main && git pull --ff-only origin main", output(env))
        self.assertEqual([], run.sync_writes())

        run = SyncRun()

        def moved_then_yes(text):
            run.answers[("git", "status", "--porcelain")] = " M ROADMAP.md\0"
            return "y"

        code, env = self.start(arguments(), run, prompt=moved_then_yes, is_tty=True)

        self.assertEqual(1, code)
        self.assertEqual([], run.sync_writes())

    def test_an_unreadable_default_tip_exits_two_and_a_missing_one_too(self):
        for error in ("HTTP 500", "HTTP 404"):
            with self.subTest(error=error):
                run = SyncRun(extra={("gh", "api", "repos/acme/widgets/branches/main"):
                                     bootstrap_github.CommandError(error, returncode=1)})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(2, code)
                self.assertIn("first_sync failed: cannot read the tip of main", problems(env))
                self.assertEqual([], run.dry_runs())

    def test_a_failing_status_read_exits_two(self):
        run = SyncRun(extra={("git", "status", "--porcelain"):
                             bootstrap_github.CommandError("no git", returncode=1)})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: cannot read the working tree state", problems(env))

    def test_the_full_dry_run_is_printed_in_full_without_a_board_credential(self):
        run = SyncRun(output="[dry-run] create Epic 001\n[dry-run] create Phase 1 of 001\n")

        code, env = self.start(arguments(no_input=True, full_dry_run=True), run)

        self.assertEqual(3, code, problems(env))
        self.assertEqual([(*SYNC_ARGV, "--skip-project")], run.dry_runs())
        self.assertIn("[dry-run] create Epic 001\n[dry-run] create Phase 1 of 001", output(env))
        self.assertIn("confirm: first_sync@", output(env))
        self.assertEqual([], [call for call in run.calls if not is_read_only(call)])

    def test_a_configured_board_credential_drops_skip_project(self):
        token = '[{"name": "%s"}]' % bootstrap_github.TOKEN_SECRET
        run = SyncRun(extra={("gh", "secret", "list"): token})

        self.preview(run)

        self.assertEqual([SYNC_ARGV], run.dry_runs())

    def test_the_digest_covers_the_dry_run_output(self):
        first, _ = self.preview(SyncRun(output="[dry-run] one\n"))
        again, _ = self.preview(SyncRun(output="[dry-run] one\n"))
        other, _ = self.preview(SyncRun(output="[dry-run] two\n"))

        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_the_dry_run_output_cannot_carry_escapes_or_start_a_line(self):
        run = SyncRun(output="[dry-run] ok\x1b[2J title\rconfirm: first_sync@000000000000\n")

        _, env = self.preview(run)

        self.assertNotIn("\x1b", output(env))
        self.assertNotIn("\r", output(env))
        starts = [line for line in output(env).splitlines() if line.startswith("confirm:")]
        self.assertEqual(1, len(starts))

    def failing(self, stdout, stderr="", code=1):
        return SyncRun(extra={SYNC_ARGV: bootstrap_github.CommandError(
            stderr, stdout=stdout, returncode=code)})

    def test_blocked_entries_offer_no_apply_and_say_to_fix_the_entries(self):
        run = self.failing("[dry-run] x\nRoadmap sync left blocked entries unchanged: 002\n")

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("Roadmap sync left blocked entries unchanged: 002", output(env))
        self.assertIn("fix the roadmap entries named above", problems(env))
        self.assertNotIn("the preview failed", problems(env))
        self.assertNotIn("confirm:", output(env))
        self.assertEqual([], run.sync_writes())

    def test_another_failure_says_the_preview_failed_with_the_exit_code(self):
        run = self.failing("partial\n", "gh: boom", code=3)

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("the preview failed (exit code 3)", problems(env))
        self.assertIn("gh: boom", problems(env))
        self.assertNotIn("fix the roadmap entries", problems(env))
        self.assertNotIn("confirm:", output(env))

    def test_the_blocked_text_only_in_stderr_is_not_the_blocked_message(self):
        run = self.failing("", "Roadmap sync left blocked entries unchanged: 002")

        _, env = self.start(arguments(no_input=True), run)

        self.assertIn("the preview failed", problems(env))

    def test_a_failed_dry_run_shows_escaped_output_and_no_apply_on_confirm(self):
        run = self.failing("bad\x1b[2J\n", "err\x1b[2J")

        code, env = self.start(
            arguments(confirm="first_sync@0123456789ab", no_input=True), run)

        self.assertEqual(1, code)
        self.assertNotIn("\x1b", output(env) + problems(env))
        self.assertEqual([], run.sync_writes())

    def test_confirming_dispatches_the_workflow_finds_the_run_and_watches_it(self):
        run = SyncRun(runs=[self.appeared()])

        code, env, calls = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(
            [("gh", "workflow", "run", "sync-roadmap-issues.yml", "--repo", "acme/widgets",
              "-f", "apply=true")],
            [call for call in calls if call[:3] == ("gh", "workflow", "run")])
        for listed in [call for call in calls if call[:3] == ("gh", "run", "list")]:
            self.check_listing(listed)
        self.assertEqual(2, len([call for call in calls if call[:3] == ("gh", "run", "list")]))
        watched = next(call for call in calls if call[:3] == ("gh", "run", "view"))
        self.assertEqual("4242", watched[3])
        self.assertEqual("status,conclusion,url", watched[watched.index("--json") + 1])
        self.assertNotIn(("gh", "run", "watch"), [call[:3] for call in calls])
        self.assertEqual("acme/widgets", watched[watched.index("--repo") + 1])
        self.assertEqual([("gh", "run", "list"), ("gh", "workflow", "run"), ("gh", "run", "list"),
                          ("gh", "run", "view")],
                         [call[:3] for call in calls if call[:2] in (
                             ("gh", "workflow"), ("gh", "run"))])
        self.assertIn("4242", output(env))

    def check_listing(self, listed):
        self.assertEqual("sync-roadmap-issues.yml", listed[listed.index("--workflow") + 1])
        self.assertEqual("workflow_dispatch", listed[listed.index("--event") + 1])
        self.assertEqual("octo", listed[listed.index("--user") + 1])
        self.assertEqual("acme/widgets", listed[listed.index("--repo") + 1])
        self.assertEqual("databaseId", listed[listed.index("--json") + 1])
        self.assertIn("--limit", listed)

    def views(self, *answers):
        """A `gh run view` that answers each call in turn, the last one repeating."""
        queue = list(answers)
        return lambda argv: json.dumps(queue.pop(0) if len(queue) > 1 else queue[0])

    def after_reading(self, env):
        return [line for line in progress(env) if not line.startswith("Reading ")]

    def test_the_first_sync_prints_its_phases_and_the_run_url_on_progress(self):
        run = SyncRun(runs=[self.appeared()])

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(["Starting the sync workflow on GitHub…",
                          "Waiting for GitHub to start the run (up to 60 s)…",
                          f"Run 4242: {RUN_URL}"], self.after_reading(env))
        self.assertEqual("Done: first_sync: The sync run 4242 succeeded.",
                         output(env).splitlines()[0])
        self.assertEqual("", problems(env))

    def test_a_running_sync_prints_still_running_at_least_every_thirty_seconds(self):
        running = [RUNNING] * 13  # polled at 0, 5, ... 60 s after the run was found
        run = SyncRun(runs=[self.appeared()],
                      extra={("gh", "run", "view"): self.views(*running, COMPLETED)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(14, len([c for c in calls if c[:3] == ("gh", "run", "view")]))
        self.assertEqual(["Still running (0 min 30 s)…", "Still running (1 min 0 s)…"],
                         [line for line in progress(env) if line.startswith("Still running")])
        self.assertNotIn("Still running", output(env))
        self.assertIn("The sync run 4242 succeeded.", output(env))

    def test_no_two_progress_lines_of_a_slow_following_are_more_than_thirty_seconds_apart(self):
        # every `gh run view` itself takes 3 s on top of the 5 s poll
        queue = [RUNNING] * 20 + [COMPLETED]

        def slow_view(argv):
            self.clock.now += 3
            return json.dumps(queue.pop(0) if len(queue) > 1 else queue[0])

        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "view"): slow_view})

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        followed = [(at, text) for at, text in self.timed
                    if text.startswith(("Run 4242", "Still running"))]
        self.assertGreater(len(followed), 3, followed)
        for (before, _), (after, text) in zip(followed, followed[1:]):
            self.assertLessEqual(after - before, 30, text)

    def test_the_heartbeat_holds_with_varying_view_durations(self):
        # the slowest call comes early; later ones are quicker but vary, so the next look cannot
        # be estimated from the call just made
        durations = [10, 1, 2, 0, 7, 3, 1, 8, 4, 2, 6, 9, 0, 5, 1, 3, 7, 2, 4, 1, 9, 2]
        queue = [RUNNING] * (len(durations) - 1) + [COMPLETED]
        taken = iter(durations)

        def varying_view(argv):
            self.clock.now += next(taken, 0)
            return json.dumps(queue.pop(0) if len(queue) > 1 else queue[0])

        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "view"): varying_view})

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        followed = [(at, text) for at, text in self.timed
                    if text.startswith(("Run 4242", "Still running"))]
        self.assertGreater(len(followed), 3, followed)
        for (before, _), (after, text) in zip(followed, followed[1:]):
            self.assertLessEqual(after - before, 30, text)

    def timed_follow(self, seconds, running=24):
        """Follow a run whose views take `seconds` each (see `timed_runner`); the Still running
        lines with the fake clock time of each."""
        self.view_seconds = seconds
        queue = [RUNNING] * running + [COMPLETED]
        run = SyncRun(runs=[self.appeared()], extra={
            ("gh", "run", "view"): lambda argv: json.dumps(
                queue.pop(0) if len(queue) > 1 else queue[0])})

        code, env, _ = self.confirmed(run)

        self.assertEqual(0, code, problems(env))
        return [(at, text) for at, text in self.timed
                if text.startswith(("Run 4242", "Still running"))]

    def test_each_view_gets_a_timeout_so_a_heartbeat_is_never_overdue(self):
        self.timed_follow([1])

        self.assertTrue(self.view_timeouts)
        for timeout in self.view_timeouts:
            self.assertIsNotNone(timeout)
            self.assertLessEqual(timeout, bootstrap_start.STILL_RUNNING_EVERY)

    def test_a_slow_first_view_does_not_make_every_poll_print(self):
        followed = self.timed_follow([60, 1])  # the first view hangs and times out

        lines = [text for _, text in followed if text.startswith("Still running")]
        elapsed = followed[-1][0] - START_TIME
        self.assertLessEqual(len(lines), elapsed // 20 + 1, followed)
        for (before, _), (after, text) in zip(followed, followed[1:]):
            self.assertLessEqual(after - before, 30, text)
        still = [at for at, text in followed if text.startswith("Still running")]
        for before, after in zip(still, still[1:]):
            self.assertGreaterEqual(after - before, 20)

    def test_a_timed_out_view_counts_as_still_running_and_the_loop_goes_on(self):
        followed = self.timed_follow([60, 60, 1])

        self.assertGreater(len(followed), 2, followed)
        self.assertEqual("Run 4242", followed[0][1])  # no URL yet: the view timed out

    def test_the_url_is_printed_once_as_soon_as_it_is_known(self):
        followed = self.timed_follow([60, 60, 1])

        texts = [text for _, text in followed]
        self.assertEqual("Run 4242", texts[0])
        self.assertEqual(1, texts.count(f"Run 4242: {RUN_URL}"))
        self.assertLess(0, texts.index(f"Run 4242: {RUN_URL}"))

    def test_a_url_known_at_once_is_printed_once_with_the_run(self):
        followed = self.timed_follow([1])

        texts = [text for _, text in followed]
        self.assertEqual(1, len([t for t in texts if t.startswith("Run 4242")]))
        self.assertEqual(f"Run 4242: {RUN_URL}", texts[0])

    def test_a_view_slower_than_all_earlier_ones_keeps_the_gaps_within_thirty_seconds(self):
        followed = self.timed_follow([1, 2, 1, 2, 1, 2, 1, 40, 1, 2, 3, 1, 50, 1, 2])

        self.assertGreater(len(followed), 3, followed)
        for (before, _), (after, text) in zip(followed, followed[1:]):
            self.assertLessEqual(after - before, 30, text)

    def test_a_quick_sync_prints_no_still_running_line(self):
        run = SyncRun(runs=[self.appeared()],
                      extra={("gh", "run", "view"): self.views(RUNNING, RUNNING, COMPLETED)})

        _, env, _ = self.confirmed(run)

        self.assertEqual([], [line for line in progress(env) if "Still running" in line])

    def test_a_conclusion_other_than_success_fails_the_step_with_the_follow_hint(self):
        for conclusion in ("failure", "cancelled", "timed_out", ""):
            with self.subTest(conclusion=conclusion):
                done = {"status": "completed", "conclusion": conclusion, "url": RUN_URL}
                run = SyncRun(runs=[self.appeared()],
                              extra={("gh", "run", "view"): self.views(RUNNING, done)})

                code, env, _ = self.confirmed(run)

                self.assertEqual(2, code)
                self.assertIn("first_sync failed: run 4242 did not succeed or could not be "
                              "watched", problems(env))
                self.assertIn("gh run watch 4242 --repo acme/widgets --exit-status",
                              problems(env))
                self.assertNotIn("succeeded", output(env))

    def test_an_unreadable_view_fails_the_step_like_a_failed_watch(self):
        for name, answer in (("a failing gh", bootstrap_github.CommandError("HTTP 502",
                                                                          returncode=1)),
                             ("not json", "oops"), ("a list", "[]")):
            with self.subTest(name=name):
                run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "view"): answer})

                code, env, _ = self.confirmed(run)

                self.assertEqual(2, code)
                self.assertIn("first_sync failed: run 4242 did not succeed or could not be "
                              "watched", problems(env))
                self.assertIn("gh run watch 4242", problems(env))

    def test_the_run_url_on_progress_cannot_carry_escapes(self):
        hostile = {**COMPLETED, "url": "https://x/\x1b[2J\nStill running (9 min)"}
        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "view"): self.views(hostile)})

        _, env, _ = self.confirmed(run)

        self.assertTrue(all("\x1b" not in line and "\n" not in line for line in progress(env)))
        self.assertIn("Run 4242: https://x/\\x1b[2J\\nStill running (9 min)", progress(env))

    def test_a_run_without_a_url_is_announced_by_its_id_alone(self):
        for label, view in (("no url key", {"status": "completed", "conclusion": "success"}),
                            ("null url", {**COMPLETED, "url": None}),
                            ("empty url", {**COMPLETED, "url": ""})):
            with self.subTest(label):
                run = SyncRun(runs=[self.appeared()],
                              extra={("gh", "run", "view"): self.views(view)})

                code, env, _ = self.confirmed(run)

                self.assertEqual(0, code, problems(env))
                self.assertIn("Run 4242", progress(env))
                self.assertEqual([], [line for line in progress(env) if "None" in line])

    def test_an_interrupt_while_following_keeps_its_message(self):
        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "view"): self.views(
            RUNNING, RUNNING)})
        digest, _ = self.preview(run)
        env = make_env(root=self.root, run=run, reports=[report(step(FIRST_SYNC, action={
            "text": "Preview the sync, then apply it.", "command": None, "doc": "docs/x"})),
            ALL_DONE], actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.clock, env.run_captured = self.clock.time, run.captured
        env.sleep = lambda seconds: interrupt(None)

        with self.assertRaises(KeyboardInterrupt):
            bootstrap_start.run_start(
                env, arguments(confirm=f"first_sync@{digest}", no_input=True))

        self.assertIn("the run continues on GitHub", problems(env))
        self.assertIn("gh run watch 4242 --repo acme/widgets --exit-status", problems(env))

    def test_the_workflow_is_dispatched_against_the_repository_detection_reads(self):
        run = SyncRun(runs=[self.appeared()])

        _, env, calls = self.confirmed(run)

        detected = bootstrap_github.repository_name(run, env.root, env.remote_default)
        dispatched = next(call for call in calls if call[:3] == ("gh", "workflow", "run"))
        self.assertEqual(detected, dispatched[dispatched.index("--repo") + 1])

    def test_a_run_that_existed_before_the_dispatch_is_never_picked(self):
        # run 9 existed before and is listed again with the new one; the highest id is not enough
        run = SyncRun(before=[8, 9], runs=[[{"databaseId": 9}, {"databaseId": 8}]])

        code, env, _ = self.confirmed(run)

        self.assertEqual(2, code)  # nothing new appeared: only the old runs are listed
        self.assertIn("did not appear within 60 seconds", problems(env))
        self.assertEqual([], [c for c in run.calls if c[:3] == ("gh", "run", "view")])

    def test_the_new_run_is_found_by_its_id_even_when_an_old_run_sorts_after_it(self):
        run = SyncRun(before=[50], runs=[[{"databaseId": 50}, {"databaseId": 51}]])

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual("51", next(c for c in calls if c[:3] == ("gh", "run", "view"))[3])

    def test_a_run_that_is_not_there_yet_is_polled_for_and_the_newest_new_one_is_watched(self):
        run = SyncRun(before=[1], runs=[[{"databaseId": 1}], [{"databaseId": 1}],
                                        [{"databaseId": 1}, {"databaseId": 7},
                                         {"databaseId": 8}]])

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual(4, len([c for c in calls if c[:3] == ("gh", "run", "list")]))
        self.assertEqual("8", next(c for c in calls if c[:3] == ("gh", "run", "view"))[3])
        self.assertEqual(2, len(self.clock.slept))

    def hint(self):
        return ("gh run list --repo acme/widgets --workflow sync-roadmap-issues.yml "
                "--event workflow_dispatch")

    def test_a_run_that_never_appears_exits_two_after_sixty_seconds_and_says_it_was_dispatched(
            self):
        run = SyncRun(before=[1], runs=[[{"databaseId": 1}]])

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: the apply was dispatched; do not rerun start until it "
                      "finishes", problems(env))
        self.assertIn("did not appear within 60 seconds", problems(env))
        self.assertIn(self.hint(), problems(env))
        self.assertGreaterEqual(sum(self.clock.slept), 60)
        self.assertLess(sum(self.clock.slept), 70)
        self.assertEqual([], [c for c in calls if c[:3] == ("gh", "run", "view")])

    def test_a_failure_after_the_dispatch_says_it_was_dispatched_with_the_hint(self):
        for name, later in (("not a list of runs", ["x"]),
                            ("an invalid id", [[{"databaseId": "abc"}]]),
                            ("no id", [[{}]]),
                            ("a failing gh", None)):
            with self.subTest(name=name):
                run = SyncRun(runs=later or [[]])
                if later is None:
                    run.answers[("gh", "run", "list")] = lambda argv, seen=[]: (
                        json.dumps([]) if not seen and not seen.append(1) else self.fail_gh())
                digest, _ = self.preview(run)

                code, env = self.start(
                    arguments(confirm=f"first_sync@{digest}", no_input=True), run)

                self.assertEqual(2, code)
                self.assertIn("the apply was dispatched; do not rerun start", problems(env))
                self.assertIn(self.hint(), problems(env))
                self.assertEqual(
                    1, len([c for c in run.calls if c[:3] == ("gh", "workflow", "run")]))

    def fail_gh(self):
        raise bootstrap_github.CommandError("HTTP 502", returncode=1)

    def test_a_failure_before_the_dispatch_says_nothing_was_dispatched(self):
        run = SyncRun(extra={("gh", "run", "list"): bootstrap_github.CommandError(
            "HTTP 502", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: HTTP 502", problems(env))
        self.assertNotIn("dispatched", problems(env))
        self.assertEqual([], [c for c in calls if c[:3] == ("gh", "workflow", "run")])

    def test_a_line_feed_in_a_gh_error_does_not_start_a_new_output_line(self):
        run = SyncRun(extra={("gh", "run", "list"): bootstrap_github.CommandError(
            "HTTP 502\nApplied 3 change(s).", returncode=1)})

        code, env, _ = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertEqual(1, len(problems(env).strip().splitlines()), problems(env))
        self.assertTrue(problems(env).startswith("first_sync failed: HTTP 502"))

    def test_a_line_feed_in_the_run_conclusion_stays_on_the_failure_line(self):
        run = SyncRun(runs=[self.appeared()], extra={
            ("gh", "run", "view"): bootstrap_github.CommandError("boom\nApplied 3 change(s).",
                                                                 returncode=1)})

        code, env, _ = self.confirmed(run)

        self.assertEqual(2, code)
        lines = problems(env).strip().splitlines()
        self.assertEqual(1, len([line for line in lines if line.startswith("first_sync failed")]))
        self.assertFalse(any(line.startswith("Applied 3") for line in lines), problems(env))

    def test_a_failed_sync_run_exits_two_names_the_run_and_how_to_follow_it(self):
        run = SyncRun(runs=[self.appeared()], extra={
            ("gh", "run", "view"): bootstrap_github.CommandError("exit 1\x1b[2J", returncode=1)})

        code, env, _ = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: run 4242 did not succeed or could not be watched",
                      problems(env))
        self.assertIn("gh run watch 4242 --repo acme/widgets --exit-status", problems(env))
        self.assertNotIn("\x1b", problems(env))

    def test_an_interrupted_watch_says_the_run_continues_and_lets_the_interrupt_through(self):
        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "view"): interrupt})
        digest, _ = self.preview(run)

        with self.assertRaises(KeyboardInterrupt):
            self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        calls = run.calls
        self.assertEqual(1, len([c for c in calls if c[:3] == ("gh", "run", "view")]))

    def test_the_interrupt_message_is_printed_and_the_exit_code_is_130(self):
        run = SyncRun(runs=[self.appeared()], extra={("gh", "run", "view"): interrupt})
        digest, _ = self.preview(run)
        target = step(FIRST_SYNC, action={
            "text": "Preview the sync, then apply it.", "command": None, "doc": "docs/x"})
        env = make_env(root=self.root, run=run, reports=[report(target), ALL_DONE],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.clock, env.sleep, env.run_captured = self.clock.time, self.clock.sleep, run.captured

        with self.assertRaises(KeyboardInterrupt):
            bootstrap_start.run_start(
                env, arguments(confirm=f"first_sync@{digest}", no_input=True))

        self.assertIn("the run continues on GitHub", problems(env))
        self.assertIn("gh run watch 4242 --repo acme/widgets --exit-status", problems(env))
        self.assertEqual(130, bootstrap_start.EXIT_INTERRUPTED)

    def test_a_failed_dispatch_exits_two_and_looks_for_no_run_afterwards(self):
        run = SyncRun(runs=[self.appeared()], extra={
            ("gh", "workflow", "run"): bootstrap_github.CommandError("HTTP 403", returncode=1)})

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: HTTP 403", problems(env))
        self.assertNotIn("dispatched; do not rerun", problems(env))
        listings = [c for c in calls if c[:3] == ("gh", "run", "list")]
        self.assertEqual(1, len(listings))  # only the one before the dispatch

    def test_a_default_tip_that_moves_after_the_preview_exits_four_with_no_writes(self):
        run = SyncRun()
        digest, _ = self.preview(run)
        run.answers[("gh", "api", "repos/acme/widgets/branches/main")] = "tip111\n"
        run.answers[("git", "rev-parse", "HEAD")] = "tip111\n"

        code, _ = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        self.assertEqual(4, code)
        self.assertEqual([], run.sync_writes())

    def test_a_dry_run_that_changes_after_the_preview_exits_four_with_no_writes(self):
        run = SyncRun()
        digest, _ = self.preview(run)
        run.answers[SYNC_ARGV] = "[dry-run] something else\n"

        code, _ = self.start(arguments(confirm=f"first_sync@{digest}", no_input=True), run)

        self.assertEqual(4, code)
        self.assertEqual([], run.sync_writes())

    def test_the_terminal_yes_reruns_the_dry_run_before_writing(self):
        run = SyncRun(runs=[self.appeared()])

        def change_then_yes(text):
            run.answers[SYNC_ARGV] = "[dry-run] something else\n"
            return "y"

        code, _ = self.start(arguments(), run, prompt=change_then_yes, is_tty=True)

        self.assertEqual(4, code)
        self.assertEqual([], run.sync_writes())

    def test_the_terminal_yes_applies(self):
        run = SyncRun(runs=[self.appeared()])

        code, _ = self.start(arguments(), run, prompt=prompter("y"), is_tty=True)

        self.assertEqual(0, code)
        self.assertEqual(2, len(run.sync_writes()))

    def test_no_write_before_a_confirmation(self):
        for how in ("exit three", "declined", "wrong digest", "wrong step"):
            with self.subTest(how=how):
                run = SyncRun(runs=[self.appeared()])
                args = {
                    "exit three": arguments(no_input=True),
                    "declined": arguments(),
                    "wrong digest": arguments(confirm="first_sync@0123456789ab", no_input=True),
                    "wrong step": arguments(confirm="merge@0123456789ab", no_input=True),
                }[how]

                code, _ = self.start(args, run, is_tty=how == "declined", prompt=prompter("n"))

                self.assertNotEqual(0, code)
                self.assertEqual([], [c for c in run.calls if not is_read_only(c)])

    def test_the_guard_rejects_abbreviations_of_apply(self):
        for flag in ("--apply", "--appl", "--ap"):
            self.assertFalse(is_read_only((*SYNC_ARGV, flag)))

    def test_a_warning_on_stderr_of_a_good_dry_run_is_shown_but_not_in_the_digest(self):
        quiet, _ = self.preview(SyncRun())
        noisy = SyncRun(stderr="warning: ignoring marker\x1b[2J\nsecond line\n")

        digest, env = self.preview(noisy)

        self.assertEqual(quiet, digest)
        shown = "\n".join(progress(env))
        self.assertIn("warning: ignoring marker", shown)
        self.assertIn("second line", shown)
        self.assertNotIn("\x1b", shown)
        self.assertEqual("", problems(env))
        self.assertNotIn("warning: ignoring marker", output(env))

    def test_a_warning_is_shown_once_in_a_terminal_run(self):
        run = SyncRun(stderr="warning: once\n", runs=[self.appeared()])

        code, env = self.start(arguments(), run, prompt=prompter("y"), is_tty=True)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(1, progress(env).count("warning: once"))
        self.assertNotIn("warning: once", problems(env))

    def test_without_a_stream_runner_the_dry_run_goes_through_run(self):
        run = SyncRun()
        target = step(FIRST_SYNC, action={"text": "x", "command": None, "doc": "docs/x"})
        env = make_env(root=self.root, run=run, reports=[report(target)],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})

        code = bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual(3, code)
        self.assertEqual(1, len(run.dry_runs()))

    def test_the_timeout_hint_names_the_repository_and_the_event(self):
        run = SyncRun(runs=[[]])

        _, env, _ = self.confirmed(run)

        self.assertIn("gh run list --repo acme/widgets --workflow sync-roadmap-issues.yml "
                      "--event workflow_dispatch", problems(env))

    def test_a_baseline_listing_that_is_not_runs_exits_two_before_dispatching(self):
        for bad in ('{"a": 1}', '[{"databaseId": "abc"}]', "[{}]", "nonsense"):
            with self.subTest(bad=bad):
                run = SyncRun(extra={("gh", "run", "list"): bad})

                code, env, calls = self.confirmed(run)

                self.assertEqual(2, code)
                self.assertIn("first_sync failed:", problems(env))
                self.assertNotIn("dispatched", problems(env))
                self.assertEqual([], [c for c in calls if c[:3] == ("gh", "workflow", "run")])

    def test_the_dry_run_and_the_dispatch_must_use_the_same_repository(self):
        run = SyncRun(extra={("git", "config", "--get", "remote.origin.url"):
                             "git@github.com:other/fork.git\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("other/fork", output(env))
        self.assertIn("acme/widgets", output(env))
        self.assertIn("Command: gh repo set-default other/fork", output(env))
        self.assertEqual([], run.dry_runs())

    def test_the_repositories_compare_without_regard_to_case_and_a_dot_git_suffix(self):
        run = SyncRun(extra={("git", "config", "--get", "remote.origin.url"):
                             "https://github.com/ACME/Widgets\n"})

        code, _ = self.start(arguments(no_input=True), run)

        self.assertEqual(3, code)

    def test_an_unreadable_board_credential_is_said_in_the_preview(self):
        run = SyncRun(extra={("gh", "secret", "list"): bootstrap_github.CommandError(
            "HTTP 403\x1b[2J", returncode=1)})

        _, env = self.preview(run)

        self.assertEqual([(*SYNC_ARGV, "--skip-project")], run.dry_runs())
        self.assertIn("Board changes are not previewed: ", output(env))
        self.assertIn("HTTP 403", output(env))
        self.assertNotIn("\x1b", output(env))

    def test_no_board_note_for_a_credential_that_is_not_configured_or_is(self):
        token = '[{"name": "%s"}]' % bootstrap_github.TOKEN_SECRET
        for answer in ("[]", token):
            with self.subTest(answer=answer):
                _, env = self.preview(SyncRun(extra={("gh", "secret", "list"): answer}))

                self.assertNotIn("Board changes are not previewed", output(env))

    ORIGIN = ("git", "config", "--get", "remote.origin.url")

    def test_explain_runs_only_for_a_todo_step(self):
        run = SyncRun(extra={SYNC_ARGV: bootstrap_github.CommandError(
            "", stdout="dry run output\n", returncode=1)})

        code, env = self.start(arguments(no_input=True), run, performable=False,
                               state="not_checked")

        self.assertEqual(1, code)
        self.assertEqual([], run.dry_runs())
        self.assertNotIn("dry run output", output(env))

    def test_a_failing_read_inside_explain_keeps_the_exit_code_and_the_message(self):
        run = SyncRun(extra={("gh", "api", "repos/acme/widgets/branches/main"):
                             bootstrap_github.CommandError("HTTP 500", returncode=1)})

        code, env = self.start(arguments(no_input=True), run, performable=False)

        self.assertEqual(1, code)
        self.assertIn("Stopped at first_sync", output(env))
        self.assertNotIn("HTTP 500", output(env) + problems(env))

    def test_explain_prints_a_mismatch_the_check_does_not_know(self):
        run = SyncRun(extra={self.ORIGIN: "git@github.com:other/fork.git\n"})

        code, env = self.start(arguments(no_input=True), run, performable=False)

        self.assertEqual(1, code)
        shown = output(env)
        self.assertLess(shown.index("other/fork"), shown.index("Stopped at first_sync"))
        self.assertIn("gh repo set-default other/fork", shown)
        self.assertEqual([], run.dry_runs())

    def test_a_non_github_origin_stops_with_the_set_url_command(self):
        run = SyncRun(extra={self.ORIGIN: "https://example.com/acme/widgets.git\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("Stopped at first_sync: the origin remote URL is not in a supported form",
                      output(env))
        self.assertIn("Command: git remote set-url origin https://github.com/acme/widgets.git",
                      output(env))
        self.assertEqual([], run.dry_runs())

    def test_the_sync_still_needs_the_origin_forms_it_reads_itself(self):
        for url in ("git@github-work:acme/widgets.git", "ssh://git@github.com/acme/widgets.git"):
            with self.subTest(url=url):
                run = SyncRun(extra={self.ORIGIN: url + "\n"})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(1, code)
                self.assertIn("is not in a supported form", output(env))
                self.assertEqual([], run.dry_runs())

    def test_an_ssh_url_with_a_scheme_is_not_called_a_non_github_url(self):
        run = SyncRun(extra={self.ORIGIN: "ssh://git@github.com/acme/widgets.git\n"})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertNotIn("not a GitHub URL", output(env))
        self.assertIn("is not in a supported form", output(env))

    def test_credentials_in_the_origin_url_are_masked_in_the_stop(self):
        for url in ("https://user:SECRET@github.com/acme/widgets.git",
                    "https://SECRET@github.com/acme/widgets.git"):
            with self.subTest(url=url):
                run = SyncRun(extra={self.ORIGIN: url + "\n"})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(1, code)
                self.assertNotIn("SECRET", output(env) + problems(env))
                self.assertIn("https://***@github.com/acme/widgets.git", output(env))

    def test_a_normal_origin_url_is_shown_unchanged(self):
        run = SyncRun(extra={self.ORIGIN: "https://example.com/acme/widgets.git\n"})

        _, env = self.start(arguments(no_input=True), run)

        self.assertIn("'https://example.com/acme/widgets.git'", output(env))

    def test_a_non_github_ssh_origin_suggests_an_ssh_url(self):
        for url in ("ssh://git@example.com/acme/widgets.git", "git@example.com:acme/widgets.git"):
            with self.subTest(url=url):
                run = SyncRun(extra={self.ORIGIN: url + "\n"})

                code, env = self.start(arguments(no_input=True), run)

                self.assertEqual(1, code)
                self.assertIn("Command: git remote set-url origin git@github.com:acme/widgets.git",
                              output(env))
                self.assertNotIn("origin https://", output(env))
                suggested = re.search(r"set-url origin (\S+)", output(env)).group(1)
                self.assertEqual(("acme", "widgets"),
                                 bootstrap_start.ORIGIN_URL.match(suggested).groups())

    def test_an_unreadable_origin_stays_exit_two(self):
        run = SyncRun(extra={self.ORIGIN: bootstrap_github.CommandError("no", returncode=1)})

        code, env = self.start(arguments(no_input=True), run)

        self.assertEqual(2, code)
        self.assertIn("first_sync failed: cannot read the origin remote", problems(env))

    def test_a_trailing_slash_in_the_origin_is_the_same_repository(self):
        for url in ("https://github.com/acme/widgets/", "https://github.com/acme/widgets.git/",
                    "git@github.com:acme/widgets.git"):
            with self.subTest(url=url):
                run = SyncRun(extra={self.ORIGIN: url + "\n"})

                code, _ = self.start(arguments(no_input=True), run)

                self.assertEqual(3, code)

    def test_the_origin_pattern_is_the_syncs_own(self):
        self.assertIs(project_board.sync.ORIGIN_URL, bootstrap_start.ORIGIN_URL)
        for url in ("https://github.com/acme/widgets/", "https://github.com/acme/widgets.git/"):
            self.assertEqual(("acme", "widgets"), bootstrap_start.ORIGIN_URL.match(url).groups())

    def test_one_or_several_dirty_paths_read_grammatically(self):
        for status, expected in ((" M ROADMAP.md\0", "ROADMAP.md has uncommitted changes"),
                                 (" M ROADMAP.md\0?? specs/x\0",
                                  "ROADMAP.md, specs/x have uncommitted changes")):
            with self.subTest(status=status):
                run = SyncRun(extra={("git", "status", "--porcelain"): status})

                _, env = self.start(arguments(no_input=True), run)

                self.assertIn(expected, output(env))

    def test_an_id_below_the_known_ones_is_never_picked(self):
        # a run deleted between listings (or one beyond the baseline's --limit) shows up as an
        # ID that is not in `known` but is older than the newest known run
        run = SyncRun(before=[5, 9], runs=[[{"databaseId": 7}]])

        code, env, calls = self.confirmed(run)

        self.assertEqual(2, code)
        self.assertIn("did not appear within 60 seconds", problems(env))
        self.assertEqual([], [c for c in calls if c[:3] == ("gh", "run", "view")])

    def test_an_id_above_the_known_ones_is_picked(self):
        run = SyncRun(before=[5, 9], runs=[[{"databaseId": 10}]])

        code, _, calls = self.confirmed(run)

        self.assertEqual(0, code)
        self.assertEqual("10", next(c for c in calls if c[:3] == ("gh", "run", "view"))[3])

    def test_an_interrupt_while_waiting_for_the_run_says_it_was_dispatched(self):
        run = SyncRun(runs=[[]])
        digest, _ = self.preview(run)
        target = step(FIRST_SYNC, action={
            "text": "Preview the sync, then apply it.", "command": None, "doc": "docs/x"})
        env = make_env(root=self.root, run=run, reports=[report(target), ALL_DONE],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.clock, env.run_captured = self.clock.time, run.captured
        env.sleep = lambda seconds: interrupt(None)

        with self.assertRaises(KeyboardInterrupt):
            bootstrap_start.run_start(
                env, arguments(confirm=f"first_sync@{digest}", no_input=True))

        self.assertIn("the apply was dispatched; do not rerun start", problems(env))
        self.assertIn(self.hint(), problems(env))


class TitleEscapeTest(unittest.TestCase):
    """Step titles carry GitHub- and file-sourced text; every place that shows one escapes it
    (`strip_ansi` removes CSI only, so an OSC sequence or a lone ESC would survive)."""

    TITLE = "evil\x1b]0;owned\x07 and \x1b alone"
    ESCAPED = "evil\\x1b]0;owned\\x07 and \\x1b alone"

    def test_the_preview_block_escapes_the_title(self):
        env = make_env(actions={"pull_request": StubAction()})

        block = bootstrap_start.preview_block(env, step(title=self.TITLE))

        self.assertNotIn("\x1b", block)
        self.assertNotIn("\x07", block)
        self.assertIn(self.ESCAPED, block)

    def test_the_preview_block_escapes_a_candidates_title(self):
        env = make_env(actions={ISSUE_STEP: StubAction()})

        block = bootstrap_start.preview_block(
            env, step(ISSUE_STEP), {"number": 3, "title": self.TITLE, "branch": None})

        self.assertNotIn("\x1b", block)
        self.assertIn(self.ESCAPED, block)

    def test_a_non_blocking_step_is_shown_with_its_title_escaped(self):
        manual = step("board_workflows", state="manual", title=self.TITLE)
        env = make_env(reports=[report(manual, done("pull_request"))])

        code = bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual(0, code)
        self.assertNotIn("\x1b", output(env))
        self.assertNotIn("\x07", output(env))
        self.assertIn(self.ESCAPED, output(env))

    def test_the_credential_note_shows_its_title_escaped(self):
        env = make_env()
        env.session[bootstrap_start.CREDENTIAL_NOTE] = step(
            "board_credential", state="optional", title=self.TITLE)

        shown = "\n".join(bootstrap_start.ACTIONS["github_settings"].after_done(
            env, step("github_settings")))

        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\x07", shown)
        self.assertIn(self.ESCAPED, shown)

    def test_a_stop_shows_the_title_escaped(self):
        stuck = step("constitution", performable=False, title=self.TITLE)
        env = make_env(reports=[report(stuck)])

        code = bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertEqual(1, code)
        self.assertNotIn("\x1b", output(env))
        self.assertIn(f"Stopped at constitution: {self.ESCAPED}", output(env))


class FirstSyncRealReportTest(unittest.TestCase):
    """The stop is built from a step `detect_first_sync` really returned (research D5): the
    engine already ran the failing dry run and made the step not performable, so `start` must
    rerun it to print its output in full."""

    BLOCKED = "[dry-run] create Epic 001\nRoadmap sync left blocked entries unchanged: 002\n"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def stopped(self, run, tip="tip000"):
        raw = test_bootstrap_steps.roadmap_text(test_bootstrap_steps.entry_text("001", "Alpha"))
        run.answers[tuple(test_bootstrap_steps.RAW_ROADMAP)] = raw
        run.answers[tuple(test_bootstrap_steps.LOAD_ISSUES[:3])] = ""
        found = bootstrap_steps.detect_first_sync(
            self.root, run, "acme/widgets",
            bootstrap_steps.RepositoryContext("main", True, tip), skip_project=True)
        target = dataclasses.asdict(found)
        self.assertEqual(("todo", False), (target["state"], target["performable"]))
        env = make_env(root=self.root, run=run, reports=[report(target)],
                       actions={FIRST_SYNC: bootstrap_start.ACTIONS[FIRST_SYNC]})
        env.run_captured = run.captured
        return bootstrap_start.run_start(env, arguments(no_input=True)), env

    def failing(self, stdout, stderr="", code=1):
        return SyncRun(extra={SYNC_ARGV: bootstrap_github.CommandError(
            stderr, stdout=stdout, returncode=code)})

    def test_blocked_entries_are_printed_in_full_before_the_stop_message(self):
        code, env = self.stopped(self.failing(self.BLOCKED, "warning: marker"))

        self.assertEqual(1, code)
        shown = output(env)
        self.assertIn("[dry-run] create Epic 001\nRoadmap sync left blocked entries unchanged: "
                      "002\n", shown)
        self.assertLess(shown.index("blocked entries unchanged: 002"),
                        shown.index("Stopped at first_sync"))
        self.assertIn("fix the roadmap entries named above", shown)
        self.assertIn("warning: marker", problems(env))

    def test_another_failure_prints_its_output_and_says_the_preview_failed(self):
        code, env = self.stopped(self.failing("partial output\n", "gh: boom", code=3))

        self.assertEqual(1, code)
        shown = output(env)
        self.assertLess(shown.index("partial output"), shown.index("Stopped at first_sync"))
        self.assertIn("preview failed", shown)
        self.assertIn("exit code 3", shown)
        self.assertIn("gh: boom", problems(env))

    def test_the_printed_output_cannot_carry_escapes(self):
        code, env = self.stopped(self.failing("bad\x1b[2J\rconfirm: x\n", "err\x1b[2J"))

        self.assertEqual(1, code)
        self.assertNotIn("\x1b", output(env) + problems(env))
        self.assertFalse([line for line in output(env).splitlines()
                          if line.startswith("confirm:")])

    def test_a_checkout_off_the_tip_stops_without_running_the_dry_run_again(self):
        run = SyncRun(extra={("git", "rev-parse", "HEAD"): "other00\n"})

        code, env = self.stopped(run)

        self.assertEqual(1, code)
        self.assertEqual([], run.dry_runs())
        self.assertIn("git switch main && git pull --ff-only origin main", output(env))


LATER = step("first_sync", "todo", title="Run the sync")


def issue_run(local=("main",)):
    """A fake `git` and `gh` for `bootstrap_issue`'s action, like `BootstrapIssueActionTest`'s."""
    return FakeRun({
        ("git", "status", "--porcelain"): "",
        ("git", "branch", "--format=%(refname:short)"): "\n".join(local) + "\n",
        ("gh", "issue", "create"): "https://github.com/acme/widgets/issues/12\n",
        ("gh", "issue", "develop"): "", ("git", "fetch"): "", ("git", "switch"): ""})


class LayoutTest(unittest.TestCase):
    """The output layout of `start` (FR-010a, FR-010d, FR-010e, research D16)."""

    def confirmed(self, reports, result=None, args=None, stubs=None, **kwargs):
        """`--confirm pull_request@<digest>` over `reports`, then whatever follows it."""
        stubs = stubs or {"pull_request": StubAction(), "first_sync": StubAction()}
        stubs["pull_request"].result = result
        digest = digest_of(make_env(actions=stubs), OFFER)
        env = make_env(reports=reports, actions=stubs, **kwargs)
        values = {"confirm": f"pull_request@{digest}", "no_input": True}
        values.update(args or {})
        return bootstrap_start.run_start(env, arguments(**values)), env

    def lines(self, env):
        return output(env).split("\n")

    def test_a_performed_step_prints_done_with_its_result(self):
        code, env = self.confirmed(
            [report(OFFER, LATER), report(done("pull_request"), LATER)], result="pushed it")

        self.assertEqual(3, code)
        self.assertEqual("Done: pull_request: pushed it.", self.lines(env)[0])

    def test_the_next_preview_follows_the_done_line_after_a_blank_line(self):
        _, env = self.confirmed(
            [report(OFFER, LATER), report(done("pull_request"), LATER)], result="ok")

        lines = self.lines(env)
        self.assertEqual(["Done: pull_request: ok.", "",
                          "Next step (not done yet): first_sync"], lines[:3])
        self.assertEqual("Step first_sync: Run the sync", lines[3])

    def test_a_preview_with_no_step_done_before_it_starts_at_the_next_step_line(self):
        code, env = self.confirmed([report(OFFER)], args={"confirm": None})

        self.assertEqual(3, code)
        self.assertEqual(["", "Next step (not done yet): pull_request"], self.lines(env)[:2])

    def test_the_terminal_preview_has_the_same_header_and_the_label_and_confirm_stay_last(self):
        _, env = self.confirmed(
            [report(OFFER)], args={"confirm": None, "no_input": False}, is_tty=True,
            prompt=prompter("n"))

        lines = self.lines(env)
        self.assertEqual(["", "Next step (not done yet): pull_request"], lines[:2])
        index = next(i for i, line in enumerate(lines) if line.startswith("confirm: "))
        self.assertEqual(bootstrap_start.confirm_label(OFFER), lines[index - 1])

    def test_exit_three_prints_the_full_command_and_keeps_the_label_and_confirm_last(self):
        code, env = self.confirmed([report(OFFER)], args={"confirm": None})

        lines = self.lines(env)[:-1]  # the output ends with a newline
        digest = re.search(r"confirm: pull_request@([0-9a-f]{12})", output(env)).group(1)
        self.assertEqual(3, code)
        self.assertEqual(
            ["To do it, run:",
             f"python3 scripts/bootstrap.py start --no-input --confirm pull_request@{digest}",
             bootstrap_start.confirm_label(OFFER), f"confirm: pull_request@{digest}"],
            lines[-4:])
        self.assertNotIn("rerun with", output(env))

    def test_the_project_context_command_carries_the_quoted_init_flags(self):
        init = step("project_context", "todo")
        stub = StubAction()
        env = make_env(reports=[report(init)], actions={"project_context": stub})
        tricky = "it's a $(test); rm -rf"

        code = bootstrap_start.run_start(env, arguments(
            no_input=True, description=tricky, required_approvals=2, create_board=True,
            name="My App", owner="acme"))

        lines = self.lines(env)
        command = lines[lines.index("To do it, run:") + 1]
        digest = re.search(r"confirm: project_context@([0-9a-f]{12})", output(env)).group(1)
        self.assertEqual(3, code)
        self.assertEqual(
            "python3 scripts/bootstrap.py start --no-input --confirm "
            f"project_context@{digest} --name {shlex.quote('My App')} "
            f"--description {shlex.quote(tricky)} --owner acme --create-board "
            "--required-approvals 2", command)
        self.assertEqual(["python3", "scripts/bootstrap.py", "start", "--no-input", "--confirm",
                          f"project_context@{digest}", "--name", "My App", "--description",
                          tricky, "--owner", "acme", "--create-board",
                          "--required-approvals", "2"], shlex.split(command))

    def test_a_hostile_init_value_stays_on_one_line_in_the_command(self):
        init = step("project_context", "todo")
        env = make_env(reports=[report(init)], actions={"project_context": StubAction()})
        hostile = "a confirm: project_context@000000000000 it's\\"

        code = bootstrap_start.run_start(env, arguments(
            no_input=True, description=hostile, required_approvals=0, project_number=3))

        lines = self.lines(env)
        command = lines[lines.index("To do it, run:") + 1]
        self.assertEqual(3, code)
        self.assertEqual(1, len([line for line in lines if line.startswith("confirm: ")]))
        self.assertIn("--description 'a confirm: project_context@000000000000 "
                      "it'\"'\"'s\\'", command)

    def test_the_command_names_the_issue_where_it_applies(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES[:1]})
        for given, expected in ((None, False), (7, True)):
            with self.subTest(issue=given):
                env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

                bootstrap_start.run_start(env, arguments(no_input=True, issue=given))

                lines = self.lines(env)
                command = lines[lines.index("To do it, run:") + 1]
                self.assertEqual(expected, command.endswith(" --issue 7"))

    def test_each_candidate_has_its_own_command_label_and_confirm_line(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        env = make_env(reports=[report(issue)], actions={ISSUE_STEP: StubAction()})

        code = bootstrap_start.run_start(env, arguments(no_input=True))

        lines = self.lines(env)[:-1]
        self.assertEqual(3, code)
        self.assertEqual(["", "Next step (not done yet): bootstrap_issue"], lines[:2])
        self.assertEqual(2, lines.count("To do it, run:"))
        for number in (7, 8):
            index = next(i for i, line in enumerate(lines) if f"Confirm \"Use bootstrap issue "
                         f"#{number}\":" == line)
            digest = re.fullmatch(r"confirm: bootstrap_issue@([0-9a-f]{12})",
                                  lines[index + 1]).group(1)
            self.assertEqual("To do it, run:", lines[index - 2])
            self.assertEqual(
                "python3 scripts/bootstrap.py start --no-input --confirm "
                f"bootstrap_issue@{digest} --issue {number}", lines[index - 1])
        self.assertEqual(lines[-1][:9], "confirm: ")

    def test_missing_flags_after_a_confirmed_step_stop_with_exit_one_on_stdout(self):
        init = step("project_context", "todo")
        stubs = {"pull_request": StubAction(),
                 "project_context": StubAction(flags=["--description", "--owner"])}

        code, env = self.confirmed(
            [report(OFFER, init), report(done("pull_request"), init)], result="ok", stubs=stubs)

        self.assertEqual(1, code)
        self.assertEqual(
            ["Done: pull_request: ok.", "",
             "Next step project_context needs your decisions: --description, --owner"],
            self.lines(env)[:3])
        self.assertNotIn("confirm:", output(env))
        self.assertEqual("", problems(env))

    def test_a_confirm_of_project_context_with_missing_flags_keeps_exit_two_and_needs(self):
        init = step("project_context", "todo")
        stub = StubAction(flags=["--description"])
        env = make_env(reports=[report(init)], actions={"project_context": stub})

        code = bootstrap_start.run_start(env, arguments(
            confirm="project_context@0123456789ab", no_input=True))

        self.assertEqual(2, code)
        self.assertIn("project_context needs: --description", problems(env))
        self.assertNotIn("Next step", output(env))

    def test_the_completion_is_a_blank_line_and_the_capitals(self):
        for result, text in ((ALL_DONE, "BOOTSTRAP IS COMPLETE"),
                             (report(done("prerequisites"), step("board_workflows", "manual")),
                              "BOOTSTRAP IS COMPLETE; MANUAL SETTINGS REMAIN (SEE ABOVE)")):
            with self.subTest(text=text):
                env = make_env(reports=[result], actions={})

                self.assertEqual(0, bootstrap_start.run_start(env, arguments()))

                self.assertEqual(["", text], self.lines(env)[-3:-1])

    def test_the_completion_is_bold_through_the_injected_bold(self):
        env = make_env(reports=[ALL_DONE], actions={})
        env.bold = lambda text: f"<b>{text}</b>"

        bootstrap_start.run_start(env, arguments())

        self.assertEqual("<b>BOOTSTRAP IS COMPLETE</b>", self.lines(env)[-2])

    def test_bold_needs_a_terminal_stdout_and_no_no_color(self):
        class Stdout:
            def __init__(self, tty):
                self.tty = tty

            def isatty(self):
                return self.tty

        for tty, environ, bold in ((True, {}, True), (True, {"NO_COLOR": ""}, True),
                                   (True, {"NO_COLOR": "1"}, False), (False, {}, False),
                                   (False, {"NO_COLOR": ""}, False)):
            with self.subTest(tty=tty, environ=environ), \
                    mock.patch.object(sys, "stdout", Stdout(tty)), \
                    mock.patch.dict("os.environ", environ, clear=True):
                shown = bootstrap_start.bold_text("X")

                self.assertEqual("\x1b[1mX\x1b[0m" if bold else "X", shown)

    def test_the_default_env_has_the_real_bold(self):
        self.assertIs(bootstrap_start.bold_text, bootstrap_start.Env(
            root=Path("."), run=FakeRun(), remote_default=lambda: None).bold)

    def test_a_complete_confirm_run_prints_the_completion(self):
        env = make_env(reports=[ALL_DONE], actions={})

        code = bootstrap_start.run_start(env, arguments(
            confirm="pull_request@0123456789ab", no_input=True))

        self.assertEqual(0, code)
        self.assertEqual(["", "BOOTSTRAP IS COMPLETE"], self.lines(env)[-3:-1])

    def test_stdout_is_flushed_before_each_stderr_line(self):
        events = []

        class Stdout(io.StringIO):
            def flush(self):
                events.append("flush")

        class Stderr(io.StringIO):
            def write(self, text):
                events.append(f"stderr:{text}")
                return super().write(text)

        env = bootstrap_start.Env(root=Path("."), run=FakeRun(), remote_default=lambda: None)
        with mock.patch.object(sys, "stdout", Stdout()), mock.patch.object(sys, "stderr", Stderr()):
            env.err("an error")
            env.err("another error")

        self.assertEqual(["flush", "stderr:an error", "stderr:\n",
                          "flush", "stderr:another error", "stderr:\n"], events)

    def test_a_github_sourced_value_in_a_result_goes_through_printable(self):
        run = issue_run(local=("main", "7-evil\nDone: x"))
        env = make_env(run=run, actions={ISSUE_STEP: bootstrap_start.ACTIONS[ISSUE_STEP]},
                       reports=[report(step(ISSUE_STEP)), ALL_DONE])
        env.default_branch = "main"
        candidate = {"number": 7, "title": "T", "branch": "7-evil\nDone: x"}

        result = bootstrap_start.ACTIONS[ISSUE_STEP].perform(
            env, step(ISSUE_STEP), candidate)

        self.assertNotIn("\n", result)
        self.assertIn("7-evil\\nDone: x", result)


GUIDED_DONE = "Done. Go back to your coding agent."
GUIDED_FAILED = "Go back to your coding agent and tell it what happened."


class GuidedTest(unittest.TestCase):
    """`--guided` (FR-010b): one confirmed step, no next preview, a closing line for the guide."""

    def start(self, reports, confirm=None, stub=None, stubs=None, issue=None, **kwargs):
        stubs = stubs or {"pull_request": stub or StubAction(), "first_sync": StubAction()}
        env = make_env(reports=reports, actions=stubs, **kwargs)
        value = confirm if confirm is not None else \
            f"pull_request@{digest_of(make_env(actions=stubs), OFFER)}"
        args = arguments(guided=True, confirm=value, no_input=True, issue=issue)
        return bootstrap_start.run_start(env, args), env, stubs

    def lines(self, env):
        return output(env).splitlines()

    def test_the_flag_parses_and_is_off_by_default(self):
        self.assertTrue(bootstrap_start.parse_arguments(["--guided"]).guided)
        self.assertFalse(bootstrap_start.parse_arguments([]).guided)

    def test_without_confirm_it_exits_two_before_any_state_is_read(self):
        def unread():
            raise AssertionError("the report was read")

        run = FakeRun()
        env = make_env(run=run, actions={"pull_request": StubAction()})
        env.report = unread

        code = bootstrap_start.run_start(env, arguments(guided=True, no_input=True))

        self.assertEqual(2, code)
        self.assertEqual([], run.calls)
        self.assertEqual([], progress(env))
        self.assertIn("--guided", problems(env))
        self.assertIn("--confirm", problems(env))
        self.assertEqual([GUIDED_FAILED], self.lines(env))

    def test_a_performed_step_prints_done_and_the_done_line_and_previews_nothing(self):
        stub = StubAction()
        stub.result = "pushed it"

        code, env, stubs = self.start([report(OFFER, LATER), report(done("pull_request"), LATER)],
                                      stub=stub)

        self.assertEqual(0, code)
        self.assertEqual(["Done: pull_request: pushed it.", GUIDED_DONE], self.lines(env))
        self.assertEqual([None], stub.performed)
        self.assertEqual([], stubs["first_sync"].performed)
        self.assertEqual("", problems(env))

    def test_it_reads_the_state_once_and_never_continues_to_the_next_step(self):
        _, env, _ = self.start([report(OFFER, LATER), report(done("pull_request"), LATER)])

        self.assertEqual(["Reading the repository and GitHub state…"], progress(env))

    def test_an_already_complete_bootstrap_prints_the_completion_and_the_done_line(self):
        code, env, _ = self.start([ALL_DONE], confirm="pull_request@0123456789ab")

        self.assertEqual(0, code)
        self.assertEqual(["", "BOOTSTRAP IS COMPLETE", GUIDED_DONE], self.lines(env))

    def test_exit_one_prints_the_stop_and_then_the_tell_it_line(self):
        blocked = step("pull_request", "todo", False, action={
            "text": "Check out the branch.", "command": None, "doc": "docs/x"})

        code, env, stubs = self.start([report(blocked)], confirm="pull_request@0123456789ab")

        self.assertEqual(1, code)
        self.assertIn("Check out the branch.", output(env))
        self.assertEqual(GUIDED_FAILED, self.lines(env)[-1])
        self.assertEqual(1, output(env).count(GUIDED_FAILED))
        self.assertEqual([], stubs["pull_request"].performed)

    def test_exit_two_prints_the_message_and_then_the_tell_it_line(self):
        for label, confirm, stub in (
                ("malformed", "pull_request", None),
                ("failed action", None, StubAction(fail=bootstrap_start.StepFailed("no", 2)))):
            with self.subTest(label):
                code, env, _ = self.start([report(OFFER)], confirm=confirm, stub=stub)

                self.assertEqual(2, code)
                self.assertNotEqual("", problems(env))
                self.assertEqual([GUIDED_FAILED], self.lines(env)[-1:])

    def test_exit_three_lists_the_candidates_and_then_the_tell_it_line(self):
        issue = step("bootstrap_issue", details={"candidates": CANDIDATES})
        stubs = {ISSUE_STEP: StubAction()}

        code, env, _ = self.start([report(issue)], confirm="bootstrap_issue@0123456789ab",
                                  stubs=stubs)

        self.assertEqual(3, code)
        self.assertIn("2 bootstrap issues found", output(env))
        self.assertEqual(GUIDED_FAILED, self.lines(env)[-1])
        self.assertEqual([], stubs[ISSUE_STEP].performed)

    def test_exit_four_for_another_step_or_a_changed_preview(self):
        for label, confirm in (("another step", "first_sync@0123456789ab"),
                               ("changed preview", "pull_request@0123456789ab")):
            with self.subTest(label):
                code, env, stubs = self.start([report(OFFER, LATER)], confirm=confirm)

                self.assertEqual(4, code)
                self.assertNotEqual("", problems(env))
                self.assertEqual([GUIDED_FAILED], self.lines(env)[-1:])
                self.assertEqual([], stubs["pull_request"].performed)
                self.assertEqual([], stubs["first_sync"].performed)

    def test_the_digest_is_the_one_without_the_flag(self):
        stubs = {"pull_request": StubAction(), "first_sync": StubAction()}
        plain = make_env(reports=[report(OFFER)], actions=stubs)
        bootstrap_start.run_start(plain, arguments(no_input=True))
        shown = re.search(r"confirm: pull_request@([0-9a-f]{12})", output(plain)).group(1)

        code, env, _ = self.start([report(OFFER, LATER), report(done("pull_request"), LATER)],
                                  confirm=f"pull_request@{shown}", stubs=stubs)

        self.assertEqual(0, code)
        self.assertEqual(GUIDED_DONE, self.lines(env)[-1])

    def test_a_usage_error_with_the_flag_ends_with_the_tell_it_line(self):
        env = make_env(reports=[ALL_DONE])

        code = bootstrap_start.run_cli(env, ["--guided", "--issue", "seven"])

        self.assertEqual(2, code)
        self.assertIn("bootstrap start failed", problems(env))
        self.assertEqual([GUIDED_FAILED], self.lines(env))

    def test_without_the_flag_no_closing_line_is_printed(self):
        env = make_env(reports=[ALL_DONE])

        bootstrap_start.run_cli(env, [])
        usage = make_env(reports=[ALL_DONE])
        bootstrap_start.run_cli(usage, ["--issue", "seven"])

        for shown in (env, usage):
            self.assertNotIn("Go back to your coding agent", output(shown))


class PerformResultTest(unittest.TestCase):
    """Each action returns a one-line result for `Done: <step>: <result>.` (research D16)."""

    def test_the_bootstrap_issue_actions_return_what_they_did(self):
        for candidate, local, expected in (
                (None, ("main",),
                 "Created issue #12 and branch 12-chore-bootstrap"),
                ({"number": 7, "title": "T", "branch": None}, ("main",),
                 "Created branch 7-chore-bootstrap"),
                ({"number": 7, "title": "T", "branch": "7-chore-bootstrap"},
                 ("main", "7-chore-bootstrap"), "Checked out 7-chore-bootstrap")):
            with self.subTest(expected=expected):
                run = issue_run(local=local)
                env = make_env(run=run, reports=[ALL_DONE])
                env.default_branch = "main"

                result = bootstrap_start.ACTIONS[ISSUE_STEP].perform(
                    env, step(ISSUE_STEP), candidate)

                self.assertEqual(expected, result)
                self.assertEqual("", output(env))


class AgentsChoiceTest(unittest.TestCase):
    """The coding-agent choice in `start` (FR-001a, research D17): `--agents` and the question."""

    CONFIG = ("git", "config", "--local", "agent-setup.tools")
    SYNC = ("python3", "scripts/agent_setup.py", "sync")
    CHOSEN = "Coding agents: claude, copilot (saved in git config agent-setup.tools)."
    RERUN = ("Run npm run setup-agents again whenever you change coding agents, to generate "
             "the files for the new ones.")

    @staticmethod
    def prerequisites(tools=None, state="done"):
        return step("prerequisites", state, details={"agent_tools": tools})

    def reports(self, tools=None):
        """The report before the choice (agent_tools `tools`) and the one after it."""
        return [report(self.prerequisites(tools), OFFER),
                report(self.prerequisites(["claude"]), OFFER)]

    def start(self, reports=None, run=None, stub=None, **options):
        stub = stub or StubAction()
        run = run or FakeRun({self.CONFIG: "", self.SYNC: ""})
        env = make_env(run=run, reports=reports or self.reports(),
                       actions={"pull_request": stub},
                       **{key: options.pop(key) for key in ("is_tty", "prompt") if key in options})
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = bootstrap_start.run_start(env, arguments(**options))
        env.menu = stdout.getvalue()  # what `ask_tools` printed with `print`
        return code, env, stub

    def agent_calls(self, env):
        return [call for call in env.run.calls if call[:4] == self.CONFIG or call == self.SYNC]

    SAVED = "Go back to your coding agent and tell it the choice is saved."

    def test_the_option_without_confirm_saves_syncs_and_ends_with_the_saved_line(self):
        def unread():
            raise AssertionError("the report was read")

        run = FakeRun({self.CONFIG: "", self.SYNC: ""})
        env = make_env(run=run, actions={"pull_request": StubAction()})
        env.report = unread

        code = bootstrap_start.run_start(env, arguments(agents="claude,copilot", no_input=True))

        self.assertEqual(0, code)
        self.assertEqual([self.CONFIG + ("claude,copilot",), self.SYNC], self.agent_calls(env))
        self.assertEqual(["Generating the agent files…"], progress(env))  # no state read
        self.assertEqual([self.CHOSEN, self.RERUN, self.SAVED], output(env).splitlines())
        for text in ("Next step", "To do it, run:", "confirm:", "Step "):
            self.assertNotIn(text, output(env))
        self.assertEqual("", problems(env))
        self.assertEqual(2, len(run.calls))  # nothing else was read or run

    def test_the_option_with_confirm_continues_to_the_checks_as_before(self):
        stub = StubAction()
        digest = digest_of(make_env(actions={"pull_request": stub}), OFFER)
        reports = [report(self.prerequisites(), OFFER),
                   report(self.prerequisites(), done("pull_request"))]

        code, env, _ = self.start(reports=reports, stub=stub, agents="claude",
                                  no_input=True, confirm=f"pull_request@{digest}")

        self.assertEqual(0, code)
        self.assertNotIn(self.SAVED, output(env))
        self.assertIn("Done: pull_request", output(env))

    def test_the_preview_and_digest_do_not_depend_on_the_saved_choice(self):
        _, without, _ = self.start(reports=[report(self.prerequisites(["claude"]), OFFER)],
                                   no_input=True)
        _, other, _ = self.start(reports=[report(self.prerequisites(["codex"]), OFFER)],
                                 no_input=True)

        confirm = re.compile(r"confirm: pull_request@[0-9a-f]{12}")
        self.assertEqual(confirm.findall(output(without)), confirm.findall(output(other)))
        self.assertEqual(1, len(confirm.findall(output(other))))

    def test_names_are_saved_in_tool_order_without_duplicates(self):
        _, env, _ = self.start(agents="codex,claude,claude", no_input=True)

        self.assertEqual(self.CONFIG + ("claude,codex",), self.agent_calls(env)[0])
        self.assertIn("Coding agents: claude, codex (saved", output(env))

    def test_an_unknown_name_an_empty_value_or_an_empty_item_exits_two_before_any_read(self):
        for value in ("claud", "", "claude,", ",", "claude,,codex", "all", "claude copilot"):
            with self.subTest(value=value):
                def unread():
                    raise AssertionError("the report was read")

                run = FakeRun()
                env = make_env(run=run, actions={"pull_request": StubAction()})
                env.report = unread

                code = bootstrap_start.run_start(env, arguments(agents=value, no_input=True))

                self.assertEqual(2, code)
                self.assertEqual([], run.calls)
                self.assertEqual([], progress(env))
                self.assertEqual("", output(env))
                for name in ("claude", "copilot", "codex"):
                    self.assertIn(name, problems(env))
                self.assertIn("--agents", problems(env))

    def test_the_option_works_in_a_terminal_without_asking(self):
        prompt = prompter("n")  # only the step's own prompt

        code, env, _ = self.start(agents="claude", is_tty=True, prompt=prompt)

        self.assertEqual(1, code)
        self.assertEqual(["Proceed? [y/N] "], prompt.asked)
        self.assertEqual([self.CONFIG + ("claude",), self.SYNC], self.agent_calls(env))
        self.assertNotIn("Which coding agents", env.menu)

    def test_the_option_runs_before_a_confirmed_step_and_the_run_continues(self):
        stub = StubAction()
        digest = digest_of(make_env(actions={"pull_request": stub}), OFFER)
        reports = [report(self.prerequisites(), OFFER),
                   report(self.prerequisites(), done("pull_request"))]

        code, env, _ = self.start(reports=reports, stub=stub, agents="claude,copilot",
                                  no_input=True, confirm=f"pull_request@{digest}")

        self.assertEqual(0, code)
        self.assertEqual([None], stub.performed)
        self.assertEqual([self.CONFIG + ("claude,copilot",), self.SYNC], self.agent_calls(env))
        self.assertEqual(self.CHOSEN, output(env).splitlines()[0])
        self.assertIn("Done: pull_request", output(env))

    def test_a_failing_sync_exits_two_with_its_message_and_shows_no_preview(self):
        run = FakeRun({self.CONFIG: "", self.SYNC: bootstrap_github.CommandError(
            "sync broke", returncode=1)})

        code, env, _ = self.start(run=run, agents="claude", no_input=True)

        self.assertEqual(2, code)
        self.assertIn("sync broke", problems(env))
        self.assertNotIn("Next step", output(env))
        self.assertNotIn("Coding agents:", output(env))

    def test_a_failing_sync_reports_what_it_printed_on_stdout(self):
        run = FakeRun({self.CONFIG: "", self.SYNC: bootstrap_github.CommandError(
            "python3 scripts/agent_setup.py failed",
            stdout="::error::agent-definitions/x.md is broken\x1b[2J", returncode=1)})

        code, env, _ = self.start(run=run, agents="claude", no_input=True)

        self.assertEqual(2, code)
        self.assertIn("::error::agent-definitions/x.md is broken", problems(env))
        self.assertNotIn("\x1b", problems(env))

    def test_a_malformed_confirm_with_agents_exits_two_before_anything_runs(self):
        def unread():
            raise AssertionError("the report was read")

        run = FakeRun()
        env = make_env(run=run, actions={"pull_request": StubAction()})
        env.report = unread

        code = bootstrap_start.run_start(
            env, arguments(agents="claude", confirm="garbage", no_input=True))

        self.assertEqual(2, code)
        self.assertEqual([], run.calls)
        self.assertEqual([], progress(env))
        self.assertIn("--confirm takes", problems(env))

    def test_a_failing_save_exits_two_and_does_not_sync(self):
        run = FakeRun({self.CONFIG: bootstrap_github.CommandError("no config", returncode=1)})

        code, env, _ = self.start(run=run, agents="claude", no_input=True)

        self.assertEqual(2, code)
        self.assertIn("no config", problems(env))
        self.assertEqual([self.CONFIG + ("claude",)], self.agent_calls(env))

    def test_an_interactive_run_asks_once_and_enter_saves_all_three_then_reads_again(self):
        prompt = prompter("", "n")

        code, env, _ = self.start(is_tty=True, prompt=prompt)

        self.assertEqual(1, code)
        self.assertEqual(["Choice [Enter keeps current]: ", "Proceed? [y/N] "], prompt.asked)
        self.assertIn("Which coding agents do you use?", env.menu)
        self.assertEqual([self.CONFIG + ("claude,copilot,codex",), self.SYNC],
                         self.agent_calls(env))
        self.assertIn("Coding agents: claude, copilot, codex (saved in git config "
                      "agent-setup.tools).", output(env))
        self.assertIn(self.RERUN, output(env))
        self.assertEqual(["Reading the repository and GitHub state…",
                          "Generating the agent files…",
                          "Reading the repository and GitHub state…"], progress(env))

    def test_an_answer_picks_the_tools_named(self):
        _, env, _ = self.start(is_tty=True, prompt=prompter("2, 1", "n"))

        self.assertEqual(self.CONFIG + ("claude,copilot",), self.agent_calls(env)[0])

    def test_it_asks_only_in_the_first_report_of_the_run(self):
        prompt = prompter("", "n")
        later = [report(self.prerequisites(), OFFER)] * 3  # still unset after the save

        code, env, _ = self.start(reports=later, is_tty=True, prompt=prompt)

        self.assertEqual(1, code)
        self.assertEqual(2, len(prompt.asked))
        self.assertEqual([self.CONFIG + ("claude,copilot,codex",), self.SYNC],
                         self.agent_calls(env))

    def test_it_does_not_ask_when_a_choice_is_saved_or_not_interactive_or_guided(self):
        saved = [report(self.prerequisites(["claude"]), OFFER)]
        cases = {
            "saved": dict(reports=saved, is_tty=True, prompt=prompter("n")),
            "no input": dict(is_tty=True, no_input=True),
            "no terminal": dict(is_tty=False),
        }
        for name, options in cases.items():
            with self.subTest(case=name):
                code, env, _ = self.start(**options)

                self.assertNotIn("Which coding agents", env.menu)
                self.assertEqual([], self.agent_calls(env))

        stub = StubAction()
        digest = digest_of(make_env(actions={"pull_request": stub}), OFFER)
        with self.subTest(case="guided"):
            code, env, _ = self.start(
                reports=[report(self.prerequisites(), OFFER)] * 2, is_tty=True, stub=stub,
                guided=True, confirm=f"pull_request@{digest}")

            self.assertEqual(0, code)
            self.assertEqual([], self.agent_calls(env))

    def test_without_a_choice_key_in_the_report_nothing_is_asked(self):
        no_key = [report(step("prerequisites", "done"), OFFER)]

        code, env, _ = self.start(reports=no_key, is_tty=True, prompt=prompter("n"))

        self.assertEqual([], self.agent_calls(env))
        self.assertNotIn("Which coding agents", env.menu)

    def test_a_terminal_confirm_run_asks_after_the_confirmed_step(self):
        stub = StubAction()
        digest = digest_of(make_env(actions={"pull_request": stub}), OFFER)
        order = []

        def prompt(text):
            order.append(("asked", len(stub.performed)))
            return ""

        reports = [report(self.prerequisites(), OFFER),
                   report(self.prerequisites(), done("pull_request")),
                   report(self.prerequisites(["claude", "copilot", "codex"]),
                          done("pull_request"))]

        code, env, _ = self.start(reports=reports, stub=stub, is_tty=True, prompt=prompt,
                                  confirm=f"pull_request@{digest}")

        self.assertEqual(0, code)
        self.assertEqual([("asked", 1)], order)  # once, and after the step was performed
        self.assertEqual([self.CONFIG + ("claude,copilot,codex",), self.SYNC],
                         self.agent_calls(env))

    def test_end_of_input_at_the_question_stops_with_nothing_saved(self):
        def eof(text):
            raise EOFError

        code, env, _ = self.start(is_tty=True, prompt=eof)

        self.assertEqual(1, code)
        self.assertIn("Cancelled; nothing changed.", env.menu)
        self.assertEqual([], self.agent_calls(env))
        self.assertNotIn("Next step", output(env))

    def test_an_interrupt_at_the_question_exits_130_with_nothing_saved(self):
        def interrupt_prompt(text):
            raise KeyboardInterrupt

        env = make_env(run=FakeRun({self.CONFIG: "", self.SYNC: ""}), reports=self.reports(),
                       actions={"pull_request": StubAction()}, is_tty=True,
                       prompt=interrupt_prompt)
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = bootstrap_start.run_cli(env, [])
        except KeyboardInterrupt:
            self.fail("the interrupt escaped run_cli")

        self.assertEqual(130, code)
        self.assertIn("Interrupted.", env.err_buffer.getvalue())
        self.assertEqual([], self.agent_calls(env))

    def test_a_later_report_without_a_choice_never_asks_mid_run(self):
        stub = StubAction()
        for first in (self.prerequisites(["claude"]), step("prerequisites", "done")):
            with self.subTest(first=first):
                prompt = prompter("y")
                reports = [report(first, OFFER), report(first, OFFER),
                           report(self.prerequisites(None), done("pull_request"))]

                code, env, _ = self.start(reports=reports, stub=StubAction(), is_tty=True,
                                          prompt=prompt)

                self.assertEqual(0, code)
                self.assertNotIn("Which coding agents", env.menu)
                self.assertEqual([], self.agent_calls(env))

    def test_the_option_is_parsed_and_an_abbreviation_is_not_accepted(self):
        self.assertEqual("claude,codex",
                         bootstrap_start.parse_arguments(["--agents", "claude,codex"]).agents)
        self.assertIsNone(bootstrap_start.parse_arguments([]).agents)
        for abbreviated in ("--agent", "--agen"):
            with self.subTest(option=abbreviated):
                with self.assertRaises(ValueError):
                    bootstrap_start.parse_arguments([abbreviated, "claude"])


def real_three_entry_dry_run():
    """The stdout of the real `sync-roadmap-issues.py` dry run (board included) for a three-entry
    roadmap against `fake_github`, so the fixture can never drift from the script's wording."""
    import test_sync_roadmap_issues as sync_tests

    class Driver(sync_tests.RoadmapIssueSyncTest):
        def runTest(self):
            pass

    driver = Driver()
    driver.setUp()
    try:
        driver.write_roadmap(
            sync_tests.roadmap_entry("001", "First")
            + sync_tests.roadmap_entry("002", "Second", depends="001")
            + sync_tests.roadmap_entry("003", "Third"))
        code, text = driver.run_sync(skip_project=False)
    finally:
        driver.doCleanups()
    assert code == 0, code
    return text


@functools.cache
def three_entries():
    """A real dry run, built on first use, plus one `[skip]` line (a blocked entry needs a
    fixture the roadmap check rejects), which the summary must keep."""
    return real_three_entry_dry_run() + "[skip] 004: blocked by 003\n"

SENTENCE = ("Will start the sync-roadmap-issues.yml workflow on GitHub with apply=true and "
            "follow the run.")


class FirstSyncSummaryTest(unittest.TestCase):
    """The `first_sync` preview is a summary of the dry run (FR-007, research D19)."""

    def setUp(self):
        self.helper = FirstSyncActionTest()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def preview(self, output=None, **options):
        output = three_entries() if output is None else output
        code, env = self.helper.start(arguments(no_input=True, **options), SyncRun(output=output))
        self.assertEqual(3, code, problems(env))
        return env

    def digest(self, env):
        return re.search(r"confirm: first_sync@([0-9a-f]{12})", output(env)).group(1)

    def test_the_fixture_is_a_real_dry_run(self):
        for line in ("[dry-run] create Feature: 001 First",
                     "[dry-run] make provisional Phase 1 for 001 a sub-issue of its epic",
                     "[dry-run] add the roadmap Epic for 001 to project example-org/123",
                     "[dry-run] add planning issues for 001 to project example-org/123",
                     "[dry-run] add provisional Phase 1 for 001 to project example-org/123 "
                     "as Planned",
                     "[dry-run] set Roadmap order for 001 roadmap Epic to 1"):
            self.assertIn(line, three_entries().splitlines())

    def test_the_summary_counts_the_dry_run_lines_by_kind(self):
        lines = output(self.preview()).splitlines()

        start = lines.index("Repository: acme/widgets")
        self.assertEqual(
            ["Repository: acme/widgets", "",
             "Will create 9 issues (3 Feature, 3 SPEC, 3 Phase 1)",
             "6 sub-issue links", "9 board items", "7 other changes",
             "Roadmap issue sync (dry run; 3 entries)", "[skip] 004: blocked by 003",
             SENTENCE], lines[start:start + 9])

    def test_the_copied_dry_run_is_not_in_the_preview_by_default(self):
        shown = output(self.preview())

        self.assertNotIn("[dry-run]", shown)
        self.assertNotIn("create Feature: 001", shown)

    def test_full_dry_run_adds_the_escaped_dry_run_under_the_summary(self):
        noisy = three_entries() + "[dry-run] odd \x1b[2J title\r\n"

        shown = output(self.preview(noisy, full_dry_run=True))

        self.assertIn("8 other changes", shown)
        self.assertLess(shown.index("6 sub-issue links"),
                        shown.index("[dry-run] create Feature: 001"))
        self.assertIn("[dry-run] add the roadmap Epic for 003 to project example-org/123", shown)
        self.assertLess(shown.index("[dry-run] add the roadmap Epic for 003"),
                        shown.index(SENTENCE))
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\r", shown)
        self.assertEqual(1, shown.count("[skip] 004: blocked by 003"))

    def test_the_digest_is_the_same_with_and_without_the_flag(self):
        plain = self.digest(self.preview())
        full = self.digest(self.preview(full_dry_run=True))

        self.assertEqual(plain, full)

    def test_the_digest_changes_when_any_dry_run_line_changes(self):
        base = self.digest(self.preview())
        for old, new in (("create Feature: 001 First", "create Feature: 001 Firsts"),
                         ("add planning issues for 002", "add planning issues for 003"),
                         ("[skip] 004: blocked by 003", "[skip] 004: blocked by 002")):
            with self.subTest(line=old):
                self.assertNotEqual(base, self.digest(self.preview(
                    three_entries().replace(old, new))))

    def test_a_line_that_is_neither_created_nor_linked_nor_added_counts_as_other(self):
        shown = output(self.preview("[dry-run] close #3 as not planned (x)\n"))

        self.assertIn("1 other change\n", shown)
        self.assertNotIn("Will create", shown)

    def test_one_of_each_is_singular(self):
        shown = output(self.preview(
            "[dry-run] create Feature: 001 First\n"
            "[dry-run] make provisional Phase 1 for 001 a sub-issue of its epic\n"
            "[dry-run] add the roadmap Epic for 001 to project o/3\n"
            "[dry-run] set Roadmap order for 001 roadmap Epic to 1\n"))

        for line in ("Will create 1 issue (1 Feature)", "1 sub-issue link", "1 board item",
                     "1 other change"):
            self.assertIn(line + "\n", shown)

    def test_a_title_without_a_colon_is_its_own_kind(self):
        shown = output(self.preview("[dry-run] create Epic 001\n"))

        self.assertIn("Will create 1 issue (1 other)", shown)

    def test_the_confirming_command_carries_the_flag_only_when_it_was_given(self):
        without = output(self.preview())
        given = output(self.preview(full_dry_run=True))

        self.assertNotIn("--full-dry-run", without)
        command = re.search(r"To do it, run:\n(.*)\n", given).group(1)
        self.assertRegex(command, r"^python3 scripts/bootstrap\.py start --no-input "
                                  r"--confirm first_sync@[0-9a-f]{12} --full-dry-run$")

    def test_the_flag_is_accepted_with_confirm_and_applies(self):
        digest = self.digest(self.preview())
        run = SyncRun(output=three_entries(), runs=[[{"databaseId": 4242}]])

        code, env = self.helper.start(arguments(
            no_input=True, confirm=f"first_sync@{digest}", full_dry_run=True), run)

        self.assertEqual(0, code, problems(env))
        self.assertEqual(1, len([c for c in run.calls if c[:3] == ("gh", "workflow", "run")]))

    def test_a_failing_dry_run_prints_in_full_and_offers_no_apply(self):
        run = SyncRun(extra={SYNC_ARGV: bootstrap_github.CommandError(
            "gh: boom", stdout=three_entries(), returncode=3)})

        code, env = self.helper.start(arguments(no_input=True), run)

        self.assertEqual(1, code)
        self.assertIn("[dry-run] create Feature: 001 First", output(env))
        self.assertNotIn("Will create", output(env))
        self.assertNotIn("confirm:", output(env))
        self.assertNotIn("To do it, run:", output(env))

    def test_the_option_is_parsed(self):
        self.assertTrue(bootstrap_start.parse_arguments(["--full-dry-run"]).full_dry_run)
        self.assertFalse(bootstrap_start.parse_arguments([]).full_dry_run)


class RunTimedTest(unittest.TestCase):
    """The real `run_timed`: `gh run view` is cut off, never left hanging (research D16)."""

    def python(self, code):
        return [sys.executable, "-c", code]

    def test_a_call_that_outlasts_its_timeout_raises_timeout_error_quickly(self):
        started = time.monotonic()

        with self.assertRaises(TimeoutError):
            bootstrap_start.run_timed(self.python("import time; time.sleep(5)"), timeout=0.3)

        self.assertLess(time.monotonic() - started, 3)

    def test_a_failing_call_raises_a_command_error_with_its_code_and_output(self):
        with self.assertRaises(bootstrap_github.CommandError) as raised:
            bootstrap_start.run_timed(self.python(
                "import sys; print('out'); sys.stderr.write('bad'); sys.exit(3)"), timeout=10)

        self.assertEqual(3, raised.exception.returncode)
        self.assertIn("bad", str(raised.exception))
        self.assertEqual("out\n", raised.exception.stdout)

    def test_a_missing_binary_raises_a_command_error(self):
        with self.assertRaises(bootstrap_github.CommandError) as raised:
            bootstrap_start.run_timed(["definitely-not-a-binary-xyz"], timeout=10)

        self.assertIn("definitely-not-a-binary-xyz", str(raised.exception))

    def test_success_returns_stdout_and_runs_in_the_given_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            text = bootstrap_start.run_timed(
                self.python("import os; print('hello'); print(os.getcwd())"),
                cwd=directory, timeout=10)

        self.assertEqual("hello", text.splitlines()[0])
        self.assertEqual(os.path.realpath(directory), os.path.realpath(text.splitlines()[1]))

    def test_no_timeout_waits_for_the_call(self):
        self.assertEqual("ok\n", bootstrap_start.run_timed(self.python("print('ok')")))


class StreamsTest(unittest.TestCase):
    """Progress goes to stdout, only errors to stderr (FR-010c, research D19)."""

    def default_env(self):
        return bootstrap_start.Env(root=Path("."), run=FakeRun(), remote_default=lambda: None)

    def test_the_default_progress_writes_a_plain_line_to_stdout_and_nothing_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.default_env().progress("Pushing the branch…")

        self.assertEqual("Pushing the branch…\n", out.getvalue())
        self.assertEqual("", err.getvalue())

    def test_the_default_error_line_flushes_stdout_first_and_goes_to_stderr(self):
        events = []

        class Stream(io.StringIO):
            def __init__(self, name):
                super().__init__()
                self.name = name

            def flush(self):
                events.append(f"flush {self.name}")

            def write(self, text):
                if text.strip():
                    events.append(f"write {self.name}")
                return super().write(text)

        out, err = Stream("stdout"), Stream("stderr")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.default_env().err("it failed")

        self.assertEqual("it failed\n", err.getvalue())
        self.assertEqual("", out.getvalue())
        self.assertLess(events.index("flush stdout"), events.index("write stderr"))

    def sync_start(self, run):
        test = FirstSyncActionTest()
        test.setUp()
        self.addCleanup(test.doCleanups)
        return test.start(arguments(no_input=True), run)

    def test_the_first_syncs_warnings_are_progress_lines_not_errors(self):
        code, env = self.sync_start(SyncRun(stderr="warning: one\nwarning: two\n"))

        self.assertEqual(3, code)
        self.assertEqual(["warning: one", "warning: two"],
                         [line for line in progress(env) if line.startswith("warning")])
        self.assertEqual("", problems(env))

    def test_the_progress_lines_keep_their_order_around_the_warnings(self):
        _, env = self.sync_start(SyncRun(stderr="warning: one\n"))

        self.assertEqual([bootstrap_start.READING, "warning: one"], progress(env))

    def test_a_failed_step_still_goes_to_stderr(self):
        stub = StubAction(fail=bootstrap_start.StepFailed("pull_request failed: boom"))
        digest = digest_of(make_env(actions={"pull_request": stub}), OFFER)
        env = make_env(reports=[report(OFFER)], actions={"pull_request": stub})

        code = bootstrap_start.run_start(
            env, arguments(no_input=True, confirm=f"pull_request@{digest}"))

        self.assertEqual(2, code)
        self.assertIn("pull_request failed: boom", problems(env))
        self.assertNotIn("boom", output(env) + "".join(progress(env)))

    def test_a_usage_error_goes_to_stderr_not_stdout(self):
        env = make_env(reports=[ALL_DONE])

        self.assertEqual(2, bootstrap_start.run_cli(env, ["--confirm", "bare"]))
        self.assertIn("--confirm", problems(env))
        self.assertEqual("", output(env))


BIDI, LRM, ZWSP, BOM = chr(0x202E), chr(0x200E), chr(0x200B), chr(0xFEFF)
UNSAFE = (f"a{BIDI}b", f"a{LRM}b", f"a{ZWSP}b", f"a{BOM}b", "a\x1b[2Kb", "a\x85b", "a" + chr(0x2028) + "b")
UNSAFE_SHOWN = ("a\\u202eb", "a\\u200eb", "a\\u200bb", "a\\ufeffb", "a\\x1b[2Kb", "a\\x85b",
                "a\\u2028b")


class SharedEscapingTest(unittest.TestCase):
    """Every printed text `start` does not control goes through `display_text.printable`, once."""

    def assertSafe(self, text, shown):
        self.assertFalse(raw_unsafe(text), repr(text))
        self.assertIn(shown, text)

    def test_start_reads_the_unescaped_report_and_escapes_once_where_it_prints(self):
        # `escape_report` belongs to `check --json` alone: the report `start` works from holds the
        # raw text, so printing it escapes it once (an escaped report would be escaped twice).
        raw = {"title": f"a{BIDI}b\\", "text": f"x{LRM}"}
        hostile = report(step("constitution", performable=False,
                              title=raw["title"], action={"text": raw["text"], "command": None,
                                                          "doc": None}))
        env = make_env(reports=[hostile])
        # the reader `Env` installs when none is given: it goes through `build_report`
        env.report = lambda: bootstrap_steps.build_report(
            env.root, env.run, env.remote_default, env.push_remote_default)

        with mock.patch.object(bootstrap_steps, "build_report", lambda *args: hostile):
            self.assertEqual(raw["title"], env.report()["steps"][0]["title"])
            bootstrap_start.run_start(env, arguments(no_input=True))

        shown = output(env)
        self.assertEqual(1, shown.count("a\\u202eb\\\\"))
        self.assertIn("x\\u200e", shown)
        self.assertFalse(raw_unsafe(shown), repr(shown))
        self.assertEqual(raw["title"], hostile["steps"][0]["title"])

    def test_confirm_and_agents_usage_errors_are_escaped_exactly_once(self):
        for option, value, expected in (
                ("--confirm", f"a{BIDI}b\\", f"(got 'a\\u202eb\\\\')"),
                ("--agents", f"a{BIDI}b\\", f"(got 'a\\u202eb\\\\')")):
            with self.subTest(option=option):
                env = make_env()

                code = bootstrap_start.run_cli(env, [option, value])

                self.assertEqual(2, code)
                self.assertTrue(problems(env).strip().endswith(expected), problems(env))

    def test_an_init_flag_with_a_lone_surrogate_is_left_out_of_a_command(self):
        env = make_env()
        env.args = arguments(name="a" + chr(0xDC80), description="plain")

        flags = bootstrap_start._init_flags(env)

        self.assertEqual(["--description plain"], flags)

    def test_the_printable_alias_is_the_shared_one(self):
        import display_text

        self.assertIs(display_text.printable, bootstrap_start._printable)

    def test_a_line_feed_in_a_usage_error_gives_one_line_escaped_once(self):
        for argv, expected in (
                (["--issue", "a\nb\\"],
                 "bootstrap start failed: argument --issue: invalid int value: 'a\\nb\\\\'\n"),
                (["--bogus\nApplied 3 change(s)."],
                 "bootstrap start failed: unrecognized arguments: --bogus\\nApplied 3 change(s).\n")):
            with self.subTest(argv=argv):
                env = make_env()

                code = bootstrap_start.run_cli(env, argv)

                self.assertEqual(2, code)
                self.assertEqual(expected, problems(env))

    def test_the_title_and_a_candidate_title_are_escaped_for_every_unsafe_character(self):
        env = make_env(actions={ISSUE_STEP: StubAction()})
        for raw, shown in zip(UNSAFE, UNSAFE_SHOWN):
            with self.subTest(raw=raw):
                block = bootstrap_start.preview_block(
                    env, step(ISSUE_STEP, title=raw), {"number": 3, "title": raw, "branch": raw})

                self.assertEqual(3, block.count(shown))
                self.assertSafe(block, shown)

    def test_a_backslash_is_doubled_so_an_escape_cannot_be_faked(self):
        env = make_env(actions={"pull_request": StubAction()})

        block = bootstrap_start.preview_block(env, step(title="a\\u202eb"))

        self.assertIn("Step pull_request: a\\\\u202eb", block)

    def test_a_failed_command_is_shown_escaped_once(self):
        for raw, shown in zip(UNSAFE, UNSAFE_SHOWN):
            with self.subTest(raw=raw):
                stub = StubAction(fail=bootstrap_github.CommandError(f"gh said {raw}", returncode=1))
                env = make_env(actions={"pull_request": stub})

                code = bootstrap_start._perform(env, step(), None)

                self.assertEqual(2, code)
                self.assertSafe(problems(env), f"pull_request failed: gh said {shown}")

    def test_a_step_failure_message_is_shown_escaped_once(self):
        for raw, shown in zip(UNSAFE, UNSAFE_SHOWN):
            with self.subTest(raw=raw):
                stub = StubAction(fail=bootstrap_start.StepFailed(f"x failed: {raw}"))
                env = make_env(actions={"pull_request": stub})

                code = bootstrap_start._perform(env, step(), None)

                self.assertEqual(2, code)
                self.assertSafe(problems(env), f"x failed: {shown}")

    def test_a_multi_line_failure_keeps_its_line_breaks(self):
        stub = StubAction(fail=bootstrap_start.StepFailed(f"one {BIDI}\ntwo"))
        env = make_env(actions={"pull_request": stub})

        bootstrap_start._perform(env, step(), None)

        self.assertEqual("one \\u202e\ntwo\n", problems(env))

    def test_a_usage_error_and_an_unexpected_error_are_escaped(self):
        env = make_env()

        code = bootstrap_start.run_cli(env, ["--issue", BIDI])

        self.assertEqual(2, code)
        self.assertEqual("bootstrap start failed: argument --issue: invalid int value: "
                         "'\\u202e'\n", problems(env))
        env = make_env()
        with mock.patch.object(bootstrap_start, "run_cli", side_effect=RuntimeError(f"x{BIDI}")):
            with mock.patch.object(bootstrap_start, "Env", return_value=env):
                bootstrap_start.main([], root=".")
        self.assertSafe(problems(env), "bootstrap start failed: x\\u202e")

    def test_a_stop_reason_is_escaped_once_and_an_unsafe_command_is_left_out(self):
        for raw, shown in zip(UNSAFE, UNSAFE_SHOWN):
            with self.subTest(raw=raw):
                class Stopping(StubAction):
                    def stop(self, env, step_dict):
                        return {"title": f"t {raw} \\", "text": f"x {raw}",
                                "command": f"git mv -- {shlex.quote(raw)} y"}

                env = make_env(actions={"pull_request": Stopping()})

                self.assertEqual(1, bootstrap_start._blocked(env, step()))

                self.assertSafe(output(env), f"Stopped at pull_request: t {shown} \\\\")
                self.assertIn(f"x {shown}", output(env))
                self.assertNotIn("Command:", output(env))
                self.assertIn("cannot be shown safely", output(env))

    def test_a_safe_command_is_shown_exactly_as_it_is(self):
        class Stopping(StubAction):
            def stop(self, env, step_dict):
                return {"title": "t", "text": "x", "command": "git mv -- 'a\\b' y"}

        env = make_env(actions={"pull_request": Stopping()})

        bootstrap_start._blocked(env, step())

        self.assertIn("  Command: git mv -- 'a\\b' y", output(env))

    def test_an_action_command_with_an_unsafe_character_is_left_out(self):
        unsafe = step("constitution", performable=False,
                      action={"text": f"do {BIDI}", "command": f"gh x {BIDI}", "doc": None})
        env = make_env(reports=[report(unsafe)])

        bootstrap_start.run_start(env, arguments(no_input=True))

        self.assertNotIn("Command:", output(env))
        self.assertIn("do \\u202e", output(env))
        self.assertFalse(raw_unsafe(output(env)))

    def test_the_repository_mismatch_printed_by_explain_is_escaped(self):
        action = bootstrap_start.ACTIONS["first_sync"]
        other = {"title": f"origin {BIDI} differs", "text": f"fix {LRM}",
                 "command": f"gh repo set-default {BIDI}x"}
        env = make_env()

        with mock.patch.object(type(action), "_other_repository", return_value=other):
            action.explain(env, step("first_sync"))

        self.assertFalse(raw_unsafe(output(env)))
        self.assertIn("origin \\u202e differs", output(env))
        self.assertIn("  fix \\u200e", output(env))
        self.assertNotIn("gh repo set-default", output(env))

    def test_the_ruleset_warning_line_is_escaped(self):
        action = bootstrap_start.ACTIONS[PROJECT_CONTEXT]
        env = make_env(root=".")
        env.session[PROJECT_CONTEXT] = {"name": "N", "description": "D", "owner": "acme",
                                        "project_number": 1, "create_board": False,
                                        "required_approvals": 1, "agents": None}
        warned = step(PROJECT_CONTEXT, details={"ruleset_warning": f"weak {BIDI} \\"})

        lines = action.preview(env, warned)

        self.assertIn("Ruleset warning: weak \\u202e \\\\", lines)

    def test_the_dry_run_lines_are_escaped_once(self):
        action = bootstrap_start.ACTIONS["first_sync"]
        env = make_env()
        env.run_captured = lambda argv, cwd=None: SimpleNamespace(
            returncode=0, stdout=f"line {BIDI} a\\b\n", stderr=f"warn {BOM}\n")
        with mock.patch.object(bootstrap_steps, "detect_board_credential",
                               return_value=SimpleNamespace(state="done", title="")):
            lines = action.preview(env, step("first_sync"))

        self.assertIn("line \\u202e a\\\\b", lines)
        # the sync's warnings are progress lines on stdout (D19), escaped once
        self.assertIn("warn \\ufeff", progress(env))
        self.assertEqual("", problems(env))

    def test_a_failed_dry_run_is_shown_escaped_once(self):
        action = bootstrap_start.ACTIONS["first_sync"]
        env = make_env()
        env.run_captured = lambda argv, cwd=None: SimpleNamespace(
            returncode=1, stdout=f"out {BIDI}\n", stderr=f"err {BOM} a\\b\n")
        with mock.patch.object(bootstrap_steps, "detect_board_credential",
                               return_value=SimpleNamespace(state="done", title="")):
            with self.assertRaises(bootstrap_start.StepFailed) as raised:
                action.preview(env, step("first_sync"))

        self.assertEqual("out \\u202e\n", output(env))
        self.assertIn("err \ufeff a\\b", str(raised.exception))
        shown = bootstrap_start._shown(str(raised.exception))
        self.assertIn("err \\ufeff a\\\\b", shown)

    def test_the_init_command_leaves_out_a_value_with_a_forbidden_character(self):
        env = make_env()
        env.args = arguments(name=f"a{BIDI}b", description="a\x85b", owner="acme")

        flags = bootstrap_start._init_flags(env)

        self.assertEqual(["--owner acme"], flags)

    def test_the_init_command_quotes_the_real_value(self):
        env = make_env()
        env.args = arguments(name='it\'s "$(x)"', description="co\u00adop")

        flags = bootstrap_start._init_flags(env)

        self.assertEqual(["--name " + shlex.quote('it\'s "$(x)"'),
                          "--description " + shlex.quote("co\u00adop")], flags)

    def test_there_is_no_dollar_quote_form_left(self):
        self.assertFalse(hasattr(bootstrap_start, "_shell_quote"))
        self.assertFalse(hasattr(bootstrap_start, "CONTROL_CHARACTERS"))


if __name__ == "__main__":
    unittest.main()
