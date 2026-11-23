"""The guided-bootstrap `start` command: the generic machinery (research D4, D12, D13).

This module holds the preview block and digest, the driver loop and `--confirm` handling. The
per-step work plugs in through `ACTIONS`: one object per confirmable step with these methods
(all optional; `StepAction` supplies the defaults):

- `missing_flags(env, step)`: init flags still missing (only `project_context` has any);
- `invalid_values(env, step)`: a message when a given value is invalid, checked read-only
  before any preview;
- `prepare(env, step)`: in a terminal, asks for values the preview must show;
- `preview(env, step, candidate)`: the step's own preview lines;
- `commit_files(env, step)`: the paths the step will commit, hashed into the digest;
- `perform(env, step, candidate)`: the confirmed action; raises `StepFailed` to stop.

Every `gh` and `git` call goes through the injected `run`, and no write command runs before a
confirmation: previews and digests only read.
"""

import argparse
import contextlib
import errno
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import quote

import agent_setup
import bootstrap_github
import bootstrap_steps
import display_text
import project_board
import project_context

EXIT_OK, EXIT_STOPPED, EXIT_ERROR, EXIT_CONFIRM, EXIT_CHANGED, EXIT_INTERRUPTED = (
    0, 1, 2, 3, 4, 130)
DIGEST_LENGTH = 12
CONFIRM_VALUE = re.compile(r"(?P<step>[a-z][a-z_]*)@(?P<digest>[0-9a-f]{12})")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
NONE = "none"
NEW = "new"
ISSUE_STEP = "bootstrap_issue"
CREDENTIAL_STEP = "board_credential"


READING = "Reading the repository and GitHub state…"
UNSAFE_COMMAND = bootstrap_steps.UNSAFE_COMMAND_NOTE

# The one escaping of text `start` does not control (GitHub, git, the project's own settings):
# shown once, where it is printed, never again, because it also doubles every backslash.
_printable = display_text.printable


# A message of several lines: its line feeds stay, every other unsafe character is escaped.
_shown = display_text.printable_lines


def _command_lines(command, indent="  "):
    """The lines that show `command`: the command exactly as it is, or the note that it is left
    out when it holds an unsafe character. A command is never escaped: an escaped one would not
    run as shown."""
    if not command:
        return []
    if display_text.has_unsafe(command):
        return [f"{indent}{UNSAFE_COMMAND}"]
    return [f"{indent}Command: {command}"]


class StepFailed(Exception):
    """A confirmed action stopped; `code` is the exit code and `message` goes to stderr."""

    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.code = code

    def text(self):
        """The message, safe to print: it holds raw values and is escaped here, once."""
        return _shown(str(self))


class StepAction:
    """The defaults of a step action: nothing missing, nothing to hash, nothing to do yet."""

    def missing_flags(self, env, step):
        return []

    def invalid_values(self, env, step):
        return None

    def invalid_flags(self, env, step):
        """The flags whose given value fails the validation `invalid_values` runs, each with the
        validation message (never the value): `{flag: message}`, in flag order."""
        return {}

    def stop(self, env, step):
        """A reason to stop before any preview or digest, or None: `{"title", "text",
        "command"}` of the action the user must take first (a D12-style stop, exit 1)."""
        return None

    def explain(self, env, step):
        """Print what the engine's one-line action leaves out, just before a stop message (for
        `first_sync`, a failing dry run's output). Prints only; never changes anything."""

    def prepare(self, env, step):
        """In a terminal, ask for values the preview must show (before the preview)."""

    def preview(self, env, step, candidate=None):
        return []

    def commit_files(self, env, step):
        return []

    def perform(self, env, step, candidate=None):
        raise StepFailed(f"start cannot perform {step['id']} yet")

    def after_done(self, env, step):
        """Lines printed right after `Done: <step>: …` (already safe to print); none by default."""
        return []


ACTIONS = {}  # step id -> StepAction; each step's action registers itself here
_DEFAULT_ACTION = StepAction()


def discard_pending_input():
    """Throw away what was typed ahead on a terminal, so an answer is always one given after the
    preview was shown. A no-op without a terminal or without `termios` (Windows); it never fails
    the run."""
    try:
        import termios
    except ImportError:
        return
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, AttributeError, termios.error):
        pass


def _stderr_line(text=""):
    """An error line on stderr, after stdout was flushed so the two streams keep their order."""
    sys.stdout.flush()
    print(text, file=sys.stderr, flush=True)


def _stdout_line(text=""):
    """A progress line on stdout, flushed at once so a long step shows it while it runs."""
    print(text, flush=True)


def bold_text(text):
    """`text` in bold, only when stdout is a terminal and `NO_COLOR` is unset or empty, so
    agents and logs never get escape codes (FR-010e)."""
    try:
        terminal = sys.stdout.isatty()
    except (AttributeError, ValueError):
        terminal = False
    if terminal and not os.environ.get("NO_COLOR"):
        return f"\033[1m{text}\033[0m"
    return text


@dataclass
class Env:
    """What `start` reads and writes through: injected so tests never reach git or GitHub."""
    root: Path
    run: Callable
    remote_default: Callable
    # () -> [(owner, name) or None, ...], from `git remote get-url --push --all origin`;
    # None: the push URLs are not checked
    push_remote_default: Callable = None
    is_tty: bool = False
    prompt: Callable = input
    # () -> None: discards input typed before the prompt; called right before every prompt
    flush_input: Callable = discard_pending_input
    out: Callable = print
    err: Callable = _stderr_line
    bold: Callable = bold_text  # (text) -> text, bold on a terminal only (FR-010e)
    # (text) -> None: a plain progress line on stdout (FR-010c, research D19): stderr carries
    # only errors. Callers pass GitHub-sourced values through `_printable`.
    progress: Callable = _stdout_line
    actions: dict = field(default_factory=lambda: ACTIONS)
    report: Callable = None  # () -> the `check --json` document, read fresh each call
    default_branch: str = None  # from the latest report, set by the driver
    bootstrap_merged: bool = None  # likewise: whether bootstrap is merged on the default branch
    init: Callable = None  # bootstrap.init(args, root, run, is_tty, prompt), injected by `main`
    validate_context: Callable = None  # bootstrap.validate(context); raises ValueError
    toml_string: Callable = None  # bootstrap.toml_string(text): how a string is written
    args: object = None  # the parsed options, set by `run_start`
    session: dict = field(default_factory=dict)  # values a step gathered, keyed by step id
    clock: Callable = time.time  # seconds since the epoch; injected so polling never waits
    sleep: Callable = time.sleep
    # (argv, cwd) -> object with returncode, stdout and stderr; used for the sync's dry run only,
    # which is the one command whose stderr `start` shows. None: go through `run`, stdout only.
    run_captured: Callable = None
    # (argv, cwd, timeout) -> stdout like `run`, but raises TimeoutError after `timeout` seconds;
    # used for the `gh run view` calls that follow the first sync, so a hanging call cannot delay
    # the "Still running" line. None: go through `run`, with no timeout.
    run_timed: Callable = None
    _repo: str = None

    def __post_init__(self):
        if self.report is None:
            self.report = lambda: bootstrap_steps.build_report(
                self.root, self.run, self.remote_default, self.push_remote_default)

    def ask(self, text):
        """The answer to a prompt, after input typed before it was discarded."""
        self.flush_input()
        return self.prompt(text)

    def action_for(self, step_id):
        return self.actions.get(step_id, _DEFAULT_ACTION)

    def need(self, name):
        """The injected function `name`, or a clear error when the caller did not supply it."""
        function = getattr(self, name)
        if function is None:
            raise ValueError(f"Env.{name} was not supplied; bootstrap.py passes it to start")
        return function

    def repo(self):
        if self._repo is None:
            self._repo = bootstrap_github.repository_name(self.run, self.root, self.remote_default)
        return self._repo


# --- preview block and digest (research D4) ---------------------------------------------------


def strip_ansi(text):
    return ANSI.sub("", text)


class PreviewLines(list):
    """A step's preview lines. `digest_lines` (default: the lines themselves) is what the
    digest covers when that is more than what is shown (the `first_sync` summary, D19)."""

    digest_lines = None


class Block(str):
    """A preview block; `digest_text` is what the digest covers (the block itself by default)."""

    digest_text = None


def preview_block(env, step, candidate=None):
    """The step's preview, without the `confirm:` line, prompt or hints (research D4).

    The one function behind every preview, so a terminal run, an exit-3 run and a `--confirm` run
    show, and hash, the same text. The result's `digest_text` is what the digest covers.
    """
    lines = [f"Step {step['id']}: {_printable(step['title'])}"]
    if candidate is not None:
        branch = candidate.get("branch") or "no branch yet"
        lines.append(f"Bootstrap issue #{candidate['number']}: "
                     f"{_printable(candidate['title'])} ({_printable(branch)})")
    action = step.get("action") or {}
    if action.get("text"):
        lines.append(f"Action: {_printable(action['text'])}")
    shown = env.action_for(step["id"]).preview(env, step, candidate)
    hashed = [*lines, *(getattr(shown, "digest_lines", None) or shown)]
    lines.extend(shown)
    block = Block(strip_ansi("\n".join(lines)))
    block.digest_text = strip_ansi("\n".join(hashed))
    return block


def confirm_line(step_id, digest):
    return f"confirm: {step_id}@{digest}"


CONFIRM_LABELS = {
    "bootstrap_issue": "Create or use the bootstrap issue",
    "project_context": "Create project-context.toml",
    "github_settings": "Apply GitHub settings",
    "pull_request": "Commit, push and open the bootstrap pull request",
    "first_sync": "Run the first sync",
}


def confirm_label(step, candidate_number=None):
    """A fixed name for the step (or the chosen issue), shown above the `confirm:` line."""
    if candidate_number is not None:
        return f'Confirm "Use bootstrap issue #{candidate_number}":'
    return f'Confirm "{CONFIRM_LABELS[step["id"]]}":'


def _show_confirm(env, step, digest, candidate_number=None):
    env.out(confirm_label(step, candidate_number))
    env.out(confirm_line(step["id"], digest))


def _read(env, argv):
    return env.run(argv, cwd=env.root).strip()


class RefUnreadable(Exception):
    """A strict read of a ref could not be done; the message says what could not be read."""


def _tip(env, branch, strict=False):
    """The branch's tip on GitHub, `none` when it is not there or cannot be read.

    Strict: only an HTTP 404 means "not there" (as detection does); any other error raises
    `RefUnreadable`.
    """
    if not branch:
        return NONE
    try:
        return env.run(["gh", "api", f"repos/{env.repo()}/branches/{quote(branch, safe='/')}",
                        "--jq", ".commit.sha"]).strip() or NONE
    except bootstrap_github.CommandError as error:
        if strict and "HTTP 404" not in str(error):
            raise RefUnreadable(f"cannot read the tip of {branch} ({error})") from error
        return NONE


def read_refs(env, default_branch, step_id=None, candidate=None, strict=False):
    """HEAD, the default tip, the open PR's head and the branch tip, `none` where not applicable.

    The branch is the checked-out one when it is a bootstrap branch. For `bootstrap_issue` it is
    the chosen candidate's own branch instead (research D4), which may not be checked out; a
    candidate without a branch has none.
    Strict reads raise `RefUnreadable` instead of answering `none` for an error that is not a 404.
    """
    head = _read(env, ["git", "rev-parse", "HEAD"])
    default_tip = _tip(env, default_branch, strict)
    if step_id == ISSUE_STEP:
        branch = (candidate or {}).get("branch")
    else:
        current = _read(env, ["git", "branch", "--show-current"])
        branch = current if bootstrap_steps.BRANCH_PATTERN.match(current) else None
    pr_head = branch_tip = NONE
    if branch:
        try:
            listing = json.loads(env.run(
                ["gh", "pr", "list", "--repo", env.repo(), "--head", branch, "--state", "open",
                 "--json", "headRefOid"]))
            pr_head = listing[0]["headRefOid"] if listing else NONE
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError,
                IndexError) as error:
            if strict:
                raise RefUnreadable(
                    f"cannot read the open pull requests of {branch} ({error})") from error
            pr_head = NONE
        if pr_head == NONE:
            branch_tip = _tip(env, branch, strict)
    return [head, default_tip, pr_head, branch_tip]


def file_hashes(env, paths):
    """`git hash-object` of each path in path order, `deleted` for one that is gone."""
    hashes = []
    for path in sorted(paths):
        if os.path.lexists(Path(env.root) / path):  # a symlink counts even if its target is gone
            hashes.append(_read(env, ["git", "hash-object", path]))
        else:
            hashes.append("deleted")
    return hashes


def compute_digest(env, step, block, files, issue=None, candidate=None):
    """First 12 hex characters of SHA-256 over the inputs of research D4, one per line."""
    lines = [step["id"]]
    if step["id"] == ISSUE_STEP:
        lines.append(str(issue if issue is not None else NEW))
    lines.extend((getattr(block, "digest_text", None) or block).split("\n"))
    lines.extend(file_hashes(env, files))
    lines.extend(read_refs(env, env.default_branch, step["id"], candidate))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


# --- the driver loop (contracts/start-cli.md, research D12) ------------------------------------

COMPLETE = "BOOTSTRAP IS COMPLETE"
COMPLETE_MANUAL = "BOOTSTRAP IS COMPLETE; MANUAL SETTINGS REMAIN (SEE ABOVE)"
START_COMMAND = "python3 scripts/bootstrap.py start --no-input"
INIT_PLACEHOLDERS = {
    "--name": "--name '<name>'",
    "--description": "--description '<description>'",
    "--owner": "--owner <owner>",
    "--project-number": "--project-number <number>",
    "--required-approvals": "--required-approvals <number>",
    "--project-number or --create-board": "(--project-number <number> | --create-board)",
}
CHANGED = ("the preview changed since you confirmed it; run `start` again to see the new one")


def _print_action(env, step):
    action = step.get("action") or {}
    if action.get("text"):
        env.out(f"  {_printable(action['text'])}")  # the engine may quote GitHub-sourced text
    for line in _command_lines(action.get("command")):
        env.out(line)
    if action.get("doc"):
        env.out(f"  See {action['doc']}")


def _show_step(env, step):
    env.out(f"{step['id']} ({step['state']}): {_printable(step['title'])}")
    _print_action(env, step)


def _stop(env, step):
    """A blocking step that `start` does not perform: print the action and its doc link."""
    env.action_for(step["id"]).explain(env, step)
    env.out(f"Stopped at {step['id']}: {_printable(step['title'])}")
    _print_action(env, step)
    return EXIT_STOPPED


def _blocked(env, step):
    """The exit code when the step's action asks to stop first (before any preview), else None."""
    reason = env.action_for(step["id"]).stop(env, step)
    if reason is None:
        return None
    env.out(f"Stopped at {step['id']}: {_printable(reason['title'])}")
    env.out(f"  {_printable(reason['text'])}")
    for line in _command_lines(reason.get("command")):
        env.out(line)
    return EXIT_STOPPED


def _complete(env, report):
    manual = report.get("result") == "complete_with_manual_settings"
    env.out("")
    env.out(env.bold(COMPLETE_MANUAL if manual else COMPLETE))


def _preview(env, step, candidate, issue):
    """(block, digest): both from the same inputs, so they always agree."""
    action = env.action_for(step["id"])
    block = preview_block(env, step, candidate)
    return block, compute_digest(
        env, step, block, action.commit_files(env, step), issue, candidate)


def _perform(env, step, candidate):
    """Run the confirmed action and print `Done: <step>: <result>.`; None when it finished, else
    the exit code. The action returns its one-line result, already safe to print (D16)."""
    try:
        result = env.action_for(step["id"]).perform(env, step, candidate)
    except StepFailed as error:
        env.err(error.text())
        return error.code
    except bootstrap_github.CommandError as error:
        env.err(_shown(f"{step['id']} failed: {error}"))
        return EXIT_ERROR
    env.out(f"Done: {step['id']}: {(result or 'done').rstrip('.')}.")
    for line in env.action_for(step["id"]).after_done(env, step):
        env.out(line)
    return None


def _announce(env, step):
    """The blank line and the `Next step` line that open every preview (FR-010d)."""
    env.out("")
    env.out(f"Next step (not done yet): {step['id']}")


def _init_flags(env, invalid=()):
    """The init flags that were given, as shell-quoted tokens (the command must repeat them).

    A flag in `invalid` is left out: its given value must never be echoed. So is a value with a
    character `init` refuses (a newline, an escape, any format character but the three below),
    whatever `invalid` says, so such a value can never reach a printed command.

    The one exception to "a printed command is never escaped and holds no unsafe character": a
    value may still hold a zero-width joiner, a zero-width non-joiner or a soft hyphen (the only
    format characters `init` accepts, for emoji sequences and some scripts). They are the user's
    own values, repeated unchanged and shell-quoted, so the command runs with the same value."""
    args, flags = env.args, []
    for name in ("name", "description", "owner"):
        value = getattr(args, name, None)
        if (value is not None and f"--{name}" not in invalid
                and not any(display_text.is_output_unsafe(char)
                            and char not in display_text.INPUT_ALLOWED_FORMAT_CHARACTERS
                            for char in value)):
            flags.append(f"--{name} {shlex.quote(value)}")
    if getattr(args, "project_number", None) is not None and "--project-number" not in invalid:
        flags.append(f"--project-number {args.project_number}")
    if getattr(args, "create_board", False):
        flags.append("--create-board")
    if (getattr(args, "required_approvals", None) is not None
            and "--required-approvals" not in invalid):
        flags.append(f"--required-approvals {args.required_approvals}")
    return flags


def _confirm_command(env, step, digest, issue=None, named=False):
    """The full command that confirms this preview, to print after `To do it, run:`."""
    parts = [START_COMMAND, _confirm_flags(step, digest, issue, named)]
    if step["id"] == PROJECT_CONTEXT:  # the confirmed run recomputes the digest from the flags
        parts.extend(_init_flags(env))
    if step["id"] == FIRST_SYNC and getattr(env.args, "full_dry_run", False):
        parts.append("--full-dry-run")
    if step["id"] == PULL_REQUEST and getattr(env.args, "review_notes", False):
        parts.append("--review-notes")  # the confirmed run recomputes the digest with the notes
    return " ".join(parts)


def _invalid_message(env, step, invalid):
    """The stop message for an invalid given value: each flag with its reason, never the value
    (contract check 5b). Falls back to `invalid` only when no flag was given (a defaulted value)."""
    flags = env.action_for(step["id"]).invalid_flags(env, step)
    if not flags:
        return _printable(f"{step['id']} failed: {invalid}")
    reasons = "; ".join(f"{flag}: {message}" for flag, message in flags.items())
    return _printable(f"{step['id']} failed: {reasons}")


def _decisions_needed(env, step, missing):
    """Stop (exit 1) without a preview when init flags are missing and nobody can be asked."""
    invalid = env.action_for(step["id"]).invalid_flags(env, step)
    needed = [*missing, *(flag for flag in invalid if flag not in missing)]
    env.out("")
    env.out(f"Next step {step['id']} needs your decisions: {', '.join(needed)}")
    for flag, message in invalid.items():  # the reason, never the rejected value
        env.out(_printable(f"{flag}: {message}"))
    env.out("To give them, run:")
    env.out(" ".join([START_COMMAND, *_init_flags(env, invalid),
                      *(INIT_PLACEHOLDERS.get(name, name) for name in needed)]))
    return EXIT_STOPPED


def _remember(env, report):
    """Keep what later steps read from the latest report: the branch, whether bootstrap is
    merged, and the board credential step when it is not done (it is printed once, after
    `github_settings`, never as a step of its own)."""
    env.default_branch = report.get("default_branch")
    env.bootstrap_merged = report.get("bootstrap_merged")
    env.session.pop(CREDENTIAL_NOTE, None)
    for found in report.get("steps", ()):
        if found["id"] == CREDENTIAL_STEP and found["state"] != "done":
            env.session[CREDENTIAL_NOTE] = found


def _current_step(env, step_id):
    """Checks (2) to (4) of the contract on a fresh report: (exit code or None, the step).

    Used for `--confirm`, and again after a terminal yes, so a step that changed while the
    prompt waited is never performed.
    """
    env.progress(READING)
    report = env.report()
    _remember(env, report)
    next_id = report.get("next_step")
    if next_id is None:  # (2)
        _complete(env, report)
        return EXIT_OK, None
    step = next((found for found in report["steps"] if found["id"] == next_id), None)
    if step is None:
        env.err(f"the report names an unknown next step: {next_id}")
        return EXIT_ERROR, None
    if step_id != next_id:  # (3)
        env.err(f"next step is {next_id}, not {step_id}")
        return EXIT_CHANGED, None
    if not step["needs_confirmation"]:
        env.err(f"next step {next_id} needs no confirmation")
        return EXIT_CHANGED, None
    if not step["performable"]:  # (4)
        return _stop(env, step), None
    return None, step


def _confirm_flags(step, digest, issue, named):
    """The `--confirm` (and `--issue`) arguments that confirm this preview.

    `--issue N` is part of it for a chosen issue number when `--issue` was given or several
    candidates exist, because the rerun needs it to pick the same candidate.
    """
    flags = f"--confirm {step['id']}@{digest}"
    if step["id"] == ISSUE_STEP and issue not in (None, NEW):
        candidates = (step.get("details") or {}).get("candidates") or []
        if named or len(candidates) > 1:
            flags += f" --issue {issue}"
    return flags


def _confirm_step(env, args, step, interactive, candidate=None, issue=None):
    """Preview, then ask (terminal) or exit 3; None when the step was performed."""
    block, digest = _preview(env, step, candidate, issue)
    _announce(env, step)
    env.out(block)
    if not interactive:
        env.out("To do it, run:")
        env.out(_confirm_command(env, step, digest, issue, args.issue is not None))
        _show_confirm(env, step, digest)
        return EXIT_CONFIRM
    _show_confirm(env, step, digest)
    try:
        answer = env.ask("Proceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer != "y":
        env.out("Nothing was changed.")
        return EXIT_STOPPED
    code, step = _current_step(env, step["id"])  # the report may have changed meanwhile
    if code is not None:
        return code
    code = _blocked(env, step)  # the bootstrap may have merged while the prompt waited
    if code is not None:
        return code
    if step["id"] == ISSUE_STEP:  # the chosen candidate itself may have changed meanwhile
        fresh = (step.get("details") or {}).get("candidates") or []
        if issue == NEW:
            candidate = None
            same = not fresh
        else:
            candidate = next((found for found in fresh if str(found["number"]) == issue), None)
            same = candidate is not None
        if not same:
            env.err(CHANGED)
            return EXIT_CHANGED
    if _preview(env, step, candidate, issue)[1] != digest:
        env.err(CHANGED)
        return EXIT_CHANGED
    return _perform(env, step, candidate)


AGENTS_HANDLED = "agents_handled"  # `Env.session` key: the choice was saved or asked this run
GENERATING = "Generating the agent files…"
AGENTS_SAVED = "Go back to your coding agent and tell it the choice is saved."


def _parse_agents(value):
    """The tools named by `--agents`, in `agent_setup.TOOLS` order; None when the value is empty,
    has an empty item or names an unknown tool."""
    names = value.split(",")
    if any(name not in agent_setup.TOOLS for name in names):
        return None
    return [tool for tool in agent_setup.TOOLS if tool in names]


def _wants_agent_question(env, args, report):
    """Whether the first report of an interactive, unguided run says no choice is saved."""
    if getattr(args, "guided", False) or env.session.get(AGENTS_HANDLED):
        return False
    env.session[AGENTS_HANDLED] = True  # only the first state read of the run is considered
    for found in report.get("steps", ()):
        details = found.get("details") or {}
        if found["id"] == "prerequisites" and "agent_tools" in details:
            return details["agent_tools"] is None
    return False


def _ask_agents(env):
    """Ask which coding agents are used and apply the answer: None to go on, else an exit code."""
    interrupted = []

    def ask(text):  # `ask_tools` swallows Ctrl-C; note it so it can stop `start` (exit 130)
        try:
            return env.ask(text)
        except KeyboardInterrupt:
            interrupted.append(True)
            raise

    tools = agent_setup.ask_tools(ask)
    if tools is None:  # nothing is saved, like the other prompts
        if interrupted:
            raise KeyboardInterrupt
        return EXIT_STOPPED  # end of input
    return _apply_agents(env, tools)


def _apply_agents(env, tools):
    """Save the choice in the local git config, generate the agent files and say so: None when
    done, else the exit code (the saved choice stays when the sync fails)."""
    env.session[AGENTS_HANDLED] = True
    env.progress(GENERATING)
    try:
        env.run(["git", "config", "--local", "agent-setup.tools", ",".join(tools)],
                cwd=env.root)
        env.run(["python3", "scripts/agent_setup.py", "sync"], cwd=env.root)
    except bootstrap_github.CommandError as error:
        # agent_setup.py prints its errors on stdout, which the CommandError message lacks
        detail = [_printable(line) for line in
                  [str(error), *_lines(getattr(error, "stdout", None) or "")]]
        env.err("Could not set up the coding agents: " + "\n".join(detail))
        return EXIT_ERROR
    env.out(f"Coding agents: {', '.join(tools)} (saved in git config agent-setup.tools).")
    env.out(agent_setup.RERUN_HINT)
    return None


def _drive(env, args, acted=()):
    interactive = env.is_tty and not args.no_input
    shown, acted = set(), set(acted)
    while True:
        env.progress(READING)
        report = env.report()
        if interactive and _wants_agent_question(env, args, report):
            env.session[AGENTS_HANDLED] = True  # once per run, whatever the answer
            code = _ask_agents(env)
            if code is not None:
                return code
            env.progress(READING)
            report = env.report()
        _remember(env, report)
        for step in report["steps"]:
            if step["state"] == "done":
                continue
            if not step["blocking"]:
                if step["id"] not in shown and step["id"] != CREDENTIAL_STEP:
                    shown.add(step["id"])
                    _show_step(env, step)
                continue
            if step["id"] in acted:
                env.out(f"{step['id']} is still not done after its action.")
                return _stop(env, step)
            if not (step["needs_confirmation"] and step["performable"]):
                return _stop(env, step)
            code = _blocked(env, step)
            if code is not None:
                return code
            missing = env.action_for(step["id"]).missing_flags(env, step)
            if missing and not interactive:
                return _decisions_needed(env, step, missing)
            invalid = env.action_for(step["id"]).invalid_values(env, step)
            if invalid:
                env.err(_invalid_message(env, step, invalid))
                return EXIT_ERROR
            if interactive:
                try:
                    env.action_for(step["id"]).prepare(env, step)
                except StepFailed as error:
                    env.err(error.text())
                    return error.code
            candidate, issue, code = _choose_candidate(env, args, step, interactive)
            if code is not None:
                return code
            outcome = _confirm_step(env, args, step, interactive, candidate, issue)
            if outcome is not None:
                return outcome
            acted.add(step["id"])
            break
        else:
            _complete(env, report)
            return EXIT_OK


# --- candidates, `--issue` and `--confirm` (contracts/start-cli.md, research D3, D4) -----------


def _list_candidates(env, step, candidates):
    """Every candidate with its own preview and `confirm:` line; exit 3 (research D3)."""
    _announce(env, step)
    env.out(f"{len(candidates)} bootstrap issues found; choose one:")
    for candidate in candidates:
        number = str(candidate["number"])
        block, digest = _preview(env, step, candidate, number)
        env.out("")
        env.out(block)
        env.out("To do it, run:")
        env.out(_confirm_command(env, step, digest, number, named=True))
        _show_confirm(env, step, digest, number)
    return EXIT_CONFIRM


def _choose_candidate(env, args, step, interactive):
    """(candidate, issue id, exit code): the issue `bootstrap_issue` acts on, or why not yet.

    No candidate means a new issue (`new`); one candidate is used unless `--issue` names another
    number; several need a valid `--issue`, or an answer at the prompt in a terminal. Other steps
    have no candidate, and a stray `--issue` is ignored.
    """
    if step["id"] != ISSUE_STEP:
        return None, None, None
    candidates = (step.get("details") or {}).get("candidates") or []
    if not candidates:
        if args.issue is None:
            return None, NEW, None
        # a number that is not a candidate is never ignored: that would create a duplicate issue
        env.out(f"#{args.issue} is not a bootstrap issue candidate; there is none. Nothing was "
                f"changed. Rerun without --issue to create a new bootstrap issue.")
        return None, None, EXIT_CONFIRM
    wanted = args.issue
    if wanted is None and len(candidates) == 1:
        wanted = candidates[0]["number"]
    chosen = next((found for found in candidates if found["number"] == wanted), None)
    if chosen is None and interactive:
        chosen = _ask_candidate(env, candidates)
        if chosen is None:
            return None, None, EXIT_STOPPED
    if chosen is None:
        return None, None, _list_candidates(env, step, candidates)
    return chosen, str(chosen["number"]), None


def _ask_candidate(env, candidates):
    for found in candidates:
        branch = found.get("branch") or "no branch yet"
        env.out(f"  #{found['number']}: {_printable(found['title'])} ({_printable(branch)})")
    try:
        answer = env.ask("Which bootstrap issue should be used? ").strip().lstrip("#")
    except EOFError:
        answer = ""
    chosen = next((found for found in candidates if str(found["number"]) == answer), None)
    if chosen is None:
        env.out("That is not one of the listed issues. Nothing was changed.")
    return chosen


def _confirmed(env, args, value):
    """Run the checks of the contract, in order, for `--confirm`: (exit code or None, step id).

    None means the confirmed step was performed and `start` continues. Nothing changes before
    the last check.
    """
    match = CONFIRM_VALUE.fullmatch(value)  # (1), also checked early in `_run_start`
    step_id, given = match.group("step"), match.group("digest")
    code, step = _current_step(env, step_id)  # (2) to (4)
    if code is not None:
        return code, None
    code = _blocked(env, step)
    if code is not None:
        return code, None
    missing = env.action_for(step_id).missing_flags(env, step)  # (5)
    if missing:
        env.err(f"{step_id} needs: {', '.join(missing)}")
        return EXIT_ERROR, None
    invalid = env.action_for(step_id).invalid_values(env, step)
    if invalid:
        env.err(_invalid_message(env, step, invalid))
        return EXIT_ERROR, None
    candidate, issue, code = _choose_candidate(env, args, step, False)  # (6)
    if code is not None:
        return code, None
    if _preview(env, step, candidate, issue)[1] != given:  # (7)
        env.err(CHANGED)
        return EXIT_CHANGED, None
    return _perform(env, step, candidate), step_id  # (8)


GUIDED_DONE = "Done. Go back to your coding agent."
GUIDED_FAILED = "Go back to your coding agent and tell it what happened."


def _guided_end(env, code):
    """The closing line `--guided` adds for the guide (FR-010b): after a performed step or an
    already complete bootstrap (exit 0), or after any stop or error (exits 1 to 4)."""
    if code == EXIT_OK:
        env.out(GUIDED_DONE)
    elif code in (EXIT_STOPPED, EXIT_ERROR, EXIT_CONFIRM, EXIT_CHANGED):
        env.out(GUIDED_FAILED)
    return code


def _run_start(env, args):
    if getattr(args, "guided", False) and args.confirm is None:
        env.err("--guided only works with --confirm <step-id>@<digest>")  # before any state read
        return EXIT_ERROR
    if args.confirm is not None and not CONFIRM_VALUE.fullmatch(args.confirm):  # (1), no state
        env.err(_printable(f"--confirm takes <step-id>@<{DIGEST_LENGTH} hex characters>, copied "
                           f"from the `confirm:` line of a preview (got '{args.confirm}')"))
        return EXIT_ERROR
    agents = getattr(args, "agents", None)
    if agents is not None:
        tools = _parse_agents(agents)
        if tools is None:  # before any state read
            env.err(_printable(f"--agents takes comma-separated names from "
                               f"{', '.join(agent_setup.TOOLS)} (got '{agents}')"))
            return EXIT_ERROR
        code = _apply_agents(env, tools)
        if code is not None:
            return code
        if args.confirm is None and not (env.is_tty and not args.no_input):
            # the guide's run: only the choice was asked for; no state read, no preview
            env.out(AGENTS_SAVED)
            return EXIT_OK
    acted = ()
    if args.confirm is not None:
        code, step_id = _confirmed(env, args, args.confirm)
        if code is not None:
            return code
        if getattr(args, "guided", False):  # only the confirmed step; the guide shows the next
            return EXIT_OK
        acted = (step_id,)
    return _drive(env, args, acted)


def run_start(env, args):
    """The `start` command: its exit code (contracts/start-cli.md)."""
    env.args = args
    try:
        code = _run_start(env, args)
    except StepFailed as error:  # a preview or a stop check that could not run
        env.err(error.text())
        code = error.code
    return _guided_end(env, code) if getattr(args, "guided", False) else code


# --- the bootstrap_issue action (research D3) --------------------------------------------------

ISSUE_TITLE = "CHORE: bootstrap the project"  # the title docs/bootstrap.md uses
ISSUE_BODY = (f"{bootstrap_steps.BOOTSTRAP_MARKER}\n\n"
              "Tracks the one-time bootstrap of this repository: project context, constitution, "
              "roadmap, GitHub settings and the bootstrap pull request.\n")
ISSUE_NUMBER = re.compile(r"/issues/(\d+)")


class BootstrapIssueAction(StepAction):
    """Create, develop or check out the bootstrap issue's branch (research D3)."""

    def _local_branches(self, env):
        return env.run(["git", "branch", "--format=%(refname:short)"], cwd=env.root).split()

    def _carried_over(self, env):
        status = env.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                         cwd=env.root)
        return bootstrap_steps.changed_entries(status)

    def stop(self, env, step):
        """`git fetch` and `git switch` use `origin`, `gh issue create` and `gh issue develop`
        the repository `gh` uses: they must be the same one."""
        return remote_mismatch(env, bootstrap_steps.MISMATCH_ISSUE)

    def preview(self, env, step, candidate=None):
        base = env.default_branch
        if candidate is None:
            lines = [f'Will create the issue "{ISSUE_TITLE}" with the bootstrap marker, then '
                     f"develop its branch <number>-chore-bootstrap from {base} and check it out."]
        elif candidate.get("branch") is None:
            number = candidate["number"]
            lines = [f"Will develop #{number}'s branch {number}-chore-bootstrap from {base} "
                     "and check it out."]
        else:
            branch = candidate["branch"]
            quoted = shlex.quote(branch)
            fetch = "" if branch in self._local_branches(env) else (
                f"fetch it with `git fetch origin {quoted}` and ")
            lines = [f"Will {fetch}check out {_printable(branch)} with `git switch {quoted}`."]
        lines[:0] = repository_head(env)
        entries = self._carried_over(env)
        files = bootstrap_steps.BOOTSTRAP_FILES
        untracked = [path for xy, path in entries if xy == "??" and path not in files]
        carried = [path for xy, path in entries if not (xy == "??" and path not in files)]
        if untracked:
            lines.append("Untracked files, carried over and not committed: "
                         f"{_printable(', '.join(untracked))}.")
        if carried:
            lines.append(f"Carried over to the branch: {_printable(', '.join(carried))}. If the "
                         "branch already commits the same files, the checkout can fail; start "
                         "never stashes.")
        return lines

    def perform(self, env, step, candidate=None):
        created = candidate is None
        if created:
            candidate = {"number": self._create(env), "branch": None}
        number, branch = candidate["number"], candidate.get("branch")
        if branch is None:
            env.progress("Creating and checking out its branch…")
            env.run(["gh", "issue", "develop", str(number), "--base", env.default_branch,
                     "--name", f"{number}-chore-bootstrap", "--checkout", "--repo", env.repo()],
                    cwd=env.root)
            if created:
                return f"Created issue #{number} and branch {number}-chore-bootstrap"
            return f"Created branch {number}-chore-bootstrap"
        try:
            if branch not in self._local_branches(env):
                env.progress(f"Fetching {_printable(branch)}…")
                env.run(["git", "fetch", "origin", branch], cwd=env.root)
            env.progress(f"Checking out {_printable(branch)}…")
            env.run(["git", "switch", branch], cwd=env.root)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{ISSUE_STEP} failed: {error}", EXIT_ERROR) from error
        return f"Checked out {_printable(branch)}"

    def _create(self, env):
        env.progress("Creating the bootstrap issue…")
        created = env.run(["gh", "issue", "create", "--title", ISSUE_TITLE, "--body", ISSUE_BODY,
                           "--repo", env.repo()], cwd=env.root)
        found = ISSUE_NUMBER.search(created)
        if not found:
            raise StepFailed(
                f"{ISSUE_STEP} failed: could not read the issue number from: "
                f"{created.strip() or 'no output'}; the "
                "issue was probably created, and rerunning start finds it by its marker")
        return int(found.group(1))


ACTIONS[ISSUE_STEP] = BootstrapIssueAction()


# --- the project_context action (research D13, D14) --------------------------------------------

PROJECT_CONTEXT = "project_context"
INIT_FIELDS = ("name", "description", "owner", "project_number", "create_board",
               "required_approvals")


class ProjectContextAction(StepAction):
    """Write a missing `project-context.toml` with the existing `init` (never `--force`).

    Values come from the init flags; in a terminal `prepare` asks for the missing decisions
    first, so the preview shows every value that will be written (research D13).
    """

    def _values(self, env):
        gathered = env.session.get(PROJECT_CONTEXT)
        if gathered is not None:
            return dict(gathered)
        return {key: getattr(env.args, key, None) for key in INIT_FIELDS}

    def _resolved(self, env):
        """The values with the repository defaults for `--name` and `--owner` filled in."""
        values = self._values(env)
        if "remote" not in env.session:  # read once per run
            env.session["remote"] = env.remote_default()
        remote = env.session["remote"]
        if values["name"] is None:
            values["name"] = remote[1] if remote else env.root.name
        if values["owner"] is None and remote:
            values["owner"] = remote[0]
        return values

    def missing_flags(self, env, step):
        values = self._resolved(env)
        missing = []
        if values["description"] is None:
            missing.append("--description")
        if values["owner"] is None:
            missing.append("--owner")
        if values["project_number"] is None and not values["create_board"]:
            missing.append("--project-number or --create-board")
        if values["required_approvals"] is None:
            missing.append("--required-approvals")
        return missing

    def _failure(self, env, values):
        """The shared validation's error for the values, or None (read-only, research D13).

        The context is built the way `init` builds it; a value still missing is replaced by
        a valid placeholder, so only a value that is present can fail.
        """
        def given(key, placeholder):
            return placeholder if values[key] is None else values[key]

        context = {
            "project": {"name": values["name"], "description": given("description", "x")},
            "github_project": {"owner": given("owner", "x"),
                               "number": given("project_number", 1)},
            "review": {"required_approvals": given("required_approvals", 0)},
        }
        validate = env.need("validate_context")  # outside the try: a missing one is not invalid
        try:
            validate(context)
        except ValueError as error:
            return error
        return None

    def _error(self, env, values):
        """The message of `_failure`, or None."""
        error = self._failure(env, values)
        return None if error is None else str(error)

    def invalid_values(self, env, step):
        return self._error(env, self._resolved(env))

    def invalid_flags(self, env, step):
        """Every given init flag whose value fails the shared validation on its own, with the
        requirement it breaks, never the value: `{flag: requirement}`, in flag order."""
        given = self._values(env)
        base = dict.fromkeys(INIT_FIELDS)
        base.update(name="x", create_board=False)
        invalid = {}
        for key in ("name", "description", "owner", "project_number", "required_approvals"):
            error = (None if given[key] is None
                     else self._failure(env, {**base, key: given[key]}))
            if error is not None:
                flag = f"--{key.replace('_', '-')}"
                invalid[flag] = self._reason(error, flag, given[key])
        return invalid

    @staticmethod
    def _reason(error, flag, value):
        """The requirement the error names, without the rejected value. `init`'s validation
        raises `InvalidValue`, which carries it; any other `ValueError` (the shared parser's
        `<section>.<key> <requirement>`) loses its leading key. `is not valid` when nothing
        is left."""
        if isinstance(error, project_context.InvalidValue):
            return error.requirement or "is not valid"
        return str(error).partition(" ")[2] or "is not valid"

    def _ask(self, env, text, flag):
        try:
            return env.ask(text).strip()
        except EOFError:
            raise StepFailed(f"{PROJECT_CONTEXT} failed: input ended: pass {flag}",
                             EXIT_ERROR) from None

    def _ask_valid(self, env, values, key, text, flag, convert=str):
        """Ask until the answer converts and passes the shared validation."""
        while True:
            answer = self._ask(env, text, flag)
            try:
                value = convert(answer)
            except ValueError:
                env.out(_printable(f"{flag}: '{answer}' must be a whole number"))
                continue
            problem = self._error(env, {**values, key: value})
            if problem is None:
                return value
            env.out(_printable(problem))

    def prepare(self, env, step):
        values = self._resolved(env)
        if values["description"] is None:
            values["description"] = self._ask_valid(
                env, values, "description", "Project description: ", "--description")
        if values["owner"] is None:
            values["owner"] = self._ask_valid(
                env, values, "owner", "GitHub owner: ", "--owner")
        if values["project_number"] is None and not values["create_board"]:
            number = self._ask_valid(
                env, values, "project_number", "GitHub Project number (empty to create one): ",
                "--project-number", convert=lambda answer: int(answer) if answer else None)
            values["project_number"], values["create_board"] = number, number is None
        if values["required_approvals"] is None:
            values["required_approvals"] = self._ask_valid(
                env, values, "required_approvals",
                "Required approvals (0 for a solo project) [0]: ", "--required-approvals",
                convert=lambda answer: int(answer) if answer else 0)
        env.session[PROJECT_CONTEXT] = values

    def preview(self, env, step, candidate=None):
        values = self._resolved(env)
        quote_text = env.need("toml_string")  # strings are shown exactly as they are written

        def shown(value):
            return "(missing)" if value is None else quote_text(value)

        board = (f"a new board named {shown(values['name'])} will be created"
                 if values["create_board"] else f"#{values['project_number']}")
        lines = [f"Will write {bootstrap_steps.CONTEXT_FILE} with:",
                 f"  name = {shown(values['name'])}",
                 f"  description = {shown(values['description'])}",
                 f"  owner = {shown(values['owner'])}",
                 f"  project board = {board}",
                 f"  required_approvals = {values['required_approvals']}"]
        warning = (step.get("details") or {}).get("ruleset_warning")
        if warning:
            lines.append(f"Ruleset warning: {_printable(warning)}")
        return lines

    def perform(self, env, step, candidate=None):
        values = self._resolved(env)
        missing = self.missing_flags(env, step)
        if missing:  # init must never fill in a decision value, whatever the caller checked
            raise StepFailed(f"{PROJECT_CONTEXT} failed: needs {', '.join(missing)}", EXIT_ERROR)
        init = env.need("init")
        arguments = argparse.Namespace(**values, force=False, yes=True)
        env.progress(f"Writing {bootstrap_steps.CONTEXT_FILE}…")
        written = io.StringIO()
        try:
            with contextlib.redirect_stdout(written):
                init(arguments, env.root, env.run, False, env.prompt)
        except (ValueError, OSError) as error:
            raise StepFailed(f"{PROJECT_CONTEXT} failed: {error}", EXIT_ERROR) from error
        # init's own next steps are start's job: echo only its "Wrote <path>" line
        wrote = next((line for line in written.getvalue().splitlines()
                      if line.startswith("Wrote ")), None)
        # `init` escapes the path in its "Wrote" line itself
        return wrote if wrote else f"Wrote {bootstrap_steps.CONTEXT_FILE}"


ACTIONS[PROJECT_CONTEXT] = ProjectContextAction()


# --- the github_settings action (research D14) -------------------------------------------------

GITHUB_SETTINGS = "github_settings"


def _update_command(default):
    """Bring the checkout to the default branch tip; the remote and branch are named, so the
    command never depends on the local git configuration (FR-010f)."""
    quoted = shlex.quote(default)
    return f"git switch {quoted} && git pull --ff-only origin {quoted}"


def _same_context(local, remote):
    """Whether both texts parse to the same context: line endings, comments and spacing do not
    matter, a text that does not parse never matches."""
    try:
        return project_context.parse_context(local) == project_context.parse_context(remote)
    except (ValueError, KeyError):  # tomllib's decode error is a ValueError
        return False


MANUAL_STEP = "Manual step"  # the prefix of the `github` line for the board workflow
CREDENTIAL_LINE = "Board credential:"  # the `github` line `start` leaves out of the preview
MANUAL_LINES = "manual_lines"  # `Env.session` key: those lines, kept for `after_done`
CREDENTIAL_NOTE = "board_credential_note"  # `Env.session` key: the credential step, if not done


class GitHubSettingsAction(StepAction):
    """The preview is the `github` plan; the confirmed action is `github --apply` (D14).

    `--allow-weaken` is never passed: weakening a ruleset stays a decision made by hand.
    """

    def stop(self, env, step):
        """After the merge, the default branch's file is the authority (research D2): a local
        project-context.toml that differs, or is missing, must be brought up to date first."""
        if not env.bootstrap_merged:
            return None
        default = env.default_branch
        try:
            remote = bootstrap_steps.read_file(
                env.run, env.repo(), default, bootstrap_steps.CONTEXT_FILE, raw=True)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{GITHUB_SETTINGS} failed: cannot read {bootstrap_steps.CONTEXT_FILE}"
                             f" on {default}: {error}", EXIT_ERROR) from error
        path = Path(env.root) / bootstrap_steps.CONTEXT_FILE
        local = path.read_bytes().decode("utf-8", "replace") if path.is_file() else None
        if local is not None and remote is not None and _same_context(local, remote):
            return None
        why = ("is missing" if local is None else "differs from the one on the default branch")
        return {"title": f"the local {bootstrap_steps.CONTEXT_FILE} {why}",
                "text": f"Update this checkout first, so {GITHUB_SETTINGS} uses the merged file.",
                "command": _update_command(default)}

    def _github(self, env, apply):
        """Run the `github` subcommand's own code; (exit code, its printed lines, its plan)."""
        lines, plans = [], []
        arguments = argparse.Namespace(check=False, apply=apply, allow_weaken=False)
        code = bootstrap_github.run_github(
            arguments, env.root, env.run, env.remote_default,
            out=lambda text="": lines.append(str(text)), hint=False,  # `run_github` escapes
            plan_out=plans.append)
        return code, lines, plans[0]

    def preview(self, env, step, candidate=None):
        try:
            _, lines, plan = self._github(env, apply=False)
        except (ValueError, bootstrap_github.CommandError) as error:
            raise StepFailed(f"{GITHUB_SETTINGS} failed: {error}", EXIT_ERROR) from error
        # the number of planned changes, which `perform` announces: from the plan that produced
        # these lines, because a warning or note line can look like a change line
        env.session[GITHUB_SETTINGS] = len(plan.lines)
        # the manual step follows the apply, never the preview (research D18), and the board
        # credential is told once, after `Done: github_settings` (research D19)
        shown = [line for line in lines if not line.startswith((MANUAL_STEP, CREDENTIAL_LINE))]
        while shown and shown[-1] == "":
            shown.pop()
        return shown

    def perform(self, env, step, candidate=None):
        count = env.session.get(GITHUB_SETTINGS)
        if count is None:
            env.progress("Applying GitHub settings…")
        elif count:
            env.progress(f"Applying {count} GitHub {'setting' if count == 1 else 'settings'}…")
        try:
            _, lines, _ = self._github(env, apply=True)
        except ValueError as error:  # the message names the failed area and, for a ruleset
            raise StepFailed(f"{GITHUB_SETTINGS} failed: {error}", EXIT_ERROR) from error
        applied = [line.rstrip(".") for line in lines if line.startswith("Applied ")]
        env.session[MANUAL_LINES] = [line for line in lines if line.startswith(MANUAL_STEP)]
        return "; ".join(applied) if applied else "Nothing to change"

    def after_done(self, env, step):
        """The manual board line, then the credential note, each after a blank line: `start`
        prints the optional credential here, once, and not as its own step (research D19)."""
        lines = []
        for manual in env.session.pop(MANUAL_LINES, []):
            lines.extend(["", manual])
        credential = env.session.pop(CREDENTIAL_NOTE, None)
        if credential:
            lines.extend(["", _printable(credential["title"])])
            doc = (credential.get("action") or {}).get("doc")
            if doc:
                lines.append(f"  See {_printable(doc)}")
        return lines


ACTIONS[GITHUB_SETTINGS] = GitHubSettingsAction()


# --- the pull_request action (research D7) -----------------------------------------------------

PULL_REQUEST = "pull_request"
# The files the bootstrap commits are `bootstrap_steps.BOOTSTRAP_FILES`, from the pilot:
# `speckit-constitution` writes only the constitution and `roadmap-write` only ROADMAP.md, so
# nothing else is added.
COMMIT_MESSAGE = ISSUE_TITLE
PUSH_COMMAND = ["git", "push", "-u", "origin", "HEAD"]
NOTES_FILE = "bootstrap-review-notes.md"
NOTES_IGNORE_ENTRY = f"/{NOTES_FILE}"
NOTES_LIMIT = 60_000  # characters; GitHub's body limit is 65 536, the margin holds `Closes #<n>`
# The body is one argument of `gh pr create`; Linux refuses an argument over 128 KiB (131 072
# bytes), and multibyte text can pass the character limit while exceeding that.
NOTES_BYTE_LIMIT = 100_000
NOTES_DIGEST_KEY = "pull_request_notes_sha256"  # in `env.session`: the notes the digest covered


class ReviewNotes:
    """The validated review notes: where they are, their text and their SHA-256."""

    def __init__(self, path, text, raw):
        self.path, self.text = path, text
        self.sha256 = hashlib.sha256(raw).hexdigest()


# What the notes must not carry into a public pull request body (security audit T136, D21).
# The error names the reason, never the match.
CREDENTIAL_PATTERN = re.compile(
    r"gh[pousr]_[A-Za-z0-9]|github_pat_[A-Za-z0-9]|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|AKIA[0-9A-Z]{16}")
CLOSING_KEYWORD_PATTERN = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(?::[ \t]*|[ \t]+)<?"
    r"(?:(?:[\w.-]+/[\w.-]+)?#\d+"
    r"|https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/issues/\d+)", re.I)
# An @ mention anywhere: not preceded by a letter, digit or `.` (so not an email address),
# followed by a user or team name.
MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9.])@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9._-]+)?")


def _reason(error):
    return error.strerror or type(error).__name__


def _refuse_publishing_risks(text):
    if CREDENTIAL_PATTERN.search(text):
        raise ValueError(f"{NOTES_FILE} contains something that looks like a credential "
                         "(a token, a private key header or an access key ID)")
    for line in text.splitlines():
        if CLOSING_KEYWORD_PATTERN.search(line):
            raise ValueError(f"{NOTES_FILE} has a line with a closing keyword and an issue "
                             "reference, which would close an issue when the pull request merges")
        if MENTION_PATTERN.search(line):
            raise ValueError(f"{NOTES_FILE} has an @ mention, which would notify someone")


def read_review_notes(env):
    """The review notes from the fixed file at the repository root, or a ValueError naming the
    reason (never the content). Reads only; nothing is changed."""
    try:
        top = Path(_read(env, ["git", "rev-parse", "--show-toplevel"]))
    except bootstrap_github.CommandError as error:
        raise ValueError(f"the repository root could not be found ({error})") from error
    path = top / NOTES_FILE
    try:  # one open, no check-then-read: a link swapped in meanwhile is refused too
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # not on a pipe
    except FileNotFoundError as error:
        raise ValueError(f"{NOTES_FILE} is missing at the repository root ({top})") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError(
                f"{NOTES_FILE} is a symbolic link; it must be a regular file") from error
        raise ValueError(f"{NOTES_FILE} cannot be read ({_reason(error)})") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{NOTES_FILE} is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(NOTES_BYTE_LIMIT + 1)  # bounded: a huge file is never read whole
        if len(raw) > NOTES_BYTE_LIMIT:
            raise ValueError(f"{NOTES_FILE} is longer than {NOTES_BYTE_LIMIT} bytes")
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{NOTES_FILE} cannot be read as UTF-8 text") from error
    except OSError as error:
        raise ValueError(f"{NOTES_FILE} cannot be read ({_reason(error)})") from error
    finally:
        os.close(descriptor)
    if not text.strip():
        raise ValueError(f"{NOTES_FILE} is empty")
    if len(text) > NOTES_LIMIT:
        raise ValueError(f"{NOTES_FILE} is longer than {NOTES_LIMIT} characters")
    for char, name in (("\t", "a tab"), ("\r", "a carriage return (Windows line ending)")):
        if char in text:
            raise ValueError(f"{NOTES_FILE} contains {name}, which cannot be shown safely; "
                             "use spaces and line feeds only")
    if any(display_text.is_output_unsafe(char) for char in text if char != "\n"):
        raise ValueError(f"{NOTES_FILE} contains a character that cannot be shown safely "
                         "(a control or format character other than a line feed)")
    _refuse_publishing_risks(text)
    try:
        env.run(["git", "check-ignore", "-q", NOTES_FILE], cwd=top)
    except bootstrap_github.CommandError as error:
        raise ValueError(f"Git does not ignore {NOTES_FILE}; add the line {NOTES_IGNORE_ENTRY} "
                         "to .gitignore so it is never committed") from error
    return ReviewNotes(path, text, raw)


def _status_entries(status):
    """`git status --porcelain -z` as (XY, path, original path or None); a rename or copy is
    one entry followed by its source."""
    tokens = iter(status.split("\0"))
    entries = []
    for token in tokens:
        if len(token) < 4:
            continue
        original = next(tokens, "") if token[0] in "RC" or token[1] in "RC" else None
        entries.append((token[:2], token[3:], original or None))
    return entries


CONSTITUTION_FILE = bootstrap_steps.BOOTSTRAP_FILES[1]
SYNC_IMPACT_REPORT = "Sync Impact Report"
SYNC_IMPACT_REPORT_LINE = (f"Will remove the {SYNC_IMPACT_REPORT} comment at the top of "
                           f"{CONSTITUTION_FILE} before committing.")
# ASCII whitespace, then `<!--` ... the first `-->`; then the rest of that line when it is
# only whitespace, and the blank lines after it.
_LEADING_COMMENT = re.compile(
    r"\A[ \t\r\n]*<!--(?P<body>.*?)-->"
    r"(?:[ \t]*\r?\n(?:[ \t]*\r?\n)*|[ \t]*\Z)?",
    re.DOTALL)


def remove_sync_impact_report(text):
    """`text` without a leading Sync Impact Report comment (FR-006a, research D7); None when it
    does not start with a well-formed one."""
    found = _LEADING_COMMENT.match(text)
    if not found or SYNC_IMPACT_REPORT not in found.group("body"):
        return None
    return text[found.end():]


def _cleaned_constitution(env):
    """The constitution's text without its Sync Impact Report comment; None when there is
    nothing to remove (or the file cannot be read as UTF-8, which is left alone)."""
    path = Path(env.root) / CONSTITUTION_FILE
    try:
        # Never write through a link: neither the file nor any directory above it in the repo.
        if path.is_symlink() or path.resolve() != Path(env.root).resolve() / CONSTITUTION_FILE:
            return None
        text = path.read_bytes().decode("utf-8")
    except (OSError, RuntimeError, UnicodeDecodeError):  # RuntimeError: symlink loop (3.11/3.12)
        return None
    return remove_sync_impact_report(text)


class PullRequestAction(StepAction):
    """Commit, push and open, but only the parts that are missing (research D7).

    Only `bootstrap_steps.BOOTSTRAP_FILES` are committed, by name: other changed files are
    listed, never added.
    """

    def _state(self, env):
        """`git status` and the checked-out branch; a failed read stops with exit 2."""
        try:
            status = env.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                             cwd=env.root)
            return status, _read(env, ["git", "branch", "--show-current"])
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{PULL_REQUEST} failed: cannot read the working tree state ({error})",
                             EXIT_ERROR) from error

    def stop(self, env, step):
        """Not on a bootstrap branch, or a bootstrap file is gone: committing would lose it."""
        status, branch = self._state(env)
        if not bootstrap_steps.BRANCH_PATTERN.match(branch):
            return {"title": "no bootstrap branch is checked out",
                    "text": "Check out the bootstrap issue's branch (the bootstrap_issue step), "
                            "for example git switch <issue-number>-chore-bootstrap.",
                    "command": None}
        reason = remote_mismatch(env, bootstrap_steps.MISMATCH_PR, push=True)
        if reason:
            return reason
        entries = _status_entries(status)
        conflicted = sorted({path for xy, path, _ in entries if "U" in xy or xy in ("DD", "AA")})
        if conflicted:  # `git commit -- <paths>` cannot run during a merge
            many = len(conflicted) > 1
            return {"title": f"{', '.join(conflicted)} "
                             f"{'have merge conflicts' if many else 'has a merge conflict'}",
                    "text": "Resolve the merge conflict, then run start again.",
                    "command": "git status"}
        gone, commands = [], []
        for xy, path, original in entries:
            renamed = "R" in xy and original
            target = original if renamed else path
            not_ours = target not in bootstrap_steps.BOOTSTRAP_FILES
            if not_ours or os.path.lexists(Path(env.root) / target):
                continue
            gone.append(target)
            old, new = shlex.quote(target), shlex.quote(path)  # the real names, never escaped
            if renamed:  # `git mv` also puts the index entry back
                commands.append(f"git mv -- {new} {old}")
            elif xy[0] == "D":  # gone from the index too, so a plain restore finds nothing
                commands.append(f"git restore --staged --worktree -- {old}")
            else:
                commands.append(f"git restore -- {old}")
        if not gone:
            return None
        names = ", ".join(sorted(set(gone)))
        return {"title": f"{names} deleted; the removal would not be committed",
                "text": "Restore it first, then run start again.",
                "command": "; ".join(dict.fromkeys(commands))}

    def _plan(self, env):
        """What is missing: the files to commit, the other changed files, the parts to run."""
        status, branch = self._state(env)
        paths = bootstrap_steps.changed_paths(status)
        found = bootstrap_steps.BRANCH_PATTERN.match(branch)
        if not found:  # `stop` catches this first; never build a "Closes #None"
            raise StepFailed(f"{PULL_REQUEST} failed: no bootstrap branch is checked out",
                             EXIT_STOPPED)
        files = bootstrap_steps.BOOTSTRAP_FILES
        own = sorted({path for path in paths if path in files})
        others = sorted({path for path in paths if path not in files})
        try:
            head, default_tip, pr_head, branch_tip = read_refs(
                env, env.default_branch, strict=True)
        except RefUnreadable as error:
            raise StepFailed(f"{PULL_REQUEST} failed: {error}", EXIT_ERROR) from error
        if default_tip == NONE:
            raise StepFailed(f"{PULL_REQUEST} failed: cannot read the tip of "
                             f"{env.default_branch} (not found)", EXIT_ERROR)
        remote = pr_head if pr_head != NONE else branch_tip
        return {"own": own, "others": others, "branch": branch,
                "issue": found.group(1), "default_tip": default_tip,
                "clean_constitution": CONSTITUTION_FILE in own
                and _cleaned_constitution(env) is not None,
                "commit": bool(own), "push": bool(own) or head != remote,
                "open": pr_head == NONE}

    def _contained(self, env, plan):
        """The files changed since the merge base with the default branch, plus the ones that
        will be committed."""
        try:
            listing = env.run(["git", "diff", "--name-only", "-z", f"{plan['default_tip']}...HEAD"],
                              cwd=env.root)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{PULL_REQUEST} failed: cannot list the files ({error}); "
                             "run `git fetch origin` and start again", EXIT_ERROR) from error
        return sorted({path for path in listing.split("\0") if path} | set(plan["own"]))

    def commit_files(self, env, step):
        return self._plan(env)["own"]

    @staticmethod
    def _wants_notes(env):
        return bool(getattr(env.args, "review_notes", False))

    def _notes(self, env, plan):
        """The review notes when `--review-notes` was given and the pull request is still to be
        created, else None; a notes problem stops with exit 2 before anything is changed."""
        if not (self._wants_notes(env) and plan["open"]):
            return None
        try:
            return read_review_notes(env)
        except ValueError as error:
            raise StepFailed(f"{PULL_REQUEST} failed: {error}", EXIT_ERROR) from error

    def invalid_values(self, env, step):
        if not self._wants_notes(env):
            return None
        plan = self._plan(env)  # its own failures keep their exit code
        try:
            self._notes(env, plan)
        except StepFailed as error:
            return str(error).removeprefix(f"{PULL_REQUEST} failed: ")
        return None

    def preview(self, env, step, candidate=None):
        plan = self._plan(env)
        notes = self._notes(env, plan)
        env.session[NOTES_DIGEST_KEY] = notes.sha256 if notes is not None else None
        lines = repository_head(env)
        if plan["clean_constitution"]:
            lines.append(SYNC_IMPACT_REPORT_LINE)
        if plan["commit"]:
            lines.append(f"Will commit {_printable(', '.join(plan['own']))} with the message "
                         f'"{COMMIT_MESSAGE}".')
        if plan["push"]:
            lines.append(f"Will push the branch with `{' '.join(PUSH_COMMAND)}`.")
        if plan["open"]:
            lines.append(f"Will open a pull request that closes #{plan['issue']}.")
        if notes is not None:
            lines.append(f"Will add these review notes from {_printable(str(notes.path))} to the "
                         "pull request body:")
            lines.extend(f"  | {line}".rstrip() for line in notes.text.splitlines())
        elif self._wants_notes(env) and not plan["open"]:
            lines.append("The pull request already exists; the review notes are not added.")
        if plan["others"]:
            lines.append("Other changed files, not committed: "
                         f"{_printable(', '.join(plan['others']))}")
        files = self._contained(env, plan)
        if files:
            lines.append(f"The pull request will contain: {_printable(', '.join(files))}")
        if notes is None:
            return lines
        shown = PreviewLines(lines)
        shown.digest_lines = [*lines, f"Review notes SHA-256: {notes.sha256}"]
        return shown

    def perform(self, env, step, candidate=None):
        plan = self._plan(env)
        if not (plan["commit"] or plan["push"] or plan["open"]):
            raise StepFailed(f"{PULL_REQUEST} failed: nothing to commit, push or open",
                             EXIT_STOPPED)
        notes = self._notes(env, plan)  # read again, before anything is changed
        if notes is not None and notes.sha256 != env.session.get(NOTES_DIGEST_KEY):
            raise StepFailed(CHANGED, EXIT_CHANGED)  # edited since the digest was checked
        body = f"Closes #{plan['issue']}" + (f"\n\n{notes.text}" if notes else "")
        done = []
        cleaned = _cleaned_constitution(env) if plan["clean_constitution"] else None
        if cleaned is not None:  # None: the file changed since the plan; leave it alone
            try:
                (Path(env.root) / CONSTITUTION_FILE).write_bytes(cleaned.encode("utf-8"))
            except OSError as error:
                raise StepFailed(f"{PULL_REQUEST} failed: cannot remove the {SYNC_IMPACT_REPORT} "
                                 f"comment from {CONSTITUTION_FILE} ({error})",
                                 EXIT_ERROR) from error
        try:
            if plan["commit"]:
                files = len(plan["own"])
                env.progress(f"Committing {files} {'file' if files == 1 else 'files'}…")
                env.run(["git", "add", "--", *plan["own"]], cwd=env.root)
                env.run(["git", "commit", "-m", COMMIT_MESSAGE, "--", *plan["own"]], cwd=env.root)
                done.append("committed")
            if plan["push"]:
                env.progress("Pushing the branch…")
                env.run(PUSH_COMMAND, cwd=env.root)
                done.append("pushed")
            if plan["open"]:
                env.progress("Opening the pull request…")
                created = env.run(
                    ["gh", "pr", "create", "--repo", env.repo(), "--head", plan["branch"],
                     "--base", env.default_branch, "--title", COMMIT_MESSAGE,
                     "--body", body], cwd=env.root)
                done.append("opened")
        except bootstrap_github.CommandError as error:
            earlier = (f" (already {' and '.join(done)}; rerunning start does the rest)"
                       if done else "")
            raise StepFailed(f"{PULL_REQUEST} failed: {error}{earlier}", EXIT_ERROR) from error
        if plan["open"]:
            url = _printable(created.strip())
            number = re.search(r"/pull/(\d+)$", url)
            done[-1] = ("opened pull request " + (f"#{number.group(1)} " if number else "")
                        + f"({url})")  # no "." right after the URL, which it would join
        result = ", ".join(done)
        return result[0].upper() + result[1:]


ACTIONS[PULL_REQUEST] = PullRequestAction()


# --- the first_sync action (research D5) -------------------------------------------------------

FIRST_SYNC = "first_sync"
SYNC_WORKFLOW = "sync-roadmap-issues.yml"
SYNC_COMMAND = ["python3", bootstrap_steps.SYNC_SCRIPT]  # a dry run: `--apply` is never passed
RUN_WAIT = 60  # seconds to wait for the dispatched run to appear
RUN_POLL = 2
FOLLOW_POLL = 5  # seconds between `gh run view` calls while the run is followed
STILL_RUNNING_EVERY = 30  # seconds between the "Still running" progress lines
RUN_LIMIT = "100"  # earlier dispatched runs to remember; far more than one user makes
ORIGIN_URL = bootstrap_steps.ORIGIN_URL  # the sync's own pattern, defined once in the steps


def remote_mismatch(env, mismatch, push=False):
    """A stop reason (`{"title", "text", "command"}`) when `origin`, read like the rest of
    `start` reads it (`env.remote_default`, which accepts SSH aliases and `ssh://` URLs), is not
    a GitHub repository or names another one than `gh` uses (`env.repo()`); None when they
    agree. `mismatch` is the title for the second case, with `{origin}` and `{target}`. A step
    that uses git against `origin` and `gh` on a repository checks it. With `push`, the push URL
    (`env.push_remote_default`; without one, the fetch URL again) must agree too, because
    `git push` goes there."""
    reads = bootstrap_steps.OriginReads(env.remote_default, env.push_remote_default)
    reason = reads.stop(env.repo(), mismatch, push=push)
    return reason  # the title is escaped where it is printed


def repository_line(env):
    """The target repository as a preview line, so the digest binds it."""
    return f"Repository: {_printable(env.repo())}"


def repository_head(env):
    """The `Repository:` line and the blank line that follows it, before the `Will …` lines."""
    return [repository_line(env), ""]


def origin_mismatch(env, step_id, mismatch):
    """The same check for the first sync, whose dry run reads `remote.origin.url` itself with
    the sync's narrower `ORIGIN_URL`: a stop reason when origin is not in a form that pattern
    reads, or names another repository than `gh` uses; None when they agree. `mismatch` is the
    title for the second case, with `{origin}` and `{target}`."""
    try:
        reason = bootstrap_steps.origin_url_stop(env.run, env.root, env.repo(), mismatch)
    except bootstrap_github.CommandError as error:
        raise StepFailed(f"{step_id} failed: cannot read the origin remote ({error})",
                         EXIT_ERROR) from error
    return reason  # the title is escaped where it is printed


def _lines(text):
    """Split on newlines only (`splitlines` also splits on `\\r` and others, which `_printable`
    would then never see); the caller escapes each line where it prints it."""
    return text.rstrip("\n").split("\n") if text else []


def run_timed(argv, cwd=None, timeout=None):
    """Like `bootstrap.run_command`, but gives up after `timeout` seconds with `TimeoutError`."""
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False,
                              timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"{argv[0]} did not answer within {timeout} seconds") from error
    except OSError as error:
        raise bootstrap_github.CommandError(f"cannot run {argv[0]}: {error}") from error
    if done.returncode != 0:
        raise bootstrap_github.CommandError(
            done.stderr.strip() or f"{' '.join(argv[:3])} failed",
            stdout=done.stdout, returncode=done.returncode)
    return done.stdout


def run_captured(argv, cwd=None):
    """Run `argv` and keep what it printed on both streams, whatever its exit code."""
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise bootstrap_github.CommandError(f"cannot run {argv[0]}: {error}") from error
    return SimpleNamespace(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)


def _runs_hint(repo):
    return (f"gh run list --repo {shlex.quote(repo)} --workflow {SYNC_WORKFLOW} "
            "--event workflow_dispatch")


def _follow_hint(repo, run_id):
    return f"gh run watch {run_id} --repo {shlex.quote(repo)} --exit-status"


DRY_RUN_PREFIX = "[dry-run] "
SUB_ISSUE_LINK = re.compile(r"sub-issue of |^(?:parent|move): .* <- ")
# the sync's board lines: `add #5 to project o/3`, `add the roadmap Epic for 001 to project o/3`,
# `add planning issues for 001 to project o/3`, `add provisional Phase 1 for 001 to project o/3
# as Planned`; its `set … Roadmap order …` lines are other changes
BOARD_ITEM = re.compile(r"^add .+ to project \S+/\d+")


def summarize_dry_run(lines):
    """(summary lines, kept lines) of the sync's escaped dry run (research D19): counts of the
    issues to create by kind (the title's prefix up to its first `:`), sub-issue links, board
    items and other changes, from the `[dry-run]` lines; and every other line in full, because
    `[skip]` lines and the like explain a block."""
    kinds, links, items, other, kept = {}, 0, 0, 0, []
    for line in lines:
        if not line.startswith(DRY_RUN_PREFIX):
            kept.append(line)
            continue
        message = line[len(DRY_RUN_PREFIX):]
        if message.startswith("create "):
            title = message[len("create "):]
            kind = title.split(":", 1)[0] if ":" in title else "other"
            kinds[kind] = kinds.get(kind, 0) + 1
        elif SUB_ISSUE_LINK.search(message):
            links += 1
        elif BOARD_ITEM.search(message):
            items += 1
        else:
            other += 1
    summary = []
    if kinds:
        by_kind = ", ".join(f"{count} {kind}" for kind, count in kinds.items())
        total = sum(kinds.values())
        summary.append(f"Will create {total} {'issue' if total == 1 else 'issues'} ({by_kind})")
    summary.extend(f"{count} {name if count == 1 else name + 's'}" for count, name in (
        (links, "sub-issue link"), (items, "board item"), (other, "other change")) if count)
    return summary, kept


class FirstSyncAction(StepAction):
    """The preview summarizes the local dry run; the confirmed action dispatches the apply
    (D5, D19)."""

    def stop(self, env, step):
        """HEAD must be the default tip and the paths the workflow reads (`SYNC_INPUT_PATHS`:
        the roadmap, the specs, the sync's code, the workflow and its local actions) clean: the
        dispatched workflow reads what is on GitHub, so the preview must read the same."""
        default = env.default_branch
        try:
            tip = _tip(env, default, strict=True)
            head = _read(env, ["git", "rev-parse", "HEAD"])
            status = env.run(["git", "status", "--porcelain", "-z", "--untracked-files=all",
                              "--", *bootstrap_steps.SYNC_INPUT_PATHS], cwd=env.root)
        except RefUnreadable as error:
            raise StepFailed(f"{FIRST_SYNC} failed: {error}", EXIT_ERROR) from error
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{FIRST_SYNC} failed: cannot read the working tree state ({error})",
                             EXIT_ERROR) from error
        if tip == NONE:
            raise StepFailed(f"{FIRST_SYNC} failed: cannot read the tip of {default} (not found)",
                             EXIT_ERROR)
        reason = self._other_repository(env)
        if reason:
            return reason
        if head != tip:
            return {"title": f"this checkout is not at the tip of {default}",
                    "text": "The sync reads exactly what is on GitHub: update this checkout.",
                    "command": _update_command(default)}
        dirty = sorted(set(bootstrap_steps.changed_paths(status)))
        if dirty:
            return {"title": f"{', '.join(dirty)} "
                             f"{'have' if len(dirty) > 1 else 'has'} uncommitted changes",
                    "text": "Commit or stash these paths first, then run start again.",
                    "command": "git status -- " + " ".join(bootstrap_steps.SYNC_INPUT_PATHS)}
        return None

    def _other_repository(self, env):
        """A stop reason when the dry run (which reads `remote.origin.url`, like the sync's
        `origin_repo`) and the dispatch (`env.repo()`) would use different repositories."""
        return origin_mismatch(env, FIRST_SYNC, bootstrap_steps.MISMATCH_SYNC)

    def _dry_run(self, env):
        """(exit code, stdout lines, stderr lines, argv, board note) of the sync's dry run,
        raw (the caller escapes the lines where it prints them); only the note is escaped already. The note says why the board is not covered when its credential is unreadable."""
        credential = bootstrap_steps.detect_board_credential(env.repo(), env.run)
        skip = credential.state != "done"
        argv = SYNC_COMMAND + (["--skip-project"] if skip else [])
        note = (f"Board changes are not previewed: {_printable(credential.title)}"
                if credential.state == "not_checked" else None)
        try:
            if env.run_captured is None:
                try:
                    done = SimpleNamespace(returncode=0, stdout=env.run(argv, cwd=env.root),
                                           stderr="")
                except bootstrap_github.CommandError as error:
                    done = SimpleNamespace(returncode=error.returncode, stdout=error.stdout,
                                           stderr=str(error))
            else:
                done = env.run_captured(argv, cwd=env.root)
        except bootstrap_github.CommandError as error:  # the command could not be started
            done = SimpleNamespace(returncode=None, stdout="", stderr=str(error))
        return (done.returncode, _lines(done.stdout or ""), _lines(done.stderr or ""), argv, note)

    def explain(self, env, step):
        """The engine's action names a failed dry run in one line; show the output it refers
        to ("named above"), escaped. It prints only and never fails: whatever goes wrong here,
        the check's own message and the exit code stay. A repository mismatch is printed because
        the engine does not know of it; the other stops (off the tip, dirty) it already names,
        and then the dry run is not offered."""
        if step.get("state") != "todo":
            return
        try:
            other = self._other_repository(env)
            if other is not None:
                env.out(_printable(other["title"]))
                env.out(f"  {_printable(other['text'])}")
                for line in _command_lines(other.get("command")):
                    env.out(line)
                return
            if self.stop(env, step) is not None:
                return
            code, stdout, stderr, _, _ = self._dry_run(env)
        except (StepFailed, bootstrap_github.CommandError, ValueError):
            return
        if code == 0:
            return
        for line in stdout:
            env.out(_printable(line))
        for line in stderr:
            env.err(_printable(line))

    def preview(self, env, step, candidate=None):
        code, stdout, stderr, _, note = self._dry_run(env)
        if code != 0:
            for line in stdout:
                env.out(_printable(line))
            advice = ("fix the roadmap entries named above"
                      if any(bootstrap_steps.BLOCKED_OUTPUT in line for line in stdout)
                      else "the preview failed" + (
                          f" (exit code {code})" if code is not None else ""))
            raise StepFailed("\n".join(
                [*stderr, f"{FIRST_SYNC}: {advice}; no apply is offered"]), EXIT_STOPPED)
        shown = env.session.setdefault(FIRST_SYNC, set())
        if tuple(stderr) not in shown:  # a terminal run previews twice; warn once
            shown.add(tuple(stderr))
            for line in stderr:  # the sync's own warnings: shown, never part of the digest
                env.progress(_printable(line))
        workflow = (f"Will start the {SYNC_WORKFLOW} workflow on GitHub with apply=true and "
                    "follow the run.")
        head = [*repository_head(env), *([note] if note else [])]
        escaped = [_printable(line) for line in stdout]
        summary, kept = summarize_dry_run(escaped)
        full = getattr(env.args, "full_dry_run", False)
        lines = PreviewLines([*head, *summary, *(escaped if full else kept), workflow])
        # the digest covers the whole dry run, with or without --full-dry-run (D19)
        lines.digest_lines = [*head, *escaped, workflow]
        return lines

    def _run_ids(self, env, repo, login):
        """The IDs of the sync workflow's dispatched runs by `login`; ValueError, KeyError or
        TypeError when the listing is not what `gh run list --json databaseId` prints."""
        runs = json.loads(env.run(
            ["gh", "run", "list", "--repo", repo, "--workflow", SYNC_WORKFLOW,
             "--event", "workflow_dispatch", "--user", login, "--limit", RUN_LIMIT,
             "--json", "databaseId"]))
        return {int(run["databaseId"]) for run in runs}

    def _find_run(self, env, repo, login, known):
        """The newest run that was not in `known` (the IDs before the dispatch): no clock
        comparison with GitHub's timestamps is involved. Waits up to `RUN_WAIT` seconds."""
        # Run IDs only grow, so a new run is above every known one; an older ID that was not in
        # the baseline (a run deleted meanwhile, one past the listing's limit) is not ours.
        newest = max(known, default=0)
        deadline = env.clock() + RUN_WAIT
        while True:
            new = {found for found in self._run_ids(env, repo, login) if found > newest}
            if new:
                return max(new)
            if env.clock() >= deadline:
                raise ValueError(f"the run did not appear within {RUN_WAIT} seconds")
            env.sleep(RUN_POLL)

    def _view(self, env, repo, run_id, timeout=None):
        """The run's `{status, conclusion, url}` from `gh run view`; ValueError, KeyError,
        TypeError or AttributeError when it is not what `--json` prints. A view that does not
        answer within `timeout` seconds (with `env.run_timed`) counts as still running."""
        argv = ["gh", "run", "view", str(run_id), "--repo", repo,
                "--json", "status,conclusion,url"]
        if env.run_timed is None:
            text = env.run(argv, cwd=env.root)
        else:
            try:
                text = env.run_timed(argv, cwd=env.root, timeout=timeout)
            except TimeoutError:
                return "in_progress", None, None
        shown = json.loads(text)
        return shown["status"], shown.get("conclusion"), shown.get("url")

    def _follow(self, env, repo, run_id):
        """Poll the run until it completes, with a progress line about every 30 seconds
        (the injected clock, so tests never wait); CommandError or ValueError on a failure."""
        started = last = env.clock()
        announced = linked = False  # the run announced; its URL printed
        timed = env.run_timed is not None
        # without a timeout: the longest `gh run view` so far; the next may take as long
        slowest = 0
        while True:
            asked = env.clock()
            # a view ends a second before the next heartbeat is due (but gets at least one second)
            status, conclusion, url = self._view(
                env, repo, run_id, max(1, last + STILL_RUNNING_EVERY - 1 - asked))
            if not timed:
                slowest = max(slowest, env.clock() - asked)
            if not announced or (url and not linked):
                # the run once it is found, and its URL once, as soon as a view returns it
                env.progress(f"Run {run_id}: {_printable(str(url))}" if url
                             else f"Run {run_id}")
                announced, linked = True, bool(url)
                last = env.clock()
            if status == "completed":
                if conclusion != "success":
                    raise ValueError(f"its conclusion was {display_text.quoted_line(conclusion)}"
                                     if conclusion else "it finished without a conclusion")
                return
            now = env.clock()
            # print when the next look (one poll plus a view) would pass the limit; a timed view
            # is cut off at the limit, so one second of slack is enough
            if now - last + FOLLOW_POLL + (1 if timed else slowest) > STILL_RUNNING_EVERY:
                last = now
                minutes, seconds = divmod(int(now - started), 60)
                env.progress(f"Still running ({minutes} min {seconds} s)…")
            env.sleep(FOLLOW_POLL)

    def perform(self, env, step, candidate=None):
        repo = env.repo()
        try:  # nothing is dispatched yet: a plain failure
            login = env.run(["gh", "api", "user", "--jq", ".login"]).strip()
            known = self._run_ids(env, repo, login)
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError) as error:
            raise StepFailed(f"{FIRST_SYNC} failed: {display_text.inline(error)}",
                             EXIT_ERROR) from error
        env.progress("Starting the sync workflow on GitHub…")
        try:
            env.run(["gh", "workflow", "run", SYNC_WORKFLOW, "--repo", repo, "-f", "apply=true"],
                    cwd=env.root)
        except bootstrap_github.CommandError as error:
            raise StepFailed(f"{FIRST_SYNC} failed: {display_text.inline(error)}",
                             EXIT_ERROR) from error
        env.progress(f"Waiting for GitHub to start the run (up to {RUN_WAIT} s)…")
        try:  # from here on the apply is running on GitHub
            run_id = self._find_run(env, repo, login, known)
        except KeyboardInterrupt:
            env.err("Interrupted: the apply was dispatched; do not rerun start until it "
                    f"finishes. Find the run with `{_runs_hint(repo)}`.")
            raise
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError) as error:
            raise StepFailed(
                f"{FIRST_SYNC} failed: the apply was dispatched; do not rerun start until it "
                f"finishes ({display_text.inline(error)}); find the run with `{_runs_hint(repo)}`",
                EXIT_ERROR) from error
        try:
            self._follow(env, repo, run_id)
        except KeyboardInterrupt:
            env.err("Interrupted: the run continues on GitHub. Follow it with "
                    f"`{_follow_hint(repo, run_id)}`.")
            raise
        except (bootstrap_github.CommandError, ValueError, KeyError, TypeError,
                AttributeError) as error:
            raise StepFailed(
                f"{FIRST_SYNC} failed: run {run_id} did not succeed or could not be watched "
                f"({display_text.inline(error)}); follow it with `{_follow_hint(repo, run_id)}`",
                EXIT_ERROR) from error
        return f"The sync run {run_id} succeeded"


ACTIONS[FIRST_SYNC] = FirstSyncAction()


# --- command line -------------------------------------------------------------------------------


class UsageError(ValueError):
    """A command line usage error: its message quotes the user's argv, so it is shown on one line."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser(prog="bootstrap.py start"):
    """The options of `start`: its own, and the `init` flags it passes to `project_context`."""
    parser = _Parser(prog=prog, description="Work through the bootstrap steps.",
                     allow_abbrev=False)  # `--guide` must never mean `--guided`
    parser.add_argument("--no-input", action="store_true",
                        help="never prompt, even in a terminal; stop with exit 3 to confirm")
    parser.add_argument("--confirm", metavar="STEP_ID@DIGEST",
                        help="perform that step, using the value of a preview's confirm: line")
    parser.add_argument("--issue", type=display_text.int_argument, metavar="N",
                        help="the bootstrap issue to use when several exist")
    parser.add_argument("--guided", action="store_true",
                        help="with --confirm only: perform that step, preview no next step and "
                             "end with a line for the coding agent that guides you")
    parser.add_argument("--full-dry-run", action="store_true",
                        help="show the whole dry run of the first sync, not only its summary")
    parser.add_argument("--review-notes", action="store_true",
                        help="add the notes in bootstrap-review-notes.md at the repository root "
                             "to the body of the bootstrap pull request")
    parser.add_argument("--agents", metavar="TOOL,...",
                        help="save the coding agents you use (comma-separated: "
                             f"{', '.join(agent_setup.TOOLS)}) and generate their files")
    parser.add_argument("--name", help="project name (default: repository name)")
    parser.add_argument("--description", help="one-line project description")
    parser.add_argument("--owner", help="GitHub owner (default: owner of the origin remote)")
    board = parser.add_mutually_exclusive_group()
    board.add_argument("--project-number", type=display_text.int_argument, help="number of the GitHub Project board")
    board.add_argument("--create-board", action="store_true", help="create the board")
    parser.add_argument("--required-approvals", type=display_text.int_argument,
                        help="independent approvals per pull request")
    return parser


def parse_arguments(argv):
    """The parsed options; a usage error raises ValueError instead of ending the process."""
    return build_parser().parse_args(argv)


def run_cli(env, argv):
    """Parse `argv` and run `start`: 0, 1, 2, 3, 4 or 130 (interrupted)."""
    try:
        return run_start(env, parse_arguments(argv))
    except ValueError as error:
        text = f"bootstrap start failed: {error}"
        env.err(_printable(text) if isinstance(error, UsageError) else _shown(text))
        return _guided_end(env, EXIT_ERROR) if "--guided" in argv else EXIT_ERROR
    except KeyboardInterrupt:
        env.err("Interrupted.")
        return EXIT_INTERRUPTED


def _no_runner(argv, cwd=None):
    raise ValueError("start needs a command runner; `bootstrap.py start` supplies one")


def main(argv=None, *, root, run=None, remote_default=None, is_tty=None, prompt=input,
         init=None, validate=None, toml_string=None, run_captured=None,
         push_remote_default=None, run_timed=None):
    """Entry point for `bootstrap.py start`, which supplies everything it runs through: the
    command runner (`run`, as in `bootstrap.main`), `run_captured` (the dry run's runner that
    keeps stderr; None means stdout only), the repository default, and `init`, `validate` and
    `toml_string` for the `project_context` step. This module never imports `bootstrap`."""
    root = Path(root)
    runner = run or _no_runner
    env = Env(root=root, run=runner, is_tty=sys.stdin.isatty() if is_tty is None else is_tty,
              remote_default=remote_default or (lambda: None),
              push_remote_default=push_remote_default,
              prompt=prompt, init=init, validate_context=validate, toml_string=toml_string,
              run_captured=run_captured, run_timed=run_timed)
    given = sys.argv[1:] if argv is None else argv
    try:
        return run_cli(env, given)
    except Exception as error:  # the contract: a message on stderr, never a traceback
        env.err(_shown(f"bootstrap start failed: {error}"))
        return _guided_end(env, EXIT_ERROR) if "--guided" in given else EXIT_ERROR
