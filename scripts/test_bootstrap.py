import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import project_context  # noqa: E402


class FakeRun:
    """Stands in for git and gh: records calls, answers from a table keyed by argv prefix."""

    def __init__(self, remote=None, board_number=7, board_error=None):
        self.remote = remote
        self.board_number = board_number
        self.board_error = board_error
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        if argv[:3] == ["git", "remote", "get-url"]:
            if self.remote is None:
                raise bootstrap.CommandError("No such remote 'origin'")
            return self.remote + "\n"
        if argv[:3] == ["gh", "project", "create"]:
            if self.board_error:
                raise bootstrap.CommandError(self.board_error)
            return json.dumps({"number": self.board_number, "url": "https://example.test"})
        raise AssertionError(f"unexpected command {argv}")


class BootstrapTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "my-dir"
        self.root.mkdir()
        self.target = self.root / "project-context.toml"

    def init(self, *args, run=None, tty=False, answers=()):
        run = run or FakeRun(remote="https://github.com/acme/widgets.git")
        answers = list(answers)
        prompts = []

        def prompt(text):
            prompts.append(text)
            return answers.pop(0)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(
                ["init", *args], root=self.root, run=run, is_tty=tty, prompt=prompt
            )
        self.out, self.err, self.prompts, self.run = out.getvalue(), err.getvalue(), prompts, run
        return code

    def written(self):
        with self.target.open("rb") as file:
            return tomllib.load(file)


class ParseRemoteTest(unittest.TestCase):
    def test_remote_forms(self):
        for url, expected in (
            ("https://github.com/acme/widgets.git", ("acme", "widgets")),
            ("https://github.com/acme/widgets", ("acme", "widgets")),
            ("https://github.com/acme/widgets/", ("acme", "widgets")),
            ("https://user@github.com/acme/widgets.git", ("acme", "widgets")),
            ("git@github.com:acme/widgets.git", ("acme", "widgets")),
            ("ssh://git@github.com/acme/widgets.git", ("acme", "widgets")),
            ("git@github.com:acme/my.repo.git", ("acme", "my.repo")),
            ("  https://github.com/Acme-Org/Widgets.git\n", ("Acme-Org", "Widgets")),
        ):
            with self.subTest(url=url):
                self.assertEqual(bootstrap.parse_remote(url), expected)

    def test_hosts_other_than_github_or_an_ssh_alias_give_no_defaults(self):
        for url in (
            "https://gitlab.com/acme/widgets.git",
            "git@bitbucket.org:acme/widgets.git",
            "ssh://git@git.example.com/acme/widgets.git",
        ):
            with self.subTest(url=url):
                self.assertIsNone(bootstrap.parse_remote(url))
        self.assertEqual(bootstrap.parse_remote("git@github-work:acme/widgets.git"), ("acme", "widgets"))

    def test_ports_and_github_aliases(self):
        for url in (
            "ssh://git@github.com:22/acme/w.git",
            "ssh://git@ssh.github.com:443/acme/w.git",
            "git@github.com-work:acme/w.git",
            "git@github-work:acme/w.git",
            "https://github-work/acme/w.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(bootstrap.parse_remote(url), ("acme", "w"))

    def test_dotless_hosts_only_count_in_scp_form(self):
        for url in ("https://gitlab/acme/w", "ssh://git@gitlab/acme/w.git", "git@gitlab:acme/w.git"):
            with self.subTest(url=url):
                if url.startswith("git@"):
                    self.assertEqual(bootstrap.parse_remote(url), ("acme", "w"))
                else:
                    self.assertIsNone(bootstrap.parse_remote(url))

    def test_unparseable_remotes_give_none(self):
        for url in ("", "   ", "/local/path/repo.git", "https://github.com/onlyowner", "nonsense"):
            with self.subTest(url=url):
                self.assertIsNone(bootstrap.parse_remote(url))


class EmitterTest(unittest.TestCase):
    def test_hostile_strings_round_trip(self):
        hostile = [
            'quote " inside',
            "back\\slash and \\n literal",
            "line1\nline2\r\nline3",
            "tab\there",
            "control \x01\x1f\x7f chars",
            '"""triple"""',
            "'single' and ''' triple",
            "unicode åäö 日本 😀",
            "# not a comment",
            "[section]\nkey = 1",
            "ends with backslash\\",
            "",
        ]
        for value in hostile:
            with self.subTest(value=value):
                context = {
                    "project": {"name": value, "description": value},
                    "github_project": {"owner": "acme", "number": 5},
                }
                self.assertEqual(tomllib.loads(bootstrap.emit_toml(context)), context)

    def test_section_order_and_types(self):
        context = {
            "project": {"name": "n", "description": "d"},
            "github_project": {"owner": "acme", "number": 5},
            "review": {"required_approvals": 0},
            "toolkit": {"export_source": True},
        }
        text = bootstrap.emit_toml(context)
        self.assertEqual(tomllib.loads(text), context)
        order = [text.index(f"[{name}]") for name in ("project", "github_project", "review", "toolkit")]
        self.assertEqual(order, sorted(order))
        self.assertIn("export_source = true", text)
        self.assertTrue(text.endswith("\n"))

    def test_unsupported_values_are_refused(self):
        with self.assertRaises(TypeError):
            bootstrap.emit_toml({"project": {"name": 1.5}})


class InitDefaultsTest(BootstrapTestCase):
    def test_non_interactive_uses_remote_defaults(self):
        code = self.init("--description", "A tool", "--project-number", "3", "--yes")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(
            self.written(),
            {
                "project": {"name": "widgets", "description": "A tool"},
                "github_project": {"owner": "acme", "number": 3},
                "review": {"required_approvals": 1},
            },
        )
        project_context.load_context(self.target)

    def test_flags_override_defaults(self):
        code = self.init(
            "--name", "Custom", "--description", "D", "--owner", "other-org",
            "--project-number", "9", "--required-approvals", "0",
        )
        self.assertEqual(code, 0, self.err)
        data = self.written()
        self.assertEqual(data["project"]["name"], "Custom")
        self.assertEqual(data["github_project"], {"owner": "other-org", "number": 9})
        self.assertEqual(data["review"], {"required_approvals": 0})

    def test_without_a_remote_the_name_is_the_directory_and_owner_is_required(self):
        run = FakeRun(remote=None)
        code = self.init("--description", "D", "--project-number", "1", run=run)
        self.assertEqual(code, 1)
        self.assertIn("--owner", self.err)
        self.assertFalse(self.target.exists())

        code = self.init("--description", "D", "--project-number", "1", "--owner", "acme", run=run)
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["project"]["name"], "my-dir")

    def test_toolkit_section_only_when_asked(self):
        self.init("--description", "D", "--project-number", "1")
        self.assertNotIn("toolkit", self.written())
        self.init("--description", "D", "--project-number", "1", "--force", "--toolkit-export-source")
        self.assertEqual(self.written()["toolkit"], {"export_source": True})

    def test_output_names_the_file_and_next_steps_without_slash_commands(self):
        self.init("--description", "D", "--project-number", "1")
        self.assertIn(str(self.target), self.out)
        for step in ("speckit-constitution", "roadmap-write", "bootstrap.py github"):
            self.assertIn(step, self.out)
        self.assertNotIn("/speckit", self.out)
        self.assertLess(self.out.index("speckit-constitution"), self.out.index("roadmap-write"))
        self.assertLess(self.out.index("roadmap-write"), self.out.index("bootstrap.py github"))


class InitPromptTest(BootstrapTestCase):
    def test_tty_prompts_for_missing_values_and_shows_defaults(self):
        code = self.init(tty=True, answers=["", "My description", "", "4", ""])
        self.assertEqual(code, 0, self.err)
        self.assertEqual(
            self.written(),
            {
                "project": {"name": "widgets", "description": "My description"},
                "github_project": {"owner": "acme", "number": 4},
                "review": {"required_approvals": 1},
            },
        )
        joined = "\n".join(self.prompts)
        self.assertIn("widgets", joined)
        self.assertIn("acme", joined)

    def test_tty_answers_replace_defaults(self):
        code = self.init(tty=True, answers=["Typed", "Desc", "typed-org", "2", "0"])
        self.assertEqual(code, 0, self.err)
        data = self.written()
        self.assertEqual(data["project"]["name"], "Typed")
        self.assertEqual(data["github_project"]["owner"], "typed-org")
        self.assertEqual(data["review"]["required_approvals"], 0)

    def test_flags_are_not_prompted_for(self):
        code = self.init(
            "--name", "N", "--description", "D", "--owner", "acme", "--project-number", "1",
            "--required-approvals", "1", tty=True, answers=[],
        )
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.prompts, [])

    def test_yes_never_prompts(self):
        code = self.init("--yes", tty=True, answers=[])
        self.assertEqual(code, 1)
        self.assertEqual(self.prompts, [])
        self.assertIn("--description", self.err)

    def test_empty_required_answer_fails_with_the_flag_name(self):
        code = self.init(tty=True, answers=["", "", "", "", ""])
        self.assertEqual(code, 1)
        self.assertIn("--description", self.err)
        self.assertFalse(self.target.exists())


class InitNonInteractiveFailureTest(BootstrapTestCase):
    def test_missing_description_names_the_flag(self):
        code = self.init("--project-number", "1")
        self.assertEqual(code, 1)
        self.assertIn("--description", self.err)
        self.assertFalse(self.target.exists())

    def test_missing_project_number_names_both_ways_to_get_one(self):
        code = self.init("--description", "D")
        self.assertEqual(code, 1)
        self.assertIn("--project-number", self.err)
        self.assertIn("--create-board", self.err)
        self.assertFalse(self.target.exists())


class InitOverwriteTest(BootstrapTestCase):
    def test_existing_file_is_not_overwritten_without_force(self):
        self.target.write_text("keep me\n", encoding="utf-8")
        code = self.init("--description", "D", "--project-number", "1")
        self.assertEqual(code, 1)
        self.assertIn("--force", self.err)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "keep me\n")

    def test_force_replaces_the_file_and_leaves_no_temp_files(self):
        self.target.write_text("old\n", encoding="utf-8")
        code = self.init("--description", "D", "--project-number", "1", "--force")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["project"]["description"], "D")
        self.assertEqual([p.name for p in self.root.iterdir()], ["project-context.toml"])


class InitValidationTest(BootstrapTestCase):
    def test_invalid_values_are_rejected_before_writing(self):
        for args, expected in (
            (("--owner", "-bad"), "--owner: '-bad' must be a GitHub owner login"),
            (("--owner", "has space"), "--owner: 'has space' must be a GitHub owner login"),
            (("--name", "   "), "--name: '   ' must be a non-empty string"),
            (("--description", "  "), "--description: '  ' must be a non-empty string"),
            (("--description", ""), "--description: '' must be a non-empty string"),
            (("--project-number", "0"), "--project-number: 0 must be a positive integer"),
            (("--required-approvals", "-1"), "--required-approvals: -1 must be a non-negative integer"),
        ):
            with self.subTest(args=args):
                base = {"--name": "N", "--description": "D", "--owner": "acme",
                        "--project-number": "1", "--required-approvals": "1"}
                for i in range(0, len(args), 2):
                    base[args[i]] = args[i + 1]
                argv = [f"{flag}={value}" for flag, value in base.items()]
                code = self.init(*argv)
                self.assertEqual(code, 1)
                self.assertIn(expected, self.err)
                self.assertFalse(self.target.exists())

    def test_non_numeric_numbers_are_rejected(self):
        for flag in ("--project-number", "--required-approvals"):
            with self.subTest(flag=flag):
                argv = ["--description", "D", "--project-number", "1", "--required-approvals", "1"]
                argv[argv.index(flag) + 1] = "abc"
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
                    self.init(*argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse(self.target.exists())

    def test_hostile_description_is_written_safely(self):
        hostile = 'He said "hi"\\\nsecond line\n[evil]\nkey = 1'
        code = self.init("--description", hostile, "--project-number", "1")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["project"]["description"], hostile)


class CreateBoardTest(BootstrapTestCase):
    def test_create_board_uses_gh_and_the_returned_number(self):
        run = FakeRun(remote="git@github.com:acme/widgets.git", board_number=42)
        code = self.init("--description", "D", "--create-board", run=run)
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["github_project"], {"owner": "acme", "number": 42})
        create = [c for c in run.calls if c[:3] == ["gh", "project", "create"]]
        self.assertEqual(
            create,
            [["gh", "project", "create", "--owner", "acme", "--title", "widgets", "--format", "json"]],
        )

    def test_create_board_conflicts_with_project_number(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            self.init("--description", "D", "--create-board", "--project-number", "1")
        self.assertEqual(raised.exception.code, 2)

    def test_invalid_input_never_creates_a_board(self):
        run = FakeRun(remote="git@github.com:acme/widgets.git")
        code = self.init("--description", "D", "--create-board", "--owner=-bad", run=run)
        self.assertEqual(code, 1)
        self.assertEqual([c for c in run.calls if c[0] == "gh"], [])

    def test_existing_file_never_creates_a_board(self):
        self.target.write_text("x\n", encoding="utf-8")
        run = FakeRun(remote="git@github.com:acme/widgets.git")
        code = self.init("--description", "D", "--create-board", run=run)
        self.assertEqual(code, 1)
        self.assertEqual([c for c in run.calls if c[0] == "gh"], [])

    def test_gh_failure_and_bad_output_are_reported(self):
        run = FakeRun(remote="git@github.com:acme/widgets.git", board_error="not logged in")
        self.assertEqual(self.init("--description", "D", "--create-board", run=run), 1)
        self.assertIn("not logged in", self.err)
        self.assertFalse(self.target.exists())

        for bad in ("not json", json.dumps({"number": "x"}), json.dumps({"number": True}), "[]"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    bootstrap.create_board("acme", "t", lambda argv, cwd=None: bad)


class CreateBoardFailureTest(BootstrapTestCase):
    def test_write_failure_after_board_creation_reports_the_board(self):
        run = FakeRun(remote="git@github.com:acme/widgets.git", board_number=42)
        original = bootstrap.write_atomically

        def failing(*args, **kwargs):
            raise OSError("disk full")

        bootstrap.write_atomically = failing
        self.addCleanup(setattr, bootstrap, "write_atomically", original)
        code = self.init("--description", "D", "--create-board", run=run)
        self.assertEqual(code, 1)
        self.assertIn("board #42 was created; rerun with --project-number 42", self.err)
        self.assertIn("disk full", self.err)

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_unwritable_directory_is_detected_before_creating_a_board(self):
        run = FakeRun(remote="git@github.com:acme/widgets.git")
        self.root.chmod(0o500)
        self.addCleanup(self.root.chmod, 0o700)
        code = self.init("--description", "D", "--create-board", run=run)
        self.assertEqual(code, 1)
        self.assertEqual([c for c in run.calls if c[0] == "gh"], [])
        self.assertIn(str(self.root), self.err)


class PromptEdgeTest(BootstrapTestCase):
    def init_with_prompt(self, prompt, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(
                ["init", *args], root=self.root,
                run=FakeRun(remote="https://github.com/acme/widgets.git"),
                is_tty=True, prompt=prompt,
            )
        self.err = err.getvalue()
        return code

    def test_end_of_input_names_the_flag(self):
        def eof(text):
            raise EOFError

        code = self.init_with_prompt(eof)
        self.assertEqual(code, 1)
        self.assertIn("input ended: pass --name", self.err)
        self.assertNotIn("Traceback", self.err)

    def test_interrupt_exits_130_quietly(self):
        def interrupt(text):
            raise KeyboardInterrupt

        code = self.init_with_prompt(interrupt)
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", self.err)
        self.assertFalse(self.target.exists())

    def test_empty_board_answer_creates_a_board(self):
        run = FakeRun(remote="git@github.com:acme/widgets.git", board_number=11)
        code = self.init(tty=True, answers=["", "Desc", "", "", ""], run=run)
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["github_project"]["number"], 11)
        self.assertIn("GitHub Project number (empty to create one)", "\n".join(self.prompts))


class WriteTest(BootstrapTestCase):
    @unittest.skipUnless(hasattr(os, "umask"), "needs umask")
    def test_new_file_gets_the_default_mode_not_0600(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.init("--description", "D", "--project-number", "1")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)

    @unittest.skipUnless(hasattr(os, "umask"), "needs umask")
    def test_force_keeps_the_existing_mode(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.target.write_text("x\n", encoding="utf-8")
        self.target.chmod(0o600)
        self.init("--description", "D", "--project-number", "1", "--force")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_file_created_meanwhile_is_not_overwritten_without_overwrite(self):
        self.target.write_text("keep\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            bootstrap.write_atomically(self.target, "new\n", overwrite=False)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual([p.name for p in self.root.iterdir()], ["project-context.toml"])

    def test_new_file_does_not_need_hard_links(self):
        original = os.link

        def no_link(*args, **kwargs):
            raise OSError("hard links not supported")

        os.link = no_link
        self.addCleanup(setattr, os, "link", original)
        bootstrap.write_atomically(self.target, "new\n", overwrite=False)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new\n")

    def test_bad_values_in_errors_stay_on_one_line(self):
        code = self.init("--description", "D", "--project-number", "1", "--owner=bad\nowner")
        self.assertEqual(code, 1)
        self.assertIn("--owner: 'bad\\nowner' must be a GitHub owner login", self.err)
        self.assertEqual(len(self.err.strip().splitlines()), 1)

    def test_overwrite_true_replaces(self):
        self.target.write_text("old\n", encoding="utf-8")
        bootstrap.write_atomically(self.target, "new\n", overwrite=True)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new\n")


class CliTest(unittest.TestCase):
    def test_no_subcommand_prints_the_check_header(self):
        with tempfile.TemporaryDirectory() as directory, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            bootstrap.main([], root=directory, run=FakeRun(), is_tty=False)
        self.assertIn("Bootstrap status", out.getvalue())

    def test_an_unknown_subcommand_is_rejected(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            bootstrap.main(["nope"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
