import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import bootstrap_check  # noqa: E402
import bootstrap_github  # noqa: E402
import bootstrap_start  # noqa: E402
from display_text_testing import raw_unsafe  # noqa: E402
import bootstrap_steps  # noqa: E402
import project_context  # noqa: E402
import test_bootstrap_check  # noqa: E402


class _LazyModule:
    """`test_bootstrap_start` imports this module (through test_bootstrap_steps), so it is
    imported on first use."""

    def __getattr__(self, name):
        import test_bootstrap_start as module
        return getattr(module, name)


test_bootstrap_start = _LazyModule()
from test_bootstrap_github import (  # noqa: E402
    FakeGitHub, has_field, has_input, is_mutation, rest_methods)


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
        }
        text = bootstrap.emit_toml(context)
        self.assertEqual(tomllib.loads(text), context)
        order = [text.index(f"[{name}]") for name in ("project", "github_project", "review")]
        self.assertEqual(order, sorted(order))
        self.assertIn("required_approvals = 0", text)
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
                "review": {"required_approvals": 0},
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

    def test_a_defaulted_zero_prints_a_notice_but_an_explicit_value_does_not(self):
        notice = ("Notice: required_approvals = 0 (solo project). "
                  "A team project must pass --required-approvals 1 or more.")
        self.init("--description", "D", "--project-number", "1")
        self.assertIn(notice, self.out)
        for value in ("0", "1"):
            self.init("--description", "D", "--project-number", "1",
                      "--required-approvals", value, "--force")
            self.assertNotIn("Notice: required_approvals", self.out)
        self.init("--force", tty=True, answers=["", "Desc", "", "4", ""])
        self.assertIn(notice, self.out)

    def test_flag_value_is_written_explicitly(self):
        code = self.init("--description", "D", "--project-number", "1", "--required-approvals", "2")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["review"], {"required_approvals": 2})
        self.assertIn("required_approvals = 2", self.target.read_text())

    def test_without_a_remote_the_name_is_the_directory_and_owner_is_required(self):
        run = FakeRun(remote=None)
        code = self.init("--description", "D", "--project-number", "1", run=run)
        self.assertEqual(code, 1)
        self.assertIn("--owner", self.err)
        self.assertFalse(self.target.exists())

        code = self.init("--description", "D", "--project-number", "1", "--owner", "acme", run=run)
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["project"]["name"], "my-dir")

    def test_init_writes_no_toolkit_section(self):
        self.init("--description", "D", "--project-number", "1")
        self.assertNotIn("toolkit", self.written())

    def test_the_retired_toolkit_export_source_option_is_rejected(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            self.init("--description", "D", "--project-number", "1", "--toolkit-export-source")
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.target.exists())

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
                "review": {"required_approvals": 0},
            },
        )
        joined = "\n".join(self.prompts)
        self.assertIn("widgets", joined)
        self.assertIn("acme", joined)

    def test_a_typed_zero_is_explicit_and_prints_no_notice(self):
        self.init(tty=True, answers=["", "Desc", "", "4", "0"])
        self.assertNotIn("Notice: required_approvals", self.out)

    def test_tty_prompt_proposes_zero(self):
        self.init(tty=True, answers=["", "Desc", "", "4", ""])
        self.assertIn("Required approvals (0 for a solo project) [0]: ", self.prompts)

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
            (("--name", "   "), "--name: '   ' must be a non-empty single-line string "
                                "without control characters"),
            (("--description", "  "), "--description: '  ' must be a non-empty single-line "
                                     "string without control characters"),
            (("--description", ""), "--description: '' must be a non-empty single-line "
                                    "string without control characters"),
            (("--description", "a\x85b"), "--description: 'a\\x85b' must be a non-empty "
                                          "single-line string without control characters"),
            (("--name", "a\u2028b"), "--name: 'a\\u2028b' must be a non-empty single-line "
                                     "string without control characters"),
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

    def test_bidi_controls_are_rejected_before_writing(self):
        for flag in ("--name", "--description"):
            for char in ("\U0000202a", "\U0000202e", "\U00002066", "\U00002069",
                         "\U0000200e", "\U0000200f", "\U0000061c"):
                with self.subTest(flag=flag, character=f"U+{ord(char):04X}"):
                    base = {"--name": "N", "--description": "D", "--owner": "acme",
                            "--project-number": "1", "--required-approvals": "1"}
                    base[flag] = f"a{char}b"
                    code = self.init(*[f"{k}={v}" for k, v in base.items()])
                    self.assertEqual(code, 1)
                    self.assertIn(f"{flag}: 'a\\u{ord(char):04x}b' must be a non-empty "
                                  "single-line string without control characters", self.err)
                    self.assertFalse(self.target.exists())

    def test_other_format_characters_are_rejected_before_writing(self):
        for flag in ("--name", "--description"):
            for code in (0x200B, 0x2060, 0xFEFF, 0xE0041):
                with self.subTest(flag=flag, character=f"U+{code:04X}"):
                    base = {"--name": "N", "--description": "D", "--owner": "acme",
                            "--project-number": "1", "--required-approvals": "1"}
                    base[flag] = f"a{chr(code)}b"
                    code_ = self.init(*[f"{k}={v}" for k, v in base.items()])
                    self.assertEqual(code_, 1)
                    self.assertIn("must be a non-empty single-line string without control "
                                  "characters", self.err)
                    self.assertFalse(raw_unsafe(self.err), repr(self.err))
                    self.assertFalse(self.target.exists())

    def test_zero_width_joiner_and_soft_hyphen_still_pass(self):
        joined = "\U0001f468\U0000200d\U0001f469 co\U000000adoperate"
        code = self.init("--name", joined, "--description", joined, "--project-number", "1")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["project"]["name"], joined)

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
        hostile = 'He said "hi"\\ second line [evil] key = 1 \u00e4'
        code = self.init("--description", hostile, "--project-number", "1")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.written()["project"]["description"], hostile)


class ValidateInMemoryTest(unittest.TestCase):
    """`validate` is read-only and does no file I/O (it runs before any preview)."""

    CONTEXT = {"project": {"name": "N", "description": "D"},
               "github_project": {"owner": "acme", "number": 1},
               "review": {"required_approvals": 1}}

    def forbidden(self):
        message = "validate must not touch the file system"
        return (mock.patch("tempfile.TemporaryDirectory", side_effect=AssertionError(message)),
                mock.patch("tempfile.mkstemp", side_effect=AssertionError(message)),
                mock.patch.object(Path, "write_text", side_effect=AssertionError(message)),
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError(message)))

    def test_a_valid_context_passes_without_file_io(self):
        with contextlib.ExitStack() as stack:
            for patch in self.forbidden():
                stack.enter_context(patch)
            bootstrap.validate(self.CONTEXT)

    def test_a_value_that_cannot_be_written_is_rejected_as_before(self):
        context = {**self.CONTEXT, "project": {"name": "w\udc80", "description": "D"}}

        with self.assertRaises(ValueError) as raised:
            bootstrap.validate(context)
        # the message holds the raw value; it is escaped where it is printed
        self.assertEqual("--name: 'w" + chr(0xDC80) + "' contains characters that cannot be "
                         "written as UTF-8", str(raised.exception))
        self.assertEqual("--name: 'w\\udc80' contains characters that cannot be written as UTF-8",
                         bootstrap.display_text.error_text(raised.exception))

    def test_the_unwritable_value_names_its_own_flag(self):
        for section, key, flag in (("project", "description", "--description"),
                                   ("github_project", "owner", "--owner")):
            with self.subTest(flag=flag):
                context = {**self.CONTEXT, section: {**self.CONTEXT[section], key: "x\udc80"}}

                with self.assertRaises(ValueError) as raised:
                    bootstrap.validate(context)

                self.assertTrue(str(raised.exception).startswith(f"{flag}: "))
                self.assertIn("cannot be written as UTF-8", str(raised.exception))
                self.assertNotIn("codec", str(raised.exception))

    def test_an_invalid_value_raises_an_error_carrying_the_flag_and_the_requirement(self):
        for context, flag, requirement in (
                ({**self.CONTEXT, "review": {"required_approvals": -1}},
                 "--required-approvals", "must be a non-negative integer"),
                ({**self.CONTEXT, "project": {"name": "w\udc80", "description": "D"}},
                 "--name", "contains characters that cannot be written as UTF-8")):
            with self.subTest(flag=flag):
                with self.assertRaises(bootstrap.project_context.InvalidValue) as raised:
                    bootstrap.validate(context)

                self.assertIsInstance(raised.exception, ValueError)
                self.assertEqual(flag, raised.exception.flag)
                self.assertEqual(requirement, raised.exception.requirement)

    def test_a_too_long_description_is_reported_with_the_length_message(self):
        context = {**self.CONTEXT, "project": {"name": "N", "description": "D" * 351}}

        with self.assertRaises(bootstrap.project_context.InvalidValue) as raised:
            bootstrap.validate(context)

        self.assertEqual("--description", raised.exception.flag)
        self.assertEqual(bootstrap.project_context.DESCRIPTION_TOO_LONG_MESSAGE,
                         raised.exception.requirement)

    def test_init_and_github_reject_abbreviated_options(self):
        parser = bootstrap.build_parser()
        for argv in (["init", "--desc", "D"], ["init", "--proj", "3"], ["github", "--appl"],
                     ["github", "--allow", "x"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as raised, \
                        contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)
                self.assertEqual(2, raised.exception.code)

    def test_every_documented_init_and_github_option_still_parses(self):
        parser = bootstrap.build_parser()
        init = parser.parse_args([
            "init", "--name", "n", "--description", "D", "--owner", "o", "--project-number", "3",
            "--required-approvals", "1", "--force"])
        self.assertEqual(("n", "D", "o", 3, 1, True),
                         (init.name, init.description, init.owner, init.project_number,
                          init.required_approvals, init.force))
        self.assertTrue(parser.parse_args(["init", "--create-board"]).create_board)
        self.assertTrue(parser.parse_args(["github", "--apply"]).apply)
        self.assertTrue(parser.parse_args(["github", "--check"]).check)
        self.assertTrue(parser.parse_args(["github", "--apply", "--allow-weaken"]).allow_weaken)

    def test_an_invalid_context_keeps_its_flag_message_without_file_io(self):
        context = {**self.CONTEXT, "review": {"required_approvals": -1}}
        with contextlib.ExitStack() as stack:
            for patch in self.forbidden():
                stack.enter_context(patch)
            with self.assertRaises(ValueError) as raised:
                bootstrap.validate(context)
        self.assertEqual("--required-approvals: -1 must be a non-negative integer",
                         str(raised.exception))


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

    def test_a_name_that_cannot_be_written_never_creates_a_board(self):
        run = FakeRun(remote="git@github.com:acme/widgets.git")
        code = self.init("--description", "D", "--create-board", "--name", "w\udc80", run=run)
        self.assertEqual(code, 1)
        self.assertIn("--name:", self.err)
        self.assertIn("cannot be written as UTF-8", self.err)
        self.assertNotIn("codec", self.err)
        self.assertEqual([c for c in run.calls if c[:3] == ["gh", "project", "create"]], [])
        self.assertFalse(self.target.exists())

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


READ_COMMANDS = (
    ["gh", "auth", "status"], ["gh", "repo", "view"], ["gh", "api"], ["gh", "issue", "list"],
    ["gh", "pr", "list"], ["gh", "pr", "view"], ["gh", "label", "list"], ["gh", "secret", "list"],
    ["gh", "variable", "list"], ["git", "remote", "get-url"], ["git", "branch", "--show-current"],
    ["git", "branch", "--format=%(refname:short)"], ["git", "status", "--porcelain"],
    ["git", "rev-parse"], ["git", "merge-base", "--is-ancestor"], ["git", "cat-file", "-e"],
    ["git", "config", "--get"],  # only the exact read of a value; `git config key value` writes
    # the first-sync dry run: the script without --apply (is_read rejects any argv with it)
    ["python3", "scripts/sync-roadmap-issues.py"],
)


def is_read(argv, body=None):
    """True when argv starts with an allowlisted read command (an allowlist, not a denylist).

    `gh_json` sends every GraphQL body with `--input`, queries and mutations alike, so a
    `gh api graphql` call is a read only when its `body` (captured at call time, as the temp
    file is deleted after the call) holds no mutation.
    """
    if not any(argv[: len(prefix)] == prefix for prefix in READ_COMMANDS):
        return False
    if argv[0] == "python3" and (argv[1:] not in (["scripts/sync-roadmap-issues.py"],
                                                  ["scripts/sync-roadmap-issues.py",
                                                   "--skip-project"])):
        return False  # only the exact dry-run argv; never --apply or anything else
    if argv[:2] == ["gh", "api"]:
        if has_field(argv):
            return False
        if has_input(argv):
            # a REST body makes the call a POST without -X
            return argv[:3] == ["gh", "api", "graphql"] and not is_mutation(body)
        if any(method.upper() != "GET" for method in rest_methods(argv)):
            return False
    return True


class IsReadTest(unittest.TestCase):
    """The read allowlist must reject every REST write spelling of `gh api`."""

    def test_the_first_sync_reads_are_reads_but_apply_never_is(self):
        for argv in (["git", "rev-parse", "HEAD"], ["git", "cat-file", "-e", "abc^{commit}"],
                     ["git", "merge-base", "--is-ancestor", "a", "HEAD"],
                     ["gh", "pr", "list", "--repo", "o/r"],
                     ["gh", "pr", "view", "5", "--repo", "o/r", "--json", "headRefOid"],
                     ["python3", "scripts/sync-roadmap-issues.py"],
                     ["python3", "scripts/sync-roadmap-issues.py", "--skip-project"]):
            with self.subTest(argv=argv):
                self.assertTrue(is_read(argv))
        for argv in (["python3", "scripts/sync-roadmap-issues.py", "--apply"],
                     ["python3", "scripts/sync-roadmap-issues.py", "--skip-project", "--apply"],
                     ["python3", "scripts/other.py"], ["git", "merge-base", "HEAD"] + ["--x"]):
            with self.subTest(argv=argv):
                self.assertFalse(is_read(argv))

    def test_gh_api_writes_are_not_reads(self):
        for argv in (
            ["gh", "api", "repos/o/r/rulesets", "--input", "f.json"],
            ["gh", "api", "repos/o/r/rulesets", "-X", "POST"],
            ["gh", "api", "repos/o/r/rulesets", "-XPOST"],
            ["gh", "api", "repos/o/r/rulesets", "--method", "PUT"],
            ["gh", "api", "repos/o/r/rulesets", "--method=DELETE"],
            ["gh", "api", "repos/o/r/rulesets", "-f", "a=b"],
            ["gh", "api", "repos/o/r/rulesets", "--input=f.json"],
            ["gh", "api", "repos/o/r/rulesets", "--field=a=b"],
            ["gh", "api", "repos/o/r/rulesets", "--raw-field=a=b"],
            ["gh", "api", "repos/o/r/rulesets", "-fa=b"],
            ["gh", "api", "repos/o/r/rulesets", "-Fa=b"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(is_read(argv))

    def test_a_graphql_mutation_is_not_a_read(self):
        argv = ["gh", "api", "graphql", "--input", "f.json"]
        self.assertFalse(is_read(argv, {"query": "mutation { x }"}))
        self.assertFalse(is_read(argv, None))  # an unreadable body is not known to be a query

    def test_the_fakes_capture_the_graphql_body_before_the_file_disappears(self):
        for fake in (RecordingRun(), ReadingRun()):
            with self.subTest(fake=type(fake).__name__), \
                    tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "body.json"
                path.write_text(json.dumps({"query": "mutation { x }", "variables": {}}))
                try:
                    fake(["gh", "api", "graphql", "--input", str(path)])
                except (bootstrap.CommandError, KeyError, AssertionError):
                    pass  # only the recording matters here, not the fake's answer
                path.unlink()
                self.assertEqual(1, len(fake.calls))
                self.assertFalse(is_read(fake.calls[0], fake.bodies[0]))

    def test_gh_api_reads_are_reads(self):
        for argv in (
            ["gh", "api", "repos/o/r/rulesets"],
            ["gh", "api", "repos/o/r/rulesets", "-X", "GET"],
            ["gh", "api", "repos/o/r/rulesets", "--method=GET"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(is_read(argv))

    def test_a_graphql_query_is_a_read(self):
        argv = ["gh", "api", "graphql", "--input", "f.json"]
        self.assertTrue(is_read(argv, {"query": "query { viewer { id } }"}))


class RecordingRun:
    """Records every command and fails each one as an unreachable gh or git would."""

    def __init__(self):
        self.calls = []
        self.bodies = {}  # call index -> body, read before the temp file goes

    def __call__(self, argv, cwd=None):
        self.bodies[len(self.calls)] = FakeGitHub.body(list(argv))
        self.calls.append(list(argv))
        raise bootstrap.CommandError(f"cannot run {argv[0]}: No such file or directory")


class ReadingRun(test_bootstrap_check.CheckGitHub):
    """A gh and git that answer every read, so the real builder runs past its first calls.

    Commands outside the answers below are recorded and then fail, so nothing a builder adds
    can go unseen by the allowlist.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra_calls = []
        self.bodies = {}  # call index -> body, read before the temp file goes

    @staticmethod
    def repository_answer(argv):
        """What each read of a repository with bootstrap merged returns, in gh's output shape
        (a listing of branch names with --jq, the issue listing as one JSON object per line)."""
        joined = " ".join(argv)
        if argv[:3] == ["gh", "repo", "view"] and "defaultBranchRef" in argv:
            return '{"defaultBranchRef": {"name": "main"}}'
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return "" if "/issues?" in joined else "main\n"  # no issues yet
        if argv[:2] == ["gh", "api"] and "/contents/" in argv[-1]:
            path = argv[-1].split("/contents/")[1].split("?")[0]
            return {"project-context.toml": test_bootstrap_check.CONTEXT.format(approvals=1),
                    "ROADMAP.md": test_bootstrap_check.ROADMAP,
                    ".specify/memory/constitution.md": "# Constitution\n",
                    }.get(path)  # any other path is unanswered, so the parent fake rejects it
        if argv[:2] == ["gh", "api"] and "/branches/" in joined:
            return "abc\n"
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return "abc\n"
        if argv[:4] == ["git", "config", "--get", "remote.origin.url"]:
            return "https://github.com/acme/widgets.git\n"
        if argv[0] == "python3":  # the first-sync dry run: nothing pending
            return ""
        return None

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.bodies[len(self.calls)] = FakeGitHub.body(argv)
        answers = (
            (["git", "remote", "get-url"], "https://github.com/acme/widgets.git\n"),
            (["gh", "api", "user"], "tester\n"),
            (["gh", "issue", "list"], "[]"),
            (["git", "branch", "--show-current"], "main\n"),
            (["git", "branch", "--format=%(refname:short)"], "main\n"),
            (["git", "status", "--porcelain"], ""),
        )
        for prefix, answer in answers:
            if argv[: len(prefix)] == prefix:
                self.calls.append(argv)
                return answer
        answer = self.repository_answer(argv)
        if answer is not None:
            self.calls.append(argv)
            return answer
        try:
            return super().__call__(argv, cwd)
        except AssertionError:  # an unanswered command: already recorded by FakeGitHub
            raise bootstrap.CommandError(f"unanswered command {argv}")


def report(result="incomplete", state="todo"):
    """A minimal report in the shape of contracts/check-json.md (format version 1)."""
    return {
        "format_version": 1, "result": result, "next_step": None, "bootstrap_merged": False,
        "default_branch": "main",
        "steps": [{"id": "prerequisites", "title": "t", "state": state, "blocking": True,
                   "actor": "user", "needs_decision": False, "needs_confirmation": False,
                   "performable": False, "bypasses_review": False, "action": None,
                   "details": {}}],
    }


class CheckJsonTest(unittest.TestCase):
    """`check --json`: the report builder is bootstrap_steps.build_report(root, run,
    remote_default) -> dict; the CLI serialises it with json.dumps (contracts/check-json.md)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def check_json(self, build=None, run=None):
        run = run or RecordingRun()
        out, err = io.StringIO(), io.StringIO()
        patched = (mock.patch.object(bootstrap_steps, "build_report", build, create=True)
                   if build else contextlib.nullcontext())
        with patched, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(["check", "--json"], root=self.root, run=run, is_tty=False)
        self.out, self.err, self.run = out.getvalue(), err.getvalue(), run
        return code

    def test_prints_exactly_one_json_document_on_stdout(self):
        code = self.check_json(lambda root, run, remote_default, push_remote_default=None: report())
        self.assertEqual(code, 1)
        document = json.loads(self.out)  # raises when stdout holds anything else
        self.assertEqual(document, report())
        self.assertNotIn("Bootstrap status", self.out)

    def test_an_unexpected_error_in_the_builder_prints_to_stderr_only_and_exits_2(self):
        def build(root, run, remote_default, push_remote_default=None):
            raise KeyError("surprise")

        code = self.check_json(build)

        self.assertEqual(2, code)
        self.assertEqual("", self.out)
        self.assertIn("surprise", self.err)

    def test_exit_code_follows_the_result(self):
        for result, state, expected in (
            ("complete", "done", 0),
            ("complete_with_manual_settings", "manual", 0),
            ("incomplete", "todo", 1),
        ):
            with self.subTest(result=result):
                code = self.check_json(
                    lambda root, run, remote_default, push_remote_default=None: report(result, state))
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(self.out)["result"], result)

    def test_the_builder_receives_root_run_and_the_remote_default_lookup(self):
        seen = {}

        def build(root, run, remote_default, push_remote_default=None):
            seen.update(root=Path(root), run=run, remote_default=remote_default)
            return report()

        run = RecordingRun()
        self.check_json(build, run)
        self.assertEqual(seen["root"], self.root)
        self.assertIs(seen["run"], run)
        self.assertTrue(callable(seen["remote_default"]))

    def test_no_report_means_exit_2_with_the_message_on_stderr_and_nothing_on_stdout(self):
        for error in (bootstrap.CommandError("boom"), ValueError("boom"), OSError("boom")):
            def build(root, run, remote_default, push_remote_default=None, error=error):
                raise error

            with self.subTest(error=type(error).__name__):
                code = self.check_json(build)
                self.assertEqual(code, 2)
                self.assertEqual(self.out, "")
                self.assertIn("boom", self.err)

    def test_gh_missing_gives_not_checked_github_steps_and_exit_1(self):
        (self.root / "project-context.toml").write_text(
            test_bootstrap_check.CONTEXT.format(approvals=1))
        code = self.check_json(run=test_bootstrap_check.CheckGitHub(gh="missing"))
        self.assertEqual(code, 1)
        document = json.loads(self.out)
        self.assertEqual(document["format_version"], 1)
        self.assertEqual(document["result"], "incomplete")
        steps = {step["id"]: step for step in document["steps"]}
        self.assertEqual(len(document["steps"]), 11)
        for step_id in ("github_settings", "first_sync"):
            self.assertEqual(steps[step_id]["state"], "not_checked", step_id)
            self.assertTrue(steps[step_id]["title"])

    def test_gh_logged_out_gives_not_checked_github_steps_and_exit_1(self):
        run = test_bootstrap_check.CheckGitHub(gh="logged-out")
        code = self.check_json(run=run)
        self.assertEqual(code, 1)
        steps = {step["id"]: step for step in json.loads(self.out)["steps"]}
        self.assertEqual(steps["github_settings"]["state"], "not_checked")
        self.assertEqual(steps["first_sync"]["state"], "not_checked")

    def assert_only_reads(self, run):
        for index, call in enumerate(run.calls):
            self.assertTrue(is_read(call, run.bodies.get(index)),
                            f"not an allowlisted read: {call}")

    def test_with_every_command_failing_the_builder_issues_only_allowlisted_reads(self):
        (self.root / "project-context.toml").write_text("[project]\nname = \"x\"\n")
        before = test_bootstrap_check.snapshot(self.root)
        self.assertEqual(self.check_json(), 1)
        self.assert_only_reads(self.run)
        self.assertEqual(test_bootstrap_check.snapshot(self.root), before)

    def test_with_every_read_succeeding_the_builder_reaches_its_reads_and_writes_nothing(self):
        # A failing fake stops the builder at its first reads; this run lets it go through
        # every step, so a write added anywhere in the builder is recorded.
        (self.root / "project-context.toml").write_text(
            test_bootstrap_check.CONTEXT.format(approvals=1))
        (self.root / ".specify" / "memory").mkdir(parents=True)
        (self.root / ".specify" / "memory" / "constitution.md").write_text("# C\n")
        (self.root / "ROADMAP.md").write_text(test_bootstrap_check.ROADMAP)
        (self.root / ".claude" / "agents").mkdir(parents=True)
        (self.root / ".claude" / "agents" / "a.md").write_text("x")
        files = ({Path(".claude/agents/a.md"): "x"}, [])
        for target, value in (("find_problems", []), ("expected_agent_files", files)):
            patcher = mock.patch.object(
                test_bootstrap_check.bootstrap_check.agent_setup, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        before = test_bootstrap_check.snapshot(self.root)
        run = ReadingRun()
        self.assertIn(self.check_json(run=run), (0, 1))
        steps = {step["id"]: step for step in json.loads(self.out)["steps"]}
        self.assertEqual(len(steps), 11)
        # every detector reached a real answer; a step stuck at not_checked proves nothing
        self.assertEqual([], [(i, s["title"]) for i, s in steps.items()
                              if s["state"] == "not_checked"])
        self.assertGreater(len(run.calls), 3, "the builder stopped before reaching its reads")
        for prefix in (["gh", "pr", "list"], ["python3", "scripts/sync-roadmap-issues.py"]):
            self.assertTrue(any(call[:len(prefix)] == prefix for call in run.calls), prefix)
        self.assertEqual(run.writes, [])
        self.assert_only_reads(run)
        self.assertEqual(test_bootstrap_check.snapshot(self.root), before)


class HumanCommandsUnchangedTest(test_bootstrap_check.CheckTestCase):
    """FR-014, research D1: check, init and github keep their output and exit codes."""

    def test_human_check_prints_the_six_step_report_and_exits_1_on_an_error(self):
        code = self.check()
        self.assertEqual(code, 1)
        lines = self.out.splitlines()
        self.assertEqual(lines[0], f"Bootstrap status for {self.root}")
        self.assertEqual(lines[1], "  \u2717 1. project-context.toml is missing")
        self.assertEqual(lines[2], "       Next: run `python3 scripts/bootstrap.py init`")
        self.assertEqual(sum(1 for line in lines if line.startswith(("  \u2717", "  \u2013", "  \u2713"))), 6)
        self.assertIn("Next: run `python3 scripts/bootstrap.py init`. See docs/bootstrap.md.", lines)
        with self.assertRaises(ValueError):
            json.loads(self.out)

    def test_human_check_passes_with_exit_0_and_the_wrap_up(self):
        self.write()
        run = self.configured_github()
        self.assertEqual(self.check(run), 0)
        self.assertIn("If you have not done so yet:", self.out)
        self.assertIn("gh workflow run sync-roadmap-issues.yml -f apply=true", self.out)

    def test_init_without_a_terminal_defaults_required_approvals_to_0(self):
        run = FakeRun(remote="https://github.com/acme/widgets.git")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(
                ["init", "--description", "d", "--project-number", "3"],
                root=self.root, run=run, is_tty=False,
                prompt=lambda text: self.fail("must not prompt without a terminal"))
        self.assertEqual(code, 0)
        with (self.root / "project-context.toml").open("rb") as file:
            self.assertEqual(tomllib.load(file)["review"]["required_approvals"], 0)
        self.assertIn(f"Wrote {self.root / 'project-context.toml'}", out.getvalue())
        self.assertIn("Notice: required_approvals = 0 (solo project).", out.getvalue())
        self.assertIn("Next steps:", out.getvalue())

    def test_init_failure_exits_1_with_the_flag_named_on_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(["init"], root=self.root, run=FakeRun(), is_tty=False)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("bootstrap init failed: Project description is required: pass --description",
                      err.getvalue())

    def test_github_preview_apply_and_check_keep_their_output_and_exit_codes(self):
        self.write()
        run = test_bootstrap_check.CheckGitHub()
        outputs = {}
        for command, expected in ((("github",), 0), (("github", "--apply"), 0),
                                  (("github", "--check"), 0)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = bootstrap.main(list(command), root=self.root, run=run, is_tty=False)
            self.assertEqual(code, expected, command)
            outputs[command[-1]] = out.getvalue()
        self.assertIn("Preview only; run with --apply to make these changes.", outputs["github"])
        self.assertIn("Ruleset: create Default branch protection (required approvals 1)",
                      outputs["github"])
        self.assertIn("Applied 14 change(s).", outputs["--apply"])
        self.assertIn("Nothing to change: labels, board, ruleset and fork PR approval match.",
                      outputs["--check"])

    def test_github_without_a_project_context_exits_2_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = bootstrap.main(["github", "--check"], root=self.root,
                                  run=test_bootstrap_check.CheckGitHub(), is_tty=False)
        self.assertEqual(code, 2)
        self.assertIn("bootstrap github failed: Missing project context", err.getvalue())


FIRST_SYNC = "first_sync"


def sync_pending():
    return test_bootstrap_start.report(test_bootstrap_start.step(FIRST_SYNC, action={
        "text": "Preview the sync, then apply it.", "command": None, "doc": "docs/x"}))


class StartSubcommandTest(BootstrapTestCase):
    """`bootstrap.py start` (T048, T049): the wiring only; the engine has its own tests."""

    def start(self, *args, report=None, run=None, tty=False, prompt=None, **options):
        report = report or sync_pending()
        out, err = io.StringIO(), io.StringIO()

        def no_prompt(text):
            raise AssertionError(f"prompt called: {text}")

        with mock.patch.object(bootstrap_steps, "build_report", return_value=report), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(["start", *args], root=self.root,
                                  run=run or test_bootstrap_start.SyncRun(), is_tty=tty,
                                  prompt=prompt or no_prompt, **options)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def test_the_subcommand_is_listed_and_documented(self):
        self.assertIn("start", bootstrap.build_parser().format_help())
        self.assertIn("start", bootstrap.__doc__)

    def test_exit_0_when_bootstrap_is_complete(self):
        done = test_bootstrap_start.report(test_bootstrap_start.done("prerequisites"))

        self.assertEqual(0, self.start(report=done))
        self.assertEqual("\nBOOTSTRAP IS COMPLETE\n", self.out[-len("\nBOOTSTRAP IS COMPLETE\n"):])
        self.assertNotIn("\x1b", self.out)  # captured output is no terminal: nothing is bold

    def test_exit_1_when_stopped_at_a_step_start_does_not_perform(self):
        stuck = test_bootstrap_start.report(
            test_bootstrap_start.step("constitution", performable=False))

        self.assertEqual(1, self.start(report=stuck))
        self.assertIn("Stopped at constitution", self.out)

    def test_exit_2_for_a_malformed_confirm_and_for_a_usage_error(self):
        self.assertEqual(2, self.start("--confirm", "bare"))
        self.assertIn("--confirm takes", self.err)
        self.assertEqual(2, self.start("--issue", "seven"))
        self.assertIn("--issue", self.err)

    def test_exit_3_prints_the_preview_and_the_confirm_line(self):
        self.assertEqual(3, self.start("--no-input"))
        self.assertRegex(self.out, r"confirm: first_sync@[0-9a-f]{12}")
        self.assertRegex(self.out, r"To do it, run:\npython3 scripts/bootstrap\.py start "
                                   r"--no-input --confirm first_sync@[0-9a-f]{12}\n")

    def test_guided_without_confirm_exits_2_with_the_tell_it_line(self):
        self.assertEqual(2, self.start("--guided"))
        self.assertIn("--guided", self.err)
        self.assertEqual("Go back to your coding agent and tell it what happened.\n", self.out)

    def test_guided_with_a_wrong_digest_exits_4_with_the_tell_it_line(self):
        self.assertEqual(4, self.start("--guided", "--confirm", "first_sync@0123456789ab"))
        self.assertTrue(self.out.endswith(
            "Go back to your coding agent and tell it what happened.\n"))

    def test_exit_4_when_the_digest_no_longer_matches(self):
        self.assertEqual(4, self.start("--no-input", "--confirm", "first_sync@0123456789ab"))
        self.assertIn("the preview changed since you confirmed it", self.err)

    def test_exit_130_when_interrupted(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(bootstrap_steps, "build_report", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrap.main(["start", "--no-input"], root=self.root,
                                  run=test_bootstrap_start.SyncRun(), is_tty=False)

        self.assertEqual(130, code)
        self.assertIn("Interrupted.", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_no_input_in_a_terminal_behaves_as_without_a_terminal(self):
        without = self.start("--no-input", tty=False)
        without_out = self.out

        def no_prompt(text):
            raise AssertionError(f"prompt called: {text}")

        with_terminal = self.start("--no-input", tty=True, prompt=no_prompt)

        self.assertEqual((3, without_out), (with_terminal, self.out))
        self.assertEqual(3, without)

    def test_a_terminal_without_no_input_asks(self):
        asked = []
        code = self.start(tty=True, prompt=lambda text: asked.append(text) or "n")

        self.assertEqual(1, code)
        self.assertEqual(["Proceed? [y/N] "], asked)

    def test_agents_saves_and_syncs_the_choice_through_the_runner(self):
        run = test_bootstrap_start.SyncRun(extra={
            ("git", "config", "--local", "agent-setup.tools"): "",
            ("python3", "scripts/agent_setup.py", "sync"): ""})

        code = self.start("--no-input", "--agents", "claude,copilot", run=run)

        self.assertEqual(0, code)  # without --confirm it ends here, with no preview
        self.assertIn("Go back to your coding agent and tell it the choice is saved.", self.out)
        self.assertNotIn("confirm:", self.out)
        self.assertIn(("git", "config", "--local", "agent-setup.tools", "claude,copilot"),
                      run.calls)
        self.assertIn(("python3", "scripts/agent_setup.py", "sync"), run.calls)
        self.assertIn("Coding agents: claude, copilot (saved in git config agent-setup.tools).",
                      self.out)
        self.assertIn("Generating the agent files…", self.out)
        self.assertNotIn("Generating the agent files…", self.err)

    def test_agents_with_an_unknown_name_exits_2_and_runs_nothing(self):
        run = test_bootstrap_start.SyncRun()

        self.assertEqual(2, self.start("--no-input", "--agents", "gemini", run=run))
        self.assertEqual([], run.calls)
        self.assertIn("claude, copilot, codex", self.err)

    def test_the_agents_option_is_listed_in_the_help(self):
        self.assertIn("--agents", bootstrap.build_parser().format_help())

    def test_the_guided_option_is_listed_in_the_help_usage(self):
        help_text = " ".join(bootstrap.build_parser().format_help().split())
        self.assertIn("[--issue N] [--guided]]", help_text)

    def test_the_full_dry_run_option_is_listed_in_the_help_usage_and_reaches_the_engine(self):
        help_text = " ".join(bootstrap.build_parser().format_help().split())
        self.assertIn("[--full-dry-run]", help_text)

        run = test_bootstrap_start.SyncRun(
            output="[dry-run] create EPIC: 001 First\n[dry-run] add #5 to project o/1\n")
        self.assertEqual(3, self.start("--no-input", "--full-dry-run", run=run))
        self.assertIn("[dry-run] create EPIC: 001 First", self.out)
        self.assertIn("--confirm first_sync@", self.out)
        self.assertRegex(self.out, r"first_sync@[0-9a-f]{12} --full-dry-run\n")

    def test_the_arguments_after_start_reach_the_engine_unchanged(self):
        argv = ["--no-input", "--confirm", "bootstrap_issue@0123456789ab", "--issue", "7",
                "--name", "n", "--description", "d", "--owner", "o", "--project-number", "3",
                "--required-approvals", "1"]
        runner = test_bootstrap_start.SyncRun(extra={
            ("git", "remote", "get-url", "origin"): "git@github-work:acme/widgets.git\n",
            ("git", "remote", "get-url", "--push", "--all", "origin"):
                "git@github.com:acme/widgets.git\ngit@github.com:other/fork.git\nnot a url\n"})
        prompt = lambda text: "y"  # noqa: E731
        with mock.patch.object(bootstrap_start, "main", return_value=0) as engine:
            code = bootstrap.main(["start", *argv], root=self.root, run=runner, is_tty=False,
                                  prompt=prompt)

        self.assertEqual(0, code)
        (passed,), kwargs = engine.call_args
        self.assertEqual(argv, passed)
        self.assertEqual(self.root, kwargs["root"])
        self.assertIs(runner, kwargs["run"])
        self.assertIs(prompt, kwargs["prompt"])
        self.assertIs(False, kwargs["is_tty"])
        self.assertIs(bootstrap.init, kwargs["init"])
        self.assertIs(bootstrap.validate, kwargs["validate"])
        self.assertIs(bootstrap.toml_string, kwargs["toml_string"])
        # the repository default is what `git remote get-url origin` says, parsed like `init` does
        self.assertEqual(("acme", "widgets"), kwargs["remote_default"]())
        self.assertIn(("git", "remote", "get-url", "origin"), runner.calls)
        # `git push` goes to every push URL, so all of them are read and parsed, in order
        self.assertEqual([("acme", "widgets"), ("other", "fork"), None],
                         kwargs["push_remote_default"]())
        self.assertIn(("git", "remote", "get-url", "--push", "--all", "origin"), runner.calls)

    def test_an_unreadable_or_empty_push_url_listing_is_one_unknown_remote(self):
        def failing(argv, cwd=None):
            raise bootstrap.CommandError("no origin", returncode=2)

        self.assertEqual([None], bootstrap.push_remote_defaults(self.root, failing))
        self.assertEqual([None], bootstrap.push_remote_defaults(self.root, lambda a, cwd=None: ""))

    def test_the_real_runner_gets_the_stream_keeping_dry_run_runner(self):
        with mock.patch.object(bootstrap_start, "main", return_value=0) as engine:
            bootstrap.main(["start"], root=self.root, is_tty=False)
            self.assertIs(bootstrap.run_command, engine.call_args.kwargs["run"])
            self.assertIs(bootstrap_start.run_captured, engine.call_args.kwargs["run_captured"])
            self.assertIs(bootstrap_start.run_timed, engine.call_args.kwargs["run_timed"])

            bootstrap.main(["start"], root=self.root, run=test_bootstrap_start.SyncRun(),
                           is_tty=False)
            self.assertIsNone(engine.call_args.kwargs["run_captured"])
            self.assertIsNone(engine.call_args.kwargs["run_timed"])

            injected = object()
            bootstrap.main(["start"], root=self.root, run=test_bootstrap_start.SyncRun(),
                           is_tty=False, run_captured=injected)
            self.assertIs(injected, engine.call_args.kwargs["run_captured"])

    def test_the_dry_runs_stderr_is_shown_when_start_runs_through_bootstrap(self):
        run = test_bootstrap_start.SyncRun(stderr="warning: ignoring a marker\n")

        code = self.start("--no-input", run=run, run_captured=run.captured)

        self.assertEqual(3, code)
        self.assertIn("warning: ignoring a marker", self.out)
        self.assertNotIn("warning: ignoring a marker", self.err)
        self.assertEqual("", self.err)  # a successful run leaves stderr empty

    def test_progress_lines_go_to_stdout_in_order_before_the_preview(self):
        run = test_bootstrap_start.SyncRun(stderr="warning: ignoring a marker\n")

        self.start("--no-input", run=run, run_captured=run.captured)

        lines = self.out.splitlines()
        self.assertEqual("Reading the repository and GitHub state…", lines[0])
        self.assertEqual("warning: ignoring a marker", lines[1])
        self.assertLess(self.out.index("warning:"), self.out.index("Next step"))

    def test_a_failure_is_on_stderr_and_not_on_stdout(self):
        code = self.start("--no-input", "--confirm", "first_sync@0123456789ab")

        self.assertEqual(4, code)
        self.assertIn("the preview changed since you confirmed it", self.err)
        self.assertNotIn("the preview changed", self.out)

    def test_an_error_escaping_start_exits_2_not_1(self):
        for error in (bootstrap.CommandError("boom"), OSError("disk"), ValueError("bad")):
            with self.subTest(error=error):
                err = io.StringIO()
                with mock.patch.object(bootstrap_start, "main", side_effect=error), \
                        contextlib.redirect_stderr(err):
                    code = bootstrap.main(["start"], root=self.root, run=FakeRun(), is_tty=False)

                self.assertEqual(2, code)
                self.assertIn(f"bootstrap start failed: {error}", err.getvalue())

    def test_start_has_no_late_import_of_bootstrap(self):
        source = Path(bootstrap_start.__file__).read_text()

        self.assertNotRegex(source, r"(?m)^\s+import bootstrap\s*(#.*)?$")


BIDI, ZWSP, BOM = chr(0x202E), chr(0x200B), chr(0xFEFF)


class TomlStringTest(unittest.TestCase):
    def test_every_output_unsafe_character_is_written_as_a_toml_unicode_escape(self):
        for code in (0x00, 0x1B, 0x7F, 0x85, 0x200B, 0x200D, 0x200E, 0x202E, 0x2028, 0x2066,
                     0xFEFF, 0xAD, 0xE0001):
            char = chr(code)
            with self.subTest(code=hex(code)):
                written = bootstrap.toml_string(f"a{char}b")

                self.assertFalse(raw_unsafe(written), repr(written))
                self.assertNotIn(char, written)
                self.assertEqual(f"a{char}b", tomllib.loads(f"k = {written}")["k"])

    def test_named_escapes_and_ordinary_text_are_unchanged(self):
        self.assertEqual('"a\\nb\\tc\\\\\\"d \u00e9 \U0001f600"',
                         bootstrap.toml_string('a\nb\tc\\"d \u00e9 \U0001f600'))


class EscapedMessagesTest(BootstrapTestCase):
    def test_the_prompt_default_is_escaped(self):
        self.root = self.root.parent / f"d{BIDI}x\\y"
        self.root.mkdir()
        self.target = self.root / "project-context.toml"

        self.init("--description", "D", "--owner", "acme", "--project-number", "1",
                  "--required-approvals", "1", run=FakeRun(remote=None), tty=True,
                  answers=[""])

        self.assertEqual(1, len(self.prompts))
        self.assertFalse(raw_unsafe(self.prompts[0]), repr(self.prompts[0]))
        self.assertIn("[d\\u202ex\\\\y]", self.prompts[0])

    def test_an_error_message_is_escaped(self):
        for command in ("init", "github", "check"):
            with self.subTest(command=command):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with mock.patch.object(bootstrap, "init",
                                           side_effect=ValueError(f"bad {BIDI}\x1b")), \
                         mock.patch.object(bootstrap_github, "run_github",
                                           side_effect=ValueError(f"bad {BIDI}\x1b")), \
                         mock.patch.object(bootstrap_check, "run_check",
                                           side_effect=ValueError(f"bad {BIDI}\x1b")):
                        code = bootstrap.main([command], root=Path(tempfile.gettempdir()),
                                              run=FakeRun(), is_tty=False)

                self.assertNotEqual(0, code)
                self.assertFalse(raw_unsafe(err.getvalue()), repr(err.getvalue()))
                self.assertIn(f"bootstrap {command} failed: bad \\u202e\\x1b", err.getvalue())

    def test_a_message_that_quotes_a_value_is_escaped_once(self):
        # Messages hold raw values; a backslash is doubled once, where the message is printed.
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = bootstrap.main(
                ["init", "--description", "D", "--project-number", "1",
                 "--owner", "a\\b" + BIDI], root=self.root, run=FakeRun(), is_tty=False)

        self.assertEqual(1, code)
        self.assertIn("--owner: 'a\\\\b" + "\\" + "u202e' must be", err.getvalue())

    def test_a_usage_error_of_the_top_level_parser_is_escaped_once(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            bootstrap.main(["github", "--bogus" + BIDI + "\\"], root=self.root,
                           run=FakeRun(), is_tty=False)

        self.assertEqual(2, raised.exception.code)
        self.assertTrue(err.getvalue().endswith(
            "error: unrecognized arguments: --bogus\\u202e\\\\\n"), err.getvalue())

    def test_an_invalid_subcommand_is_escaped_once(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            bootstrap.main(["x" + BIDI], root=self.root, run=FakeRun(), is_tty=False)

        self.assertIn("error: argument command: invalid choice: 'x\\u202e' (choose from",
                      err.getvalue())

    def test_a_line_feed_in_a_usage_error_stays_on_one_line(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            bootstrap.main(["github", "--bogus\nApplied 3 change(s)."], root=self.root,
                           run=FakeRun(), is_tty=False)

        self.assertTrue(err.getvalue().endswith(
            "error: unrecognized arguments: --bogus\\nApplied 3 change(s).\n"), err.getvalue())

    def test_the_check_json_failure_message_is_escaped(self):
        def build(root, run, remote_default, push_remote_default=None):
            raise RuntimeError(f"s{BIDI}\x1b")

        err = io.StringIO()
        with mock.patch.object(bootstrap_steps, "build_report", build), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = bootstrap.main(["check", "--json"], root=self.root, run=FakeRun(),
                                  is_tty=False)

        self.assertEqual(2, code)
        self.assertFalse(raw_unsafe(err.getvalue()), repr(err.getvalue()))
        self.assertIn("\\u202e\\x1b", err.getvalue())


if __name__ == "__main__":
    unittest.main()
