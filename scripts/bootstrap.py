#!/usr/bin/env python3
"""Bootstrap a repository created from the template (see docs/bootstrap.md).

Subcommands (no subcommand runs check):
  check   report where you are and what to do next (read-only)
  init    write project-context.toml
  github  preview (default), --apply or --check the GitHub settings (labels, board, ruleset)
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import bootstrap_check
import bootstrap_github
import project_context
# Re-exported: callers and tests use bootstrap.CommandError; it is defined in bootstrap_github
# because bootstrap imports that module (defining it here would be a circular import).
from bootstrap_github import CommandError  # noqa: F401


ROOT = project_context.ROOT
CONFIG_NAME = project_context.DEFAULT_CONFIG.name
SECTION_ORDER = ("project", "github_project", "review", "toolkit")
SCP_REMOTE = re.compile(
    r"[\w.-]+@(?P<host>[\w.-]+):(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?"
)
URL_REMOTE = re.compile(
    r"(?:https?|ssh|git)://(?:[^@/\s]+@)?(?P<host>[^/\s]+)"
    r"/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?"
)
# (section, key, flag, requirement): maps a validation error to the flag that fixes it.
FLAG_MESSAGES = (
    ("github_project", "owner", "--owner", "must be a GitHub owner login"),
    ("github_project", "number", "--project-number", "must be a positive integer"),
    ("project", "name", "--name", "must be a non-empty string"),
    ("project", "description", "--description", "must be a non-empty string"),
    ("review", "required_approvals", "--required-approvals", "must be a non-negative integer"),
)
NEXT_STEPS = """\
Next steps:
  1. Run the speckit-constitution skill to write the constitution, giving it the approval count
     (`[review] required_approvals` in project-context.toml).
  2. Run the roadmap-write skill to create ROADMAP.md.
  3. Run `python3 scripts/bootstrap.py github` to preview the GitHub settings, then add --apply.
  Run `python3 scripts/bootstrap.py` at any time to see what is still missing.
See docs/bootstrap.md for the full guide."""


def run_command(argv, cwd=None):
    """The one place that starts subprocesses; tests inject a fake instead."""
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise CommandError(f"cannot run {argv[0]}: {error}") from error
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or f"{' '.join(argv[:3])} failed")
    return result.stdout


def github_host(host, scp):
    """GitHub's hosts, SSH aliases named github-* or github.com-*, and (scp form only) dotless aliases."""
    host = host.lower()
    if not scp:
        host = re.sub(r":\d+$", "", host)
    return (
        host in ("github.com", "www.github.com", "ssh.github.com")
        or host.startswith(("github.com-", "github-"))
        or (scp and "." not in host)
    )


def parse_remote(url):
    """Return (owner, repository) from an https or ssh GitHub remote URL, else None."""
    url = url.strip()
    for pattern, scp in ((SCP_REMOTE, True), (URL_REMOTE, False)):
        match = pattern.fullmatch(url)
        if match and github_host(match.group("host"), scp):
            return match.group("owner"), match.group("repo")
    return None


def toml_string(value):
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def emit_toml(context):
    """Minimal TOML writer for tables of strings, integers and booleans."""
    sections = []
    for name in SECTION_ORDER:
        if name in context:
            lines = [f"[{name}]"]
            lines += [f"{key} = {toml_value(value)}" for key, value in context[name].items()]
            sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def create_board(owner, title, run):
    """Create a GitHub Project with gh and return its number."""
    output = run(
        ["gh", "project", "create", "--owner", owner, "--title", title, "--format", "json"]
    )
    try:
        number = json.loads(output)["number"]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"unexpected output from gh project create: {output.strip()!r}") from error
    if type(number) is not int or number < 1:
        raise ValueError(f"unexpected project number from gh project create: {number!r}")
    return number


def validate(context):
    """Run the shared validation on the context as it would be written."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / CONFIG_NAME
        path.write_text(emit_toml(context), encoding="utf-8")
        try:
            project_context.load_context(path)
        except ValueError as error:
            for section, key, flag, requirement in FLAG_MESSAGES:
                if str(error).startswith(f"{section}.{key}"):
                    value = repr(context[section][key])  # repr keeps it on one line
                    if len(value) > 80:
                        value = value[:77] + "..."
                    raise ValueError(f"{flag}: {value} {requirement}") from error
            raise


def check_writable(directory):
    """Fail early, before a board is created, when the directory cannot be written."""
    try:
        handle, name = tempfile.mkstemp(dir=directory, prefix=".project-context-check-")
    except OSError as error:
        raise ValueError(f"cannot write to {directory}: {error}") from error
    os.close(handle)
    Path(name).unlink(missing_ok=True)


def write_atomically(path, text, overwrite):
    """Write text to path. A new file is created exclusively (mode from the umask) so a file that
    appeared meanwhile is never overwritten; an existing file is replaced via a temp file that
    keeps its mode."""
    if not overwrite or not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return
    mode = path.stat().st_mode & 0o7777
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".project-context-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def remote_defaults(root, run):
    try:
        return parse_remote(run(["git", "remote", "get-url", "origin"], cwd=root))
    except CommandError:
        return None


def init(args, root, run, is_tty, prompt):
    target = root / CONFIG_NAME
    if target.exists() and not args.force:
        raise ValueError(f"{target} already exists; use --force to overwrite it")
    interactive = is_tty and not args.yes

    def ask(text, flag):
        try:
            return prompt(text)
        except EOFError:
            raise ValueError(f"input ended: pass {flag}") from None

    remote = remote_defaults(root, run)

    defaulted = set()

    def resolve(value, label, flag, default=None):
        if value is not None:
            return value
        if interactive:
            suffix = f" [{default}]" if default is not None else ""
            answer = ask(f"{label}{suffix}: ", flag).strip()
            if answer:
                return answer
        if default is not None:
            defaulted.add(flag)
            return default
        raise ValueError(f"{label} is required: pass {flag}")

    name = resolve(args.name, "Project name", "--name", remote[1] if remote else root.name)
    description = resolve(args.description, "Project description", "--description")
    owner = resolve(args.owner, "GitHub owner", "--owner", remote[0] if remote else None)
    number = args.project_number
    create = args.create_board
    if number is None and not create:
        if interactive:
            answer = ask(
                "GitHub Project number (empty to create one): ", "--project-number"
            ).strip()
            if answer:
                try:
                    number = int(answer)
                except ValueError:
                    raise ValueError(f"Project number must be a whole number, not {answer!r}") from None
            else:
                create = True
        else:
            raise ValueError(
                "A GitHub Project number is required: pass --project-number N "
                "or --create-board to create a board"
            )
    approvals = resolve(
        args.required_approvals, "Required approvals (0 for a solo project)",
        "--required-approvals", 0,
    )
    try:
        approvals = int(approvals)
    except ValueError:
        raise ValueError(f"Required approvals must be a whole number, not {approvals!r}") from None
    context = {
        "project": {"name": name, "description": description},
        "github_project": {"owner": owner, "number": number if number is not None else 1},
        "review": {"required_approvals": approvals},
    }
    validate(context)  # before any board is created
    check_writable(root)
    if create:
        number = create_board(owner, name, run)
        context["github_project"]["number"] = number
    try:
        if create:
            validate(context)
        write_atomically(target, emit_toml(context), overwrite=args.force)
    except (OSError, ValueError) as error:
        if create:
            raise ValueError(
                f"{error} (board #{number} was created; rerun with --project-number {number})"
            ) from error
        raise
    print(f"Wrote {target}")
    if "--required-approvals" in defaulted and approvals == 0:
        print(
            "Notice: required_approvals = 0 (solo project). "
            "A team project must pass --required-approvals 1 or more."
        )
    print()
    print(NEXT_STEPS)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = parser.add_subparsers(dest="command")
    commands.default = "check"  # a plain `bootstrap.py` runs check
    commands.add_parser(
        "check", help="report setup status and the next step (read-only; also the default)"
    )
    init_parser = commands.add_parser("init", help="write project-context.toml")
    init_parser.add_argument("--name", help="project name (default: repository name)")
    init_parser.add_argument("--description", help="one-line project description (required)")
    init_parser.add_argument("--owner", help="GitHub owner (default: owner of the origin remote)")
    board = init_parser.add_mutually_exclusive_group()
    board.add_argument("--project-number", type=int, help="number of the GitHub Project board")
    board.add_argument(
        "--create-board", action="store_true", help="create the board with `gh project create`"
    )
    init_parser.add_argument(
        "--required-approvals",
        type=int,
        help="independent approvals per pull request (default 0, a solo project; "
        "use the number your constitution sets). "
        "A team project must set at least 1; 0 lets any member merge their own "
        "pull request without review.",
    )
    init_parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    init_parser.add_argument(
        "--yes", action="store_true", help="never prompt; use defaults for missing values"
    )
    github_parser = commands.add_parser(
        "github", help="preview, apply or check labels, board and branch ruleset"
    )
    mode = github_parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="make the changes")
    mode.add_argument(
        "--check", action="store_true", help="change nothing; exit 1 if anything differs"
    )
    github_parser.add_argument(
        "--allow-weaken",
        action="store_true",
        help="let --apply lower the ruleset's approvals or turn off last-push approval "
        "(refused and reported otherwise)",
    )
    return parser


def main(argv=None, *, root=ROOT, run=run_command, is_tty=None, prompt=input):
    args = build_parser().parse_args(argv)
    if is_tty is None:
        is_tty = sys.stdin.isatty()
    try:
        if args.command == "check":
            return bootstrap_check.run_check(
                args, Path(root), run, lambda: remote_defaults(Path(root), run)
            )
        if args.command == "github":
            return bootstrap_github.run_github(
                args, Path(root), run, lambda: remote_defaults(Path(root), run)
            )
        init(args, Path(root), run, is_tty, prompt)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (CommandError, OSError, ValueError) as error:
        print(f"bootstrap {args.command} failed: {error}", file=sys.stderr)
        return 2 if args.command == "github" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
