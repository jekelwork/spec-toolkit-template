"""The `bootstrap.py check` subcommand: a read-only status report for a bootstrapping repository.

It lists each setup step with a mark and the next action, and never changes anything locally or
on GitHub. GitHub settings come from the `github --check` logic; every `gh` call goes through the
injected `run`, so tests never reach GitHub.
"""

import argparse
import re

import agent_setup
import bootstrap_github
import check_roadmap
import check_roadmap_dependencies
import display_text
import project_context

OK, FAIL, SKIP = "✓", "✗", "–"
CONSTITUTION = ".specify/memory/constitution.md"
CHANGE_LINE = re.compile(r"(?:Labels|Board|Ruleset|Actions): ")
# A weakening change refused without --allow-weaken: never applied, but drift.
REFUSED_LINE = re.compile(r"Warning: .*--allow-weaken")
GUIDE = project_context.BOOTSTRAP_GUIDE


class Step:
    def __init__(self, mark, title, next_action=None, note=None, fixable_by_sync=False,
                 escaped=False):
        self.mark, self.title, self.next_action, self.note = mark, title, next_action, note
        self.fixable_by_sync = fixable_by_sync  # agent files missing or outdated: sync fixes them
        # True: the text holds lines `run_github` already escaped; `run_check` must not escape
        # them again. Every other step holds raw text (error messages), escaped where it is printed.
        self.escaped = escaped


def first_line(text):
    return str(text).strip().splitlines()[0] if str(text).strip() else "unknown error"


def gh_too_old(error):
    """True when `gh auth status --active` was refused as an unknown flag (gh older than 2.40)."""
    return "unknown flag" in str(error).lower()


def check_context(root):
    """Return (step, context or None)."""
    path = root / project_context.DEFAULT_CONFIG.name
    action = "run `python3 scripts/bootstrap.py init`"
    if not path.exists():
        return Step(FAIL, "project-context.toml is missing", action), None
    try:
        context = project_context.load_context(path)
    except (ValueError, OSError) as error:
        return Step(FAIL, f"project-context.toml is invalid: {first_line(error)}", action), None
    return Step(OK, "project-context.toml is valid"), context


def check_constitution(root, context):
    if (root / CONSTITUTION).is_file():
        return Step(OK, f"Constitution exists ({CONSTITUTION})")
    count = ""
    if context is not None:
        count = (f", giving it the approval count ({project_context.required_approvals(context)} "
                 "from [review] required_approvals)")
    return Step(FAIL, f"Constitution is missing ({CONSTITUTION})",
                f"run the `speckit-constitution` skill{count}")


def check_roadmap_step(root):
    action = "run the `roadmap-write` skill (see python3 scripts/check_roadmap.py)"
    if not (root / "ROADMAP.md").exists():
        return Step(FAIL, "ROADMAP.md is missing", action)
    try:
        entries = check_roadmap.validate_roadmap(root)
    except (check_roadmap_dependencies.InvalidData, OSError, UnicodeError) as error:
        return Step(FAIL, f"ROADMAP.md is invalid: {first_line(error)}", action)
    return Step(OK, f"ROADMAP.md is valid ({len(entries)} entries)")


def github_steps(root, run, remote_default, context):
    """Return (settings step, credential step) from the read-only `github --check` logic."""
    unchecked = Step(SKIP, "Board credential not checked (optional)")
    if context is None:
        return Step(SKIP, "GitHub settings not checked: project-context.toml comes first"), unchecked
    try:
        run(["gh", "auth", "status", "--active", "--hostname", "github.com"])
    except bootstrap_github.CommandError as error:
        if gh_too_old(error):
            return Step(SKIP, "GitHub settings not checked: gh 2.40 or newer is needed",
                        f"upgrade gh to 2.40 or newer ({first_line(error)})"), unchecked
        return Step(SKIP, "GitHub settings not checked: gh is not installed or not logged in",
                    f"install gh and run `gh auth login` ({first_line(error)})"), unchecked
    lines = []
    try:
        code = bootstrap_github.run_github(
            argparse.Namespace(check=True, apply=False), root, run, remote_default,
            out=lines.append)
    except (bootstrap_github.CommandError, ValueError) as error:
        return Step(FAIL, f"GitHub settings could not be checked: {first_line(error)}",
                    "fix the cause, then run `python3 scripts/bootstrap.py github`"), unchecked
    changes = [line for line in lines if CHANGE_LINE.match(line) or REFUSED_LINE.match(line)]
    if code:
        settings = Step(FAIL, f"GitHub settings differ ({len(changes)} change(s))",
                        "run `python3 scripts/bootstrap.py github` to preview, then add --apply")
    else:
        settings = Step(OK, "GitHub settings match (labels, board, ruleset)")
    reviews_required = project_context.required_approvals(context) >= 1
    return settings, credential_step(lines, reviews_required)


def credential_step(lines, reviews_required=False):
    """The credential step from the lines `run_github` printed, which it escaped already."""
    step = _credential_step(lines, reviews_required)
    step.escaped = True
    return step


def _credential_step(lines, reviews_required):
    warnings = [line for line in lines
                if line.startswith("Warning:") and not REFUSED_LINE.match(line)]
    status = next((line.split(": ", 1)[1] for line in lines if line.startswith("Board credential: ")),
                  None)
    note = " ".join(warnings) or None
    if status is None:
        return Step(SKIP, "Board credential not checked (optional)", note=note)
    if status.startswith("not configured"):
        return Step(SKIP, "Board credential is not configured (optional: board sync is skipped)",
                    f"see {bootstrap_github.CREDENTIALS_GUIDE} to enable board sync", note=note)
    if reviews_required and status.startswith(bootstrap_github.TOKEN_SECRET):
        hint = (f"Hint: {bootstrap_github.TOKEN_SECRET} is set and reviews are required: if it is a "
                "personal token, consider a machine account or the GitHub App "
                f"({bootstrap_github.CREDENTIALS_GUIDE}).")
        note = f"{note} {hint}" if note else hint
    return Step(OK, f"Board credential: {status}", note=note)


def check_agents(root):
    """Generated files of the selected tools must exist and match their sources."""
    action = "run `python3 scripts/agent_setup.py check`"
    try:
        problems = agent_setup.find_problems(root)
        expected, _ = agent_setup.expected_agent_files(root)
        selected, tool_error = agent_setup.selected_tools(root, None)
    except (OSError, ValueError, KeyError) as error:
        return Step(FAIL, f"Agent files cannot be checked: {first_line(error)}", action)
    if problems or tool_error:
        return Step(FAIL, f"Agent setup has problems: {first_line(problems[0] if problems else tool_error)}",
                    action)
    wanted = {path: text for path, text in expected.items()
              if agent_setup.tool_of(path) in selected}
    if not wanted:
        return Step(FAIL, "No sub-agent definitions found", action)
    missing = [path for path in wanted if not (root / path).is_file()]
    stale = missing + [path for path, text in wanted.items() if path not in missing
                       and (root / path).read_text(encoding="utf-8") != text]
    sync = "run `npm ci` or `python3 scripts/agent_setup.py sync`"
    if len(missing) == len(wanted):
        return Step(FAIL, "Agent files are not generated", sync, fixable_by_sync=True)
    if stale:
        return Step(FAIL, f"Agent files are out of date ({len(stale)} of {len(wanted)})",
                    "run `python3 scripts/agent_setup.py sync`", fixable_by_sync=True)
    return Step(OK, "Agent files are generated and up to date")


def run_check(args, root, run, remote_default, out=print):
    """Print the report and return 0 when every required step passes, else 1."""
    context_step, context = check_context(root)
    settings, credential = github_steps(root, run, remote_default, context)
    steps = [
        context_step,
        check_constitution(root, context),
        check_roadmap_step(root),
        settings,
        credential,
        check_agents(root),
    ]
    def show(step, text):
        """One line of the report: error text (from gh, git, the files) is escaped here, once."""
        out(text if step is not None and step.escaped else display_text.printable(text))

    show(None, f"Bootstrap status for {root}")
    for number, step in enumerate(steps, 1):
        show(step, f"  {step.mark} {number}. {step.title}")
        if step.mark != OK and step.next_action:
            show(step, f"       Next: {step.next_action}")
        if step.note:
            show(step, f"       {step.note}")
    failed = [step for step in steps if step.mark == FAIL]
    out("")
    if failed:
        show(failed[0], f"Next: {failed[0].next_action}. See {GUIDE}.")
        return 1
    if settings.mark == SKIP:
        out("Local steps pass; GitHub settings were not verified (see above).")
    out("If you have not done so yet:")
    out("  1. Commit these files in one bootstrap pull request and merge it.")
    out("  2. Run the first roadmap sync: preview, read the log, then apply:")
    out("       gh workflow run sync-roadmap-issues.yml -f apply=false")
    out("       gh workflow run sync-roadmap-issues.yml -f apply=true")
    out("  3. Start your first feature: run the `speckit-specify` skill for a roadmap entry")
    out(f"     (see {GUIDE}#your-first-feature).")
    return 0
