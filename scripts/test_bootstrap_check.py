import contextlib
import io
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import bootstrap_check  # noqa: E402
from test_bootstrap_github import CONTEXT, FakeGitHub  # noqa: E402

ROADMAP = (
    "## Planned Specs\n\n### 900 — Example  [status: planned]\n"
    "- **Description:** A test entry\n- **Outcome:** Done\n"
    "- **Scope (in):** Everything\n- **Depends on:** none\n"
)


class CheckGitHub(FakeGitHub):
    """FakeGitHub plus `gh auth status`, and switches for a missing or logged-out gh."""

    def __init__(self, *args, gh="ok", **kwargs):
        super().__init__(*args, **kwargs)
        self.gh = gh

    def __call__(self, argv, cwd=None):
        if argv[0] == "gh" and self.gh == "missing":
            self.calls.append(list(argv))
            raise bootstrap.CommandError("cannot run gh: No such file or directory")
        if argv[:3] == ["gh", "auth", "status"]:
            assert argv[3:] == ["--active", "--hostname", "github.com"], argv
            self.calls.append(list(argv))
            if self.gh == "old":
                raise bootstrap.CommandError("unknown flag: --active\nUsage: gh auth status")
            if self.gh == "logged-out":
                raise bootstrap.CommandError("You are not logged into any GitHub hosts.")
            return "Logged in\n"
        if argv[:3] == ["git", "remote", "get-url"]:
            self.calls.append(list(argv))
            raise bootstrap.CommandError("No such remote 'origin'")
        return super().__call__(argv, cwd)


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
                  for p in root.rglob("*"))


class CheckTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / ".claude" / "agents").mkdir(parents=True)
        (self.root / ".claude" / "agents" / "a.md").write_text("x")
        files = ({Path(".claude/agents/a.md"): "x"}, [])
        for target, value in (("find_problems", []), ("expected_agent_files", files)):
            patcher = mock.patch.object(bootstrap_check.agent_setup, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, approvals=1, constitution=True, roadmap=True):
        (self.root / "project-context.toml").write_text(CONTEXT.format(approvals=approvals))
        if constitution:
            (self.root / ".specify" / "memory").mkdir(parents=True)
            (self.root / ".specify" / "memory" / "constitution.md").write_text("# C\n")
        if roadmap:
            (self.root / "ROADMAP.md").write_text(ROADMAP)

    def check(self, run=None, *args, command=("check",)):
        run = run or CheckGitHub()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main([*command, *args], root=self.root, run=run, is_tty=False)
        self.out = out.getvalue()
        self.run = run
        return code

    def configured_github(self, secrets=("PROJECT_BOARD_TOKEN",)):
        run = CheckGitHub()
        run.secrets = list(secrets)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                bootstrap.main(["github", "--apply"], root=self.root, run=run, is_tty=False), 0)
        run.writes.clear()
        run.calls.clear()
        return run

    def line(self, text):
        return next(line for line in self.out.splitlines() if text in line)


class StateTest(CheckTestCase):
    def test_fresh_repository_fails_and_names_the_first_actions(self):
        code = self.check()
        self.assertEqual(code, 1)
        self.assertIn("✗", self.line("project-context.toml"))
        self.assertIn("✗", self.line("onstitution"))
        self.assertIn("✗", self.line("ROADMAP.md"))
        for action in ("bootstrap.py init", "speckit-constitution", "roadmap-write"):
            self.assertIn(action, self.out)
        self.assertIn("–", self.line("GitHub settings"))  # needs project-context.toml first
        self.assertEqual(self.run.calls, [])

    def test_partial_setup_reports_each_step_and_fails(self):
        self.write(approvals=2, roadmap=False)
        code = self.check(self.configured_github())
        self.assertEqual(code, 1)
        self.assertIn("✓", self.line("project-context.toml"))
        self.assertIn("✓", self.line("onstitution"))
        self.assertIn("✗", self.line("ROADMAP.md"))
        self.assertIn("roadmap-write", self.out)
        self.assertNotIn("Start your first feature", self.out)

    def test_missing_constitution_asks_for_the_approval_count(self):
        self.write(approvals=3, constitution=False)
        self.check(self.configured_github())
        self.assertIn("3", self.out[self.out.index("speckit-constitution"):].splitlines()[0])

    def test_invalid_context_is_a_failure_with_the_reason(self):
        (self.root / "project-context.toml").write_text("[project]\nname = 1\n")
        self.assertEqual(self.check(), 1)
        self.assertIn("✗", self.line("project-context.toml"))
        self.assertIn("bootstrap.py init", self.out)

    def test_invalid_roadmap_is_a_failure(self):
        self.write()
        (self.root / "ROADMAP.md").write_text("### 900 — Broken  [status: planned]\n")
        self.assertEqual(self.check(self.configured_github()), 1)
        self.assertIn("✗", self.line("ROADMAP.md"))

    def test_everything_done_passes_and_points_to_the_first_feature(self):
        self.write()
        code = self.check(self.configured_github())
        self.assertEqual(code, 0, self.out)
        self.assertNotIn("✗", self.out)
        self.assertIn("✓", self.line("GitHub settings"))
        self.assertIn("✓", self.line("oard credential"))
        self.assertIn("If you have not done so yet:", self.out)
        self.assertIn("Start your first feature", self.out)
        self.assertIn("speckit-specify", self.out)
        self.assertIn("docs/bootstrap.md#your-first-feature", self.out)
        for text in ("one bootstrap pull request", "gh workflow run sync-roadmap-issues.yml -f apply=false",
                     "gh workflow run sync-roadmap-issues.yml -f apply=true"):
            self.assertIn(text, self.out)

    def test_no_subcommand_runs_check(self):
        self.write()
        self.assertEqual(self.check(self.configured_github(), command=()), 0)
        self.assertIn("Start your first feature", self.out)


class GitHubStepTest(CheckTestCase):
    def test_drift_fails_and_points_to_the_preview(self):
        self.write()
        self.assertEqual(self.check(CheckGitHub()), 1)
        self.assertIn("✗", self.line("GitHub settings"))
        self.assertIn("bootstrap.py github", self.out)

    def test_weak_fork_pr_approval_is_drift(self):
        self.write()
        github = self.configured_github()
        github.fork_approval = "first_time_contributors"

        self.assertEqual(self.check(github), 1)
        self.assertIn("✗", self.line("GitHub settings"))
        self.assertIn("(1 change(s))", self.line("GitHub settings"))

    def test_gh_missing_skips_with_a_note(self):
        self.write()
        self.assertEqual(self.check(CheckGitHub(gh="missing")), 0)
        self.assertIn("–", self.line("GitHub settings"))
        self.assertIn("gh", self.line("GitHub settings"))

    def test_gh_not_logged_in_skips_with_a_note(self):
        self.write()
        self.assertEqual(self.check(CheckGitHub(gh="logged-out")), 0)
        self.assertIn("gh auth login", self.out)
        self.assertIn("–", self.line("GitHub settings"))

    def test_an_old_gh_asks_for_an_upgrade_not_a_login(self):
        self.write()
        self.assertEqual(self.check(CheckGitHub(gh="old")), 0)
        self.assertIn("2.40", self.out)
        self.assertNotIn("not logged in", self.line("GitHub settings"))

    def test_a_github_error_is_a_failure_not_a_crash(self):
        self.write()
        self.assertEqual(self.check(CheckGitHub(board=False)), 1)
        self.assertIn("✗", self.line("GitHub settings"))
        self.assertIn("gh project create", self.out)


    def test_refused_weakening_counts_as_drift_and_is_not_a_credential_note(self):
        self.write(approvals=2)
        run = self.configured_github()
        self.write(approvals=1, constitution=False, roadmap=False)
        self.assertEqual(self.check(run), 1)
        self.assertIn("(1 change(s))", self.line("GitHub settings"))
        self.assertIn("✗", self.line("GitHub settings"))
        self.assertNotIn("--allow-weaken", self.line("oard credential"))


class CredentialStepTest(CheckTestCase):
    def test_missing_credential_is_optional_and_does_not_fail(self):
        self.write()
        self.assertEqual(self.check(self.configured_github(secrets=())), 0)
        line = self.line("oard credential")
        self.assertIn("–", line)
        self.assertNotIn("✗", line)
        self.assertIn("docs/bootstrap.md#board-credentials", self.out)

    def test_partial_app_credential_is_warned_about(self):
        self.write()
        self.assertEqual(self.check(self.configured_github(secrets=("PROJECT_STATUS_APP_ID",))), 0)
        self.assertIn("PROJECT_BOARD_APP_PRIVATE_KEY", self.out)

    def test_app_credential_is_ok(self):
        self.write()
        run = self.configured_github(
            secrets=("PROJECT_STATUS_APP_ID", "PROJECT_BOARD_APP_PRIVATE_KEY"))
        self.check(run)
        self.assertIn("✓", self.line("oard credential"))

    HINT = "consider a machine account or the GitHub App"

    def test_pat_with_required_reviews_shows_a_hint_and_keeps_the_exit_code(self):
        self.write(approvals=1)
        self.assertEqual(self.check(self.configured_github()), 0)
        hint = self.line("Hint:")
        self.assertIn(self.HINT, hint)
        self.assertIn("docs/bootstrap.md#board-credentials", hint)
        self.assertIn("✓", self.line("oard credential"))

    def test_missing_review_section_is_an_error(self):
        self.write()
        path = self.root / "project-context.toml"
        text = path.read_text()
        path.write_text(re.sub(r"\[review\]\n(?:[^\[\n].*\n|\n)*", "", text))
        self.assertNotEqual(self.check(), 0)
        self.assertIn("project-context.toml is invalid", self.out)
        self.assertIn("add `[review] required_approvals = N`", self.out)

    def test_pat_without_required_reviews_shows_no_hint(self):
        self.write(approvals=0)
        self.assertEqual(self.check(self.configured_github()), 0)
        self.assertNotIn(self.HINT, self.out)

    def test_app_and_pat_show_no_hint(self):
        self.write(approvals=1)
        run = self.configured_github(secrets=(
            "PROJECT_STATUS_APP_ID", "PROJECT_BOARD_APP_PRIVATE_KEY", "PROJECT_BOARD_TOKEN"))
        self.assertEqual(self.check(run), 0)
        self.assertNotIn(self.HINT, self.out)

    def test_no_credential_shows_no_hint(self):
        self.write(approvals=1)
        self.assertEqual(self.check(self.configured_github(secrets=())), 0)
        self.assertNotIn(self.HINT, self.out)


class AgentStepTest(CheckTestCase):
    def test_problems_fail_and_point_to_agent_setup(self):
        self.write()
        bootstrap_check.agent_setup.find_problems.return_value = ["Spec Kit version is not 1"]
        self.assertEqual(self.check(self.configured_github()), 1)
        self.assertIn("✗", self.line("gent"))
        self.assertIn("agent_setup.py check", self.out)

    def test_no_generated_files_fail_and_point_to_sync(self):
        self.write()
        (self.root / ".claude" / "agents" / "a.md").unlink()
        self.assertEqual(self.check(self.configured_github()), 1)
        self.assertIn("not generated", self.line("gent"))
        self.assertIn("agent_setup.py sync", self.out)

    def test_only_missing_or_outdated_files_are_fixable_by_sync(self):
        self.write()
        self.assertFalse(bootstrap_check.check_agents(self.root).fixable_by_sync)
        (self.root / ".claude" / "agents" / "a.md").write_text("old")
        self.assertTrue(bootstrap_check.check_agents(self.root).fixable_by_sync)
        (self.root / ".claude" / "agents" / "a.md").unlink()
        self.assertTrue(bootstrap_check.check_agents(self.root).fixable_by_sync)
        bootstrap_check.agent_setup.find_problems.return_value = ["bad"]
        self.assertFalse(bootstrap_check.check_agents(self.root).fixable_by_sync)

    def test_no_agent_definitions_fail(self):
        self.write()
        bootstrap_check.agent_setup.expected_agent_files.return_value = ({}, [])
        self.assertEqual(self.check(self.configured_github()), 1)
        self.assertIn("No sub-agent definitions found", self.out)

    def test_changed_generated_files_are_out_of_date(self):
        self.write()
        (self.root / ".claude" / "agents" / "a.md").write_text("old")
        self.assertEqual(self.check(self.configured_github()), 1)
        self.assertIn("out of date", self.line("gent"))
        self.assertIn("python3 scripts/agent_setup.py sync", self.out)

    def test_only_the_selected_tools_are_compared(self):
        self.write()
        bootstrap_check.agent_setup.expected_agent_files.return_value = (
            {Path(".claude/agents/a.md"): "x", Path(".codex/agents/a.toml"): "x"}, [])
        with mock.patch.object(bootstrap_check.agent_setup, "selected_tools",
                               return_value=({"claude"}, None)):
            self.assertEqual(self.check(self.configured_github()), 0, self.out)


class ReadOnlyTest(CheckTestCase):
    def test_check_writes_nothing_locally_or_on_github(self):
        self.write()
        run = self.configured_github()
        before = snapshot(self.root)
        self.check(run)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(run.writes, [])
        fresh = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, fresh)
        self.root = fresh
        self.check()
        self.assertEqual(list(fresh.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
