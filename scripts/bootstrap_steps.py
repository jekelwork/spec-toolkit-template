"""The guided-bootstrap step catalogue and the repository context read once per run.

Every `gh` call goes through the injected `run`, so tests never reach GitHub, and nothing here
ever runs `git fetch` (research D2).
"""

import importlib.util
import json
import re
import shlex
import sys
from collections import namedtuple
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import agent_setup
import bootstrap_check
import bootstrap_github
import display_text
import project_board
import project_context
import roadmap_issues

STATES = ("done", "todo", "manual", "optional", "waiting", "not_checked")
NON_BLOCKING_ONLY = ("manual", "optional")
CONTEXT_FILE = "project-context.toml"
# The files the bootstrap commits; they are also the merge markers (research D2, D7).
BOOTSTRAP_FILES = (CONTEXT_FILE, ".specify/memory/constitution.md", "ROADMAP.md")
RAW_ACCEPT = "Accept: application/vnd.github.raw"

# The fixed, run-independent part of a step: the catalogue table of data-model.md.
Definition = namedtuple(
    "Definition", "id blocking actor needs_decision needs_confirmation bypasses_review")


CATALOGUE = (
    Definition("prerequisites", True, "user", False, False, False),
    Definition("bootstrap_issue", True, "engine", False, True, True),
    Definition("project_context", True, "engine", True, True, True),
    Definition("constitution", True, "user", True, False, False),
    Definition("roadmap", True, "user", True, False, False),
    Definition("github_settings", True, "engine", False, True, True),
    Definition("board_credential", False, "user", False, False, False),
    Definition("board_workflows", False, "user_web", False, False, False),
    Definition("pull_request", True, "engine", False, True, False),
    Definition("merge", True, "user_web", False, False, False),
    Definition("first_sync", True, "engine", False, True, True),
)
_BY_ID = {entry.id: entry for entry in CATALOGUE}


@dataclass
class Step:
    id: str
    title: str
    state: str
    blocking: bool
    actor: str
    needs_decision: bool
    needs_confirmation: bool
    performable: bool
    bypasses_review: bool
    action: dict | None = None
    details: dict = field(default_factory=dict)


class StepError(ValueError):
    """`make_step` was called with an invalid step or state: a wiring bug, never a check result."""


def make_step(step_id, state, title, performable=False, action=None, details=None):
    """Build a Step from its catalogue definition, enforcing the data-model rules."""
    if step_id not in _BY_ID:
        raise StepError(f"unknown step id: {step_id}")
    if state not in STATES:
        raise StepError(f"unknown state: {state}")
    entry = _BY_ID[step_id]
    if state in NON_BLOCKING_ONLY and entry.blocking:
        raise StepError(f"state {state} is only allowed on non-blocking steps, not {step_id}")
    if performable and not entry.needs_confirmation:
        raise StepError(f"step {step_id} needs no confirmation, so it cannot be performable")
    return Step(
        id=step_id, title=title, state=state, blocking=entry.blocking, actor=entry.actor,
        needs_decision=entry.needs_decision, needs_confirmation=entry.needs_confirmation,
        performable=bool(performable),
        bypasses_review=entry.bypasses_review, action=action, details=details or {})


@dataclass
class RepositoryContext:
    default_branch: str | None
    bootstrap_merged: bool | None
    default_tip: str | None
    context_text: str | None = None  # the default branch's project-context.toml, read once
    error: str | None = None  # first line of the read error that made a field null


def read_file(run, repo, branch, path, raw=False):
    """The file's text (raw content when asked) on `branch`, None on a 404; others propagate."""
    url = f"repos/{repo}/contents/{path}?ref={quote(branch, safe='/')}"
    argv = ["gh", "api", "-H", RAW_ACCEPT, url] if raw else ["gh", "api", url]
    try:
        return run(argv)
    except bootstrap_github.CommandError as error:
        if "HTTP 404" in str(error):
            return None
        raise


def read_repository_context(repo, run):
    """Read the default branch, whether bootstrap is merged there and its tip (research D2).

    `project-context.toml` is read as raw text in the same call that proves it exists, so the
    validation and the approval count reuse that one read; a read error keeps its first line.
    """
    try:
        view = json.loads(run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"]))
        branch = view["defaultBranchRef"]["name"]
    except bootstrap_github.CommandError as error:
        return RepositoryContext(None, None, None, error=bootstrap_check.first_line(error))
    except (ValueError, KeyError, TypeError):
        return RepositoryContext(None, None, None, error="the default branch could not be read")
    if not branch:
        return RepositoryContext(None, None, None, error="the default branch could not be read")
    missing, error_line, text = False, None, None
    for path in BOOTSTRAP_FILES:
        try:
            found = read_file(run, repo, branch, path, raw=path == CONTEXT_FILE)
        except bootstrap_github.CommandError as error:
            error_line = error_line or bootstrap_check.first_line(error)
            continue
        missing = missing or found is None
        if path == CONTEXT_FILE:
            text = found
    # one missing file proves bootstrap is not merged, even when another read failed
    merged = False if missing else (None if error_line else True)
    try:
        tip = run(["gh", "api", f"repos/{repo}/branches/{quote(branch, safe='/')}",
                   "--jq", ".commit.sha"]).strip() or None
    except bootstrap_github.CommandError:
        tip = None
    return RepositoryContext(branch, merged, tip, text, error_line)


UNSAFE_COMMAND_NOTE = ("The command is left out: it contains characters that cannot be shown "
                       "safely, so work it out by hand after checking the names involved.")


def action(text, command=None, doc=None):
    """A step's action. A command is never escaped (an escaped one would not run as shown): one
    with an output-unsafe character is dropped and `text` says why."""
    if command is not None and display_text.has_unsafe(command):
        text, command = f"{text} {UNSAFE_COMMAND_NOTE}", None
    return {"text": text, "command": command, "doc": doc}


def _escaped(value):
    """`value` with every string in it escaped (`display_text.printable`), through lists, tuples
    and dicts; other values are kept."""
    if isinstance(value, str):
        return display_text.printable(value)
    if isinstance(value, dict):
        return {key: _escaped(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_escaped(item) for item in value]
    return value


def escape_report(report):
    """A copy of the `check --json` report with the text a guide agent would echo escaped once:
    each step's `title`, its action's `text`, every string in its `details`, and
    `default_branch`. Commands, documentation links, ids and states are left alone; the format
    stays version 1 (only unsafe characters, and backslashes, are shown differently). The report
    `start` reads is not escaped: it escapes where it prints."""
    escaped = dict(report)
    if isinstance(escaped.get("default_branch"), str):
        escaped["default_branch"] = display_text.printable(escaped["default_branch"])
    steps = []
    for step in report.get("steps", ()):
        step = dict(step, title=display_text.printable(step["title"]),
                    details=_escaped(step.get("details") or {}))
        if step.get("action"):
            step["action"] = dict(step["action"], text=display_text.printable(step["action"]["text"]))
        steps.append(step)
    if "steps" in report:
        escaped["steps"] = steps
    return escaped


def project_scope(auth_output):
    """`listed`, `missing` or `not_readable` from the "Token scopes" line of `gh auth status`."""
    line = next((l for l in auth_output.splitlines() if "Token scopes:" in l), "")
    scopes = re.findall(r"'([^']+)'", line)
    if not scopes:
        return "not_readable"
    return "listed" if "project" in scopes else "missing"


def saved_agent_tools(root):
    """The saved `agent-setup.tools` names: known ones in `TOOLS` order, then unknown ones in
    saved order, duplicates dropped; None when unset (research D17). A local git read, no GitHub call."""
    saved = agent_setup.configured_names(root)
    if not saved:
        return None
    known = [name for name in agent_setup.TOOLS if name in saved]
    unknown = [name for name in dict.fromkeys(saved) if name not in agent_setup.TOOLS]
    return known + unknown


def detect_prerequisites(root, run):
    """`gh` is logged in, has the `project` scope when readable, and agent files are current (D10)."""
    doc = "docs/bootstrap.md#prerequisites"
    tools = saved_agent_tools(root)
    try:
        output = run(["gh", "auth", "status", "--active", "--hostname", "github.com"])
    except bootstrap_github.CommandError as error:
        if bootstrap_check.gh_too_old(error):
            return make_step(
                "prerequisites", "todo",
                f"gh 2.40 or newer is needed: {bootstrap_check.first_line(error)}",
                action=action("Upgrade gh to 2.40 or newer.", None, doc),
                details={"agent_tools": tools})
        return make_step(
            "prerequisites", "todo",
            f"gh is not installed or not logged in: {bootstrap_check.first_line(error)}",
            action=action("Install gh and log in.", "gh auth login", doc),
            details={"agent_tools": tools})
    scope = project_scope(output)
    details = {"project_scope": scope, "agent_tools": tools}
    if scope == "missing":
        return make_step(
            "prerequisites", "todo", "The gh login lacks the project scope",
            action=action("Add the project scope to the gh login.",
                          "gh auth refresh -s project", doc), details=details)
    try:
        agents = bootstrap_check.check_agents(root)
    except (bootstrap_github.CommandError, ValueError, OSError) as error:
        # `gh` itself answered, so the GitHub steps still run; keep `project_scope` in details
        return _not_checked("prerequisites", bootstrap_check.first_line(error), doc,
                            details=details)
    if agents.mark != bootstrap_check.OK:
        # sync only fixes missing or outdated files; other problems need a look first
        command = ("python3 scripts/agent_setup.py sync" if agents.fixable_by_sync
                   else "python3 scripts/agent_setup.py check")
        return make_step(
            "prerequisites", "todo", agents.title,
            action=action(f"{agents.next_action[:1].upper()}{agents.next_action[1:]}.",
                          command, doc), details=details)
    return make_step("prerequisites", "done", "gh is logged in and agent files are up to date",
                     details=details)


# Titles of the stop for an `origin` that names another repository than the one `gh` uses, with
# `{origin}` and `{target}`: one per step, shared by `start` and the detection below.
MISMATCH_ISSUE = "git would use {origin} but gh would use {target}"
MISMATCH_PR = ("the branch would be pushed to {origin} but the pull request would open on "
               "{target}")


def repository_mismatch(origin, target, mismatch):
    """The stop (`{"title", "text", "command"}`) for an origin naming another repository."""
    return {"title": mismatch.format(origin=origin, target=target),
            "text": "Both must name the same repository: make gh use the origin remote's.",
            "command": f"gh repo set-default {shlex.quote(origin)}"}


class OriginReads:
    """What git says about `origin`, read lazily and once: the fetch URL as `(owner, name)` or
    None, and (when `pushes` is given) the list of push URLs, each `(owner, name)` or None.

    `stop` is the one check behind `start`'s stop and the `performable` flag of the steps that
    use git against `origin` and `gh` on a repository (research D4: performable only when
    `--confirm` would act)."""

    def __init__(self, fetch, pushes=None):
        self._readers = {"fetch": fetch, "pushes": pushes}
        self._read = {}

    def _get(self, name):
        if name not in self._read:
            reader = self._readers[name]
            self._read[name] = reader() if reader is not None else None
        return self._read[name]

    def fetch(self):
        return self._get("fetch")

    def pushes(self):
        return self._get("pushes")

    def stop(self, target, mismatch, push=False):
        """None when origin (and, with `push`, every push URL) names `target`, else the stop.
        `target` is the repository `gh` uses; `mismatch` is the title for another repository.
        An unset push URL reads as the fetch URL, so it behaves as without `push`."""
        remote = self.fetch()
        if remote is None:
            return {"title": "the origin remote is not a GitHub repository",
                    "text": "Point origin at the GitHub repository, keeping your SSH alias if you "
                            "use one (`git remote set-url origin <url>`).",
                    "command": "git remote -v"}
        origin = "/".join(remote)
        if origin.casefold() != target.casefold():
            return repository_mismatch(origin, target, mismatch)
        pushes = self.pushes() if push else None
        if pushes is None:
            return None
        for pushed in pushes:
            if pushed is None:
                return {"title": "the push URL of origin is not a GitHub repository",
                        "text": "Point every push URL at the GitHub repository, or remove it "
                                "(`git remote set-url --push origin <url>`).",
                        "command": "git remote get-url --push --all origin"}
            if "/".join(pushed).casefold() != target.casefold():
                return {"title": f"the push URL of origin names {'/'.join(pushed)} but gh "
                                 f"would use {target}",
                        "text": "Point every push URL at the same repository, or remove it "
                                "(`git remote set-url --push origin <url>`).",
                        "command": "git remote get-url --push --all origin"}
        return None


# the pattern the sync's `origin_repo` applies to `remote.origin.url`: one definition for both
ORIGIN_URL = project_board.sync.ORIGIN_URL
MISMATCH_SYNC = "the dry run reads {origin} but the apply would run on {target}"


# the one implementation lives in `project_context`, which the sync script shares
mask_url_credentials = project_context.mask_url_credentials


def origin_url_stop(run, root, repo, mismatch):
    """The first sync's stop for `origin`, shared by `start` and detection. The dry run reads
    `remote.origin.url` itself with the sync's narrower `ORIGIN_URL`: a stop reason
    (`{"title", "text", "command"}`) when origin is not in a form that pattern reads, or names
    another repository than `repo` (the one `gh` uses); None when they agree. `mismatch` is the
    title for the second case, with `{origin}` and `{target}`. Raises `CommandError` when the
    URL cannot be read."""
    url = run(["git", "config", "--get", "remote.origin.url"], cwd=root).strip()
    found = ORIGIN_URL.match(url)
    if not found:
        ssh = url.startswith(("ssh://", "git@"))  # keep the kind of URL the user has
        suggested = f"git@github.com:{repo}.git" if ssh else f"https://github.com/{repo}.git"
        return {"title": f"the origin remote URL is not in a supported form: "
                         f"{display_text.quoted_line(mask_url_credentials(url))}",
                "text": "Point origin at the GitHub repository, as https://github.com/<owner>/"
                        "<repo>.git or git@github.com:<owner>/<repo>.git.",
                "command": "git remote set-url origin " + shlex.quote(suggested)}
    origin = f"{found.group(1)}/{found.group(2)}"
    if origin.casefold() == repo.casefold():
        return None
    return repository_mismatch(origin, repo, mismatch)


# The whole name must match, and every name it accepts also passes `pr_conventions`'
# ISSUE_BRANCH_PATTERN (the CI branch check): a branch name is interpolated into commands that
# are printed for the user to run, and only such a branch can carry the bootstrap pull request.
BRANCH_PATTERN = re.compile(r"([1-9][0-9]*)-chore-bootstrap(?:-[a-z0-9]+)*\Z")
BOOTSTRAP_MARKER = "<!-- spec-toolkit:bootstrap -->"
ISSUE_DOC = "docs/bootstrap.md#bootstrap-issue"


def changed_entries(status_output):
    """`(status, path)` pairs of `git status --porcelain -z`; a rename or copy names two paths."""
    found = []
    entries = iter(status_output.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        found.append((entry[:2], entry[3:]))
        if entry[0] in "RC" or entry[1] in "RC":
            second = next(entries, "")
            if second:
                found.append((entry[:2], second))
    return found


def changed_paths(status_output):
    """Paths named by `git status --porcelain -z`; a rename or copy names two paths."""
    return [path for _, path in changed_entries(status_output)]


def other_changes(root, run):
    """Uncommitted paths that are not bootstrap files, as (changed tracked, untracked): the
    untracked ones stay in the working tree when the branch is checked out (research D19)."""
    status = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=root)
    others = [(xy, path) for xy, path in changed_entries(status) if path not in BOOTSTRAP_FILES]
    return ([path for xy, path in others if xy != "??"],
            [path for xy, path in others if xy == "??"])


def issue_candidates(root, run, repo):
    """Open bootstrap issues as `{number, title, branch}` (research D3), and the current branch."""
    login = run(["gh", "api", "user", "--jq", ".login"]).strip()
    issues = json.loads(run(["gh", "issue", "list", "--repo", repo, "--state", "open",
                             "--limit", "1000", "--json", "number,title,body,author"]))
    current = run(["git", "branch", "--show-current"], cwd=root).strip()
    local = run(["git", "branch", "--format=%(refname:short)"], cwd=root).split()
    remote = run(["gh", "api", "--paginate", f"repos/{repo}/branches",
                  "--jq", ".[].name"]).split()
    branches = {}
    for name in [current, *local, *remote]:
        match = BRANCH_PATTERN.match(name)
        if match:
            branches.setdefault(int(match.group(1)), []).append(name)
    candidates = []
    for item in sorted(issues, key=lambda entry: entry["number"]):
        number = item["number"]
        own_marker = (BOOTSTRAP_MARKER in (item.get("body") or "")
                      and (item.get("author") or {}).get("login") == login)
        if not own_marker and number not in branches:
            continue
        names = branches.get(number, [])
        branch = current if current in names else (names[0] if names else None)
        candidates.append({"number": number, "title": item["title"], "branch": branch})
    return candidates, current, set(local)


def detect_bootstrap_issue(root, run, repo, ctx, origin=None):
    """Which bootstrap issue the engine would use, and whether its branch is checked out (D3)."""
    if ctx.bootstrap_merged:
        return make_step("bootstrap_issue", "done",
                         f"Bootstrap is on {ctx.default_branch}; the issue no longer matters")
    try:
        candidates, current, local = issue_candidates(root, run, repo)
        tracked, untracked = other_changes(root, run)
    except (bootstrap_github.CommandError, ValueError, KeyError, TypeError) as error:
        return make_step("bootstrap_issue", "not_checked",
                         f"Bootstrap issue could not be checked: {bootstrap_check.first_line(error)}",
                         action=action("Fix the cause, then run the check again.",
                                       "python3 scripts/bootstrap.py check", ISSUE_DOC))
    details = {"candidates": candidates}
    checked_out = [found for found in candidates if found["branch"] == current]
    if len(checked_out) == 1:  # the checked-out branch is the choice, whatever else is open
        return make_step("bootstrap_issue", "done",
                         f"Bootstrap issue #{checked_out[0]['number']} is checked out",
                         details=details)
    # without a default branch there is nothing to cut the new branch from
    performable = not tracked and ctx.default_branch is not None
    if len(candidates) > 1:
        title = f"{len(candidates)} bootstrap issues found; choose one"
        step_action = action("Choose the issue to use: start --confirm bootstrap_issue@<digest> "
                             "--issue <number>.", doc=ISSUE_DOC)
    elif not candidates:
        title = "No bootstrap issue yet"
        step_action = action("Create the bootstrap issue and its branch.", doc=ISSUE_DOC)
    else:
        found = candidates[0]
        number, branch = found["number"], found["branch"]
        title = f"Bootstrap issue #{number} found"
        if branch is None:
            name = f"{number}-chore-bootstrap"
            base = shlex.quote(ctx.default_branch) if ctx.default_branch is not None else None
            command = (f"gh issue develop {number} --base {base} --name {name} "
                       "--checkout" if base is not None else None)
            step_action = action(f"Create the branch for issue #{number}.", command, ISSUE_DOC)
        elif branch in local:
            step_action = action(f"Check out {branch}.",
                                 f"git switch {shlex.quote(branch)}", ISSUE_DOC)
        else:
            quoted = shlex.quote(branch)
            step_action = action(f"Fetch and check out {branch}.",
                                 f"git fetch origin {quoted} && git switch {quoted}", ISSUE_DOC)
    if tracked:
        title = f"{title}; changed tracked files: {', '.join(tracked)}"
        step_action = action(f"Commit, stash or revert the changes to {', '.join(tracked)}, "
                             "then run start again.", doc=ISSUE_DOC)
    if untracked:
        title = (f"{title}; untracked files, carried over and not committed: "
                 f"{', '.join(untracked)}")
    reason = origin_stop(origin, repo, MISMATCH_ISSUE) if performable else None
    if reason:  # `start --confirm` stops here too
        return _stopped("bootstrap_issue", title, reason, ISSUE_DOC, details)
    return make_step("bootstrap_issue", "todo", title, performable=performable,
                     action=step_action, details=details)


CONTEXT_DOC = "docs/bootstrap.md#project-contexttoml"
CONSTITUTION_DOC = "docs/bootstrap.md#constitution"
ROADMAP_DOC = "docs/bootstrap.md#roadmap"
SETTINGS_DOC = "docs/bootstrap.md#github-settings"


def _local_context(root):
    """(context or None, error line or None) of the local project-context.toml."""
    path = root / CONTEXT_FILE
    if not path.is_file():
        return None, None
    try:
        return project_context.load_context(path), None
    except (ValueError, OSError, UnicodeError) as error:
        return None, bootstrap_check.first_line(error)


def _approvals(root, ctx):
    """The approval count: the default branch's file once merged, else the local one."""
    try:
        if ctx.bootstrap_merged:
            return project_context.required_approvals(
                project_context.parse_context(ctx.context_text))
    except (ValueError, TypeError):
        return None
    local, _ = _local_context(root)
    return None if local is None else project_context.required_approvals(local)


def detect_project_context(root, ctx, warning):
    """project-context.toml, plus the ruleset warning for a private repository (D2, D14).

    `warning` is the run's one `ruleset_warning`, shared with `github_settings`.
    """
    step = _project_context_step(root, ctx)
    if warning:
        step.details["ruleset_warning"] = warning
    return step


def _project_context_step(root, ctx):
    """project-context.toml: the default branch's file once merged, else the local one (D2)."""
    if ctx.bootstrap_merged:
        if ctx.context_text is None:
            return make_step("project_context", "not_checked",
                             "project-context.toml could not be read on "
                             f"{ctx.default_branch}: {ctx.error or 'no content'}",
                             action=action("Fix the cause, then run the check again.",
                                           "python3 scripts/bootstrap.py check", CONTEXT_DOC))
        try:
            project_context.parse_context(ctx.context_text)
        except ValueError as error:
            return make_step(
                "project_context", "todo",
                f"project-context.toml on {ctx.default_branch} is invalid: "
                f"{bootstrap_check.first_line(error)}",
                action=action("Fix project-context.toml in a pull request.", doc=CONTEXT_DOC))
        return make_step("project_context", "done", "project-context.toml is valid")
    local, problem = _local_context(root)
    if local is not None:
        return make_step("project_context", "done", "project-context.toml is valid")
    if problem is None:
        return make_step(
            "project_context", "todo", "project-context.toml is missing", performable=True,
            action=action("Create project-context.toml.", doc=CONTEXT_DOC))
    return make_step(
        "project_context", "todo", f"project-context.toml is invalid: {problem}",
        action=action("Fix project-context.toml; start never overwrites it.", doc=CONTEXT_DOC))


def detect_constitution(root, ctx):
    """The constitution file; done once bootstrap is merged (D2)."""
    count = _approvals(root, ctx)
    details = {"required_approvals": count}
    if ctx.bootstrap_merged or (root / bootstrap_check.CONSTITUTION).is_file():
        return make_step("constitution", "done", "Constitution exists", details=details)
    given = f", giving it the approval count ({count})" if count is not None else ""
    return make_step(
        "constitution", "todo", f"Constitution is missing ({bootstrap_check.CONSTITUTION})",
        action=action(f"Run the speckit-constitution skill{given}.", doc=CONSTITUTION_DOC),
        details=details)


def detect_roadmap(root, ctx):
    """ROADMAP.md; done once bootstrap is merged (D2)."""
    if ctx.bootstrap_merged:
        return make_step("roadmap", "done", "ROADMAP.md is on the default branch")
    local = bootstrap_check.check_roadmap_step(root)
    if local.mark == bootstrap_check.OK:
        return make_step("roadmap", "done", local.title)
    return make_step("roadmap", "todo", local.title,
                     action=action("Run the roadmap-write skill.", doc=ROADMAP_DOC))


def ruleset_warning(repo, run):
    """Why rulesets may not work on a private repository, else None (research D14).

    Read-only: the visibility, then the same rulesets GET `github` uses. A refusal goes through
    `denied_hint`; a 404 or any other error is only "could not confirm".
    """
    try:
        visibility = json.loads(run(["gh", "repo", "view", repo, "--json", "visibility"]))
        private = str(visibility.get("visibility", "")).upper() == "PRIVATE"
    except (bootstrap_github.CommandError, ValueError, AttributeError):
        return None
    if not private:
        return None
    try:
        run(["gh", "api", f"repos/{repo}/rulesets?includes_parents=false&per_page=100"])
    except bootstrap_github.CommandError as error:
        if "HTTP 403" in str(error):
            return bootstrap_github.denied_hint(error)
        return ("could not confirm that rulesets are available: "
                f"{bootstrap_check.first_line(error)}")
    return None


def detect_github_settings(root, run, repo, ctx, warning):
    """Labels, board, ruleset and fork approval against `project-context.toml` (D2, D14).

    The approval count comes from the default branch's file once bootstrap is merged, so an
    outdated checkout cannot change the result. `warning` is the run's one `ruleset_warning`.
    """
    details = {}
    if warning:
        details["ruleset_warning"] = warning
    def not_checked(error, doc):
        return make_step(
            "github_settings", "not_checked",
            f"GitHub settings could not be checked: {bootstrap_check.first_line(error)}",
            details=details,
            action=action("Fix the cause, then run the check again.",
                          "python3 scripts/bootstrap.py github --check", doc))

    # A context that will not load points at its own docs; any failure while comparing with
    # GitHub (refused rulesets, a missing repository, unexpected output) points at the settings.
    try:
        if ctx.bootstrap_merged:
            if ctx.context_text is None:
                raise ValueError(ctx.error or "project-context.toml is not readable")
            context = project_context.parse_context(ctx.context_text)
        else:
            path = root / CONTEXT_FILE
            if not path.is_file():
                raise ValueError("project-context.toml is missing")
            context = project_context.load_context(path)
    except (ValueError, OSError, KeyError) as error:
        return not_checked(error, CONTEXT_DOC)
    try:
        plan, _ = bootstrap_github.plan_settings(repo, context, run)
    except (bootstrap_github.CommandError, ValueError, OSError, KeyError) as error:
        return not_checked(error, SETTINGS_DOC)
    if plan.lines:
        changes = len(plan.lines) + len(plan.warnings)
        title = f"GitHub settings differ ({changes} change(s))"
        if plan.warnings:
            title = (f"GitHub settings differ ({changes} change(s), "
                     f"{len(plan.warnings)} to decide)")
        return make_step(
            "github_settings", "todo", title,
            performable=True, details=details,
            action=action("Preview the changes, then apply them.",
                          "python3 scripts/bootstrap.py github", SETTINGS_DOC))
    if plan.warnings:
        return make_step(
            "github_settings", "todo",
            f"GitHub settings differ ({len(plan.warnings)} to decide)",
            details=details,
            action=action("The ruleset is stricter than project-context.toml: preview with "
                          "`python3 scripts/bootstrap.py github --allow-weaken`, then decide "
                          "whether to apply with `--apply --allow-weaken` or raise "
                          "required_approvals.",
                          "python3 scripts/bootstrap.py github --allow-weaken",
                          SETTINGS_DOC))
    return make_step("github_settings", "done",
                     "GitHub settings match (labels, board, ruleset, fork approval)",
                     details=details)


CREDENTIALS_DOC = bootstrap_github.CREDENTIALS_GUIDE
WORKFLOW_PR_LINKED = "Pull request linked to issue"


def detect_board_credential(repo, run):
    """Which board credential is configured, from secret names only (FR-008, research D6).

    Non-blocking: not configured is `optional`. Values are never read, and `details.secrets`
    lists only the names of the toolkit's own credential secrets.
    """
    found = bootstrap_github.read_credential_secrets(repo, run)
    if found.error:
        return make_step(
            "board_credential", "not_checked",
            "Board credential could not be checked: cannot read secret names "
            f"({bootstrap_check.first_line(found.error)})",
            action=action("Fix the cause, then run the check again.",
                          "python3 scripts/bootstrap.py check", CREDENTIALS_DOC))
    unchecked = "" if found.organization_checked else "organization secrets not checked"
    note = f" ({unchecked})" if unchecked else ""
    kind, apps = bootstrap_github.classify_credential(found.names)
    if kind == "app":
        return make_step("board_credential", "done", "Board credential: GitHub App configured",
                         details={"secrets": list(bootstrap_github.APP_SECRETS)})
    if kind == "token":
        return make_step(
            "board_credential", "done",
            f"Board credential: {bootstrap_github.TOKEN_SECRET} configured",
            details={"secrets": [bootstrap_github.TOKEN_SECRET]})
    if apps:
        present = apps[0]
        missing = next(name for name in bootstrap_github.APP_SECRETS if name != present)
        return make_step(
            "board_credential", "optional",
            f"Board credential incomplete: only {present} is set{note}",
            action=action(f"Add {missing}, or remove {present}; board sync is skipped until the "
                          "credential is complete.", None, CREDENTIALS_DOC),
            details={"secrets": [present]})
    qualifier = f"optional; {unchecked}" if unchecked else "optional"
    return make_step(
        "board_credential", "optional", f"Board credential not configured ({qualifier})",
        action=action("Add a board credential to sync the board; it is skipped until then.",
                      f"gh secret set {bootstrap_github.TOKEN_SECRET} -R {shlex.quote(repo)}",
                      CREDENTIALS_DOC),
        details={"secrets": []})


def _board_context(root, ctx):
    """The parsed project-context.toml: the default branch's once merged, else the local one."""
    if ctx.bootstrap_merged:
        if ctx.context_text is None:
            raise ValueError(ctx.error or "project-context.toml is not readable")
        return project_context.parse_context(ctx.context_text)
    path = root / CONTEXT_FILE
    if not path.is_file():
        raise ValueError("project-context.toml is missing")
    return project_context.load_context(path)


def detect_board_workflows(root, run, ctx):
    """The board's built-in workflows (FR-009, research D6); never blocks.

    "Pull request linked to issue" off is `manual`; the sub-issues workflow is reported in
    `details.workflows` only. An unreadable board or a missing name is `not_checked`.
    """
    def not_checked(reason, workflows=None):
        return make_step(
            "board_workflows", "not_checked", f"Board workflows could not be checked: {reason}",
            action=action("Fix the cause, then run the check again.",
                          "python3 scripts/bootstrap.py check", SETTINGS_DOC),
            details={"workflows": workflows
                     or dict.fromkeys(bootstrap_github.BOARD_WORKFLOWS)})

    try:
        board = _board_context(root, ctx)["github_project"]
        owner, number = board["owner"], board["number"]
    except (ValueError, OSError, KeyError, TypeError) as error:
        return not_checked(bootstrap_check.first_line(error))
    workflows = bootstrap_github.load_board_workflows(run, owner, number)
    linked = workflows.get(WORKFLOW_PR_LINKED)
    if linked is None:
        return not_checked(f'the "{WORKFLOW_PR_LINKED}" workflow could not be read', workflows)
    if linked is False:
        return make_step(
            "board_workflows", "manual", f'Board workflow "{WORKFLOW_PR_LINKED}" is off',
            action=action(
                f"Open the board's Workflows page (project #{number} of {owner}), enable "
                f'"{WORKFLOW_PR_LINKED}" and set Status to In review.', None, SETTINGS_DOC),
            details={"workflows": workflows})
    return make_step("board_workflows", "done",
                     f'Board workflow "{WORKFLOW_PR_LINKED}" is on; check that it sets Status '
                     "to In review (the API cannot read it)",
                     details={"workflows": workflows})


PR_DOC = "docs/bootstrap.md#one-bootstrap-pull-request"
FAILED_CONCLUSIONS = ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
FAILED_STATES = ("FAILURE", "ERROR")
RERUN = "python3 scripts/bootstrap.py check"


@dataclass
class PullRequests:
    """The bootstrap pull requests (head branch `<issue>-chore-bootstrap`), read once per run."""
    open: dict | None = None
    closed: dict | None = None  # the newest one closed without merging
    merged: dict | None = None  # the newest merged one
    error: str | None = None  # first line of the listing error
    current: str = ""  # the checked-out branch, read once per run


def read_pull_requests(root, run, repo):
    """List the bootstrap pull requests, then view the chosen open one (D2).

    The listing leaves out the status checks; `gh pr view` adds `headRefOid` and
    `statusCheckRollup` for the one open pull request only.
    """
    try:
        listing = json.loads(run(
            ["gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "200", "--json",
             "number,title,state,headRefName,headRefOid,isCrossRepository"]))
        current = run(["git", "branch", "--show-current"], cwd=root).strip()
        found = PullRequests(current=current)
        ours = [item for item in listing
                if isinstance(item, dict) and not item.get("isCrossRepository")  # a fork's branch
                and BRANCH_PATTERN.match(item.get("headRefName") or "")]
        ours.sort(key=lambda item: (item.get("headRefName") != current, -item.get("number", 0)))
        for item in ours:
            if item.get("state") == "OPEN" and found.open is None:
                found.open = item
        for item in sorted(ours, key=lambda item: -item.get("number", 0)):
            if item.get("state") == "CLOSED" and found.closed is None:
                found.closed = item
            elif item.get("state") == "MERGED" and found.merged is None:
                found.merged = item
        if found.open is not None:
            found.open = {**found.open, **json.loads(run(
                ["gh", "pr", "view", str(found.open["number"]), "--repo", repo, "--json",
                 "headRefOid,statusCheckRollup"]))}
    except bootstrap_github.CommandError as error:
        return PullRequests(error=bootstrap_check.first_line(error))
    except (ValueError, TypeError, KeyError):
        return PullRequests(error="the pull request list could not be read")
    return found


def _failing_checks(item):
    """Names of the failed checks, once each, from `statusCheckRollup` (runs and statuses).

    A `CANCELLED` run does not count when a later run of the same name succeeded.
    """
    checks = item.get("statusCheckRollup") or []
    names = []
    for index, check in enumerate(checks):
        name = check.get("name") or check.get("context") or "unnamed check"
        failed = (check.get("conclusion") in FAILED_CONCLUSIONS
                  or check.get("state") in FAILED_STATES)
        if failed and check.get("conclusion") == "CANCELLED" and any(
                (later.get("name") or later.get("context")) == name
                and later.get("conclusion") == "SUCCESS" for later in checks[index + 1:]):
            continue
        if failed and name not in names:
            names.append(name)
    return names


def _ancestry(run, root, commit, ref):
    """True when `ref` contains `commit`, False when not, None when git cannot tell (exit 128:
    `commit` or `ref` is not in this clone)."""
    try:
        run(["git", "merge-base", "--is-ancestor", commit, ref], cwd=root)
    except bootstrap_github.CommandError as error:
        return None if error.returncode == 128 else False
    return True


def _contains(run, root, commit, ref):
    """`_ancestry` is True; "cannot tell" counts as no."""
    return _ancestry(run, root, commit, ref) is True


def _behind_default(root, run, ctx):
    """None when HEAD contains the default branch tip, else (title, action) for the fix (D4)."""
    if ctx.default_tip is None or ctx.default_branch is None:
        return ("the default branch tip could not be read",
                action("Fix the cause, then run the check again.", RERUN, PR_DOC))
    if _contains(run, root, ctx.default_tip, "HEAD"):
        return None
    pull = f"git pull origin {shlex.quote(ctx.default_branch)}"
    try:
        run(["git", "cat-file", "-e", f"{ctx.default_tip}^{{commit}}"], cwd=root)
        command = pull
    except bootstrap_github.CommandError:
        command = f"git fetch origin && {pull}"  # the tip is not in this clone yet
    return (f"the branch is behind {ctx.default_branch}",
            action(f"Bring the branch up to date with {ctx.default_branch}.", command, PR_DOC))


NOT_CHECKED_PREFIX = "Could not be checked: "


def _not_checked(step_id, reason, doc, fix=None, details=None):
    """`fix` replaces the generic action, for a cause another step already knows how to fix."""
    return make_step(step_id, "not_checked", f"{NOT_CHECKED_PREFIX}{reason}",
                     action=dict(fix) if fix else
                     action("Fix the cause, then run the check again.", RERUN, doc),
                     details=details)


def detect_pull_request(root, run, repo, ctx, prs, origin=None):
    """The bootstrap pull request: four ordered states with an open PR, three without (D2, D7)."""
    if ctx.bootstrap_merged:
        return make_step("pull_request", "done",
                         f"Bootstrap is on {ctx.default_branch}; the pull request is merged")
    if prs.error:
        return _not_checked("pull_request", prs.error, PR_DOC)
    try:
        return _pull_request_step(root, run, repo, ctx, prs, origin)
    except bootstrap_github.CommandError as error:
        return _not_checked("pull_request", bootstrap_check.first_line(error), PR_DOC)


def _pull_request_step(root, run, repo, ctx, prs, origin=None):
    current = prs.current
    status = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=root)
    paths = changed_paths(status)
    own = [path for path in paths if path in BOOTSTRAP_FILES]
    others = [path for path in paths if path not in BOOTSTRAP_FILES]
    note = f"; other uncommitted files, not committed: {', '.join(others)}" if others else ""
    if prs.open is None:
        return _without_open_pull_request(root, run, repo, ctx, prs, current, own, note, origin)

    number, branch, oid = prs.open["number"], prs.open["headRefName"], prs.open["headRefOid"]
    details = {"pull_request": {"number": number, "branch": branch}}
    if current != branch:  # (1)
        local = branch in run(["git", "branch", "--format=%(refname:short)"], cwd=root).split()
        left_out, unproven = bool(own), False
        if local and not own:
            held = _ancestry(run, root, branch, oid)  # exit 128 (head not here) counts as no
            unproven = held is None
            left_out = not held
        if left_out:
            why = (f"PR #{number}'s head commit is not in this clone" if unproven
                   else "has local work")
            return make_step(
                "pull_request", "todo",
                f"PR #{number} is open, but {branch} is not checked out and {why}",
                action=action(f"Check out {branch}.", f"git switch {shlex.quote(branch)}",
                              PR_DOC),
                details=details)
        return make_step("pull_request", "done",
                         f"PR #{number} is open on {branch}; nothing local is left out",
                         details=details)
    if not _contains(run, root, oid, "HEAD"):  # (2)
        return make_step(
            "pull_request", "todo",
            f"HEAD does not contain PR #{number}'s head commit; someone else pushed",
            action=action(f"Bring {branch} up to date with GitHub.",
                          f"git pull --rebase origin {shlex.quote(branch)}", PR_DOC),
            details=details)
    ahead = run(["git", "rev-parse", "HEAD"], cwd=root).strip() != oid
    if own or ahead:  # (3)
        parts = ([f"uncommitted: {', '.join(own)}"] if own else []) + (
            ["commits not on GitHub"] if ahead else [])
        title = f"PR #{number} is open; to push: {'; '.join(parts)}{note}"
        return _offer("pull_request", title, "Commit and push the missing parts.", ctx, root,
                      run, details, repo, origin)
    return make_step("pull_request", "done", f"PR #{number} is open and up to date",
                     details=details)


def origin_stop(origin, repo, mismatch, push=False):
    """The stop `start --confirm` would show for `origin` (None without `origin` reads, or when
    it agrees with `repo`): detection and `start` share `OriginReads.stop`."""
    return None if origin is None else origin.stop(repo, mismatch, push=push)


def _stopped(step_id, title, reason, doc, details):
    """A todo that `start --confirm` would stop on, with the stop's own title and command."""
    return make_step(step_id, "todo", f"{title}; {reason['title']}",
                     action=action(reason["text"], reason["command"], doc), details=details)


def _offer(step_id, title, text, ctx, root, run, details, repo=None, origin=None):
    """A todo that `start` performs, unless the branch is behind the default branch (D7) or
    `origin` is not the repository `gh` uses (`start` stops there too)."""
    behind = _behind_default(root, run, ctx)
    if behind:
        return make_step(step_id, "todo", f"{title}; {behind[0]}", action=behind[1],
                         details=details)
    reason = origin_stop(origin, repo, MISMATCH_PR, push=True)
    if reason:
        return _stopped(step_id, title, reason, PR_DOC, details)
    return make_step(step_id, "todo", title, performable=True, action=action(text, None, PR_DOC),
                     details=details)


def _without_open_pull_request(root, run, repo, ctx, prs, current, own, note, origin=None):
    details = {}
    earlier = prs.merged if ctx.bootstrap_merged is False and prs.merged else prs.closed
    title = "No bootstrap pull request yet"
    if earlier:
        state = "merged" if earlier is prs.merged else "closed"
        details["earlier_pull_request"] = {"number": earlier["number"], "state": state}
        what = ("merged without the bootstrap files" if state == "merged"
                else "closed without merging")
        title = f"PR #{earlier['number']} was {what}; a new pull request is needed"
    if not BRANCH_PATTERN.match(current):
        return make_step(
            "pull_request", "todo", f"{title}; no bootstrap branch is checked out",
            action=action("Check out the bootstrap issue's branch (the bootstrap_issue step).",
                          None, PR_DOC), details=details)
    try:
        tip = run(["gh", "api", f"repos/{repo}/branches/{quote(current, safe='/')}",
                   "--jq", ".commit.sha"], cwd=None).strip() or None
    except bootstrap_github.CommandError as error:
        if "HTTP 404" not in str(error):  # any other error is not "not pushed"
            raise  # detect_pull_request turns it into `not_checked`
        tip = None  # the branch is not pushed yet
    if tip and not _contains(run, root, tip, "HEAD"):
        return make_step(
            "pull_request", "todo", f"{title}; HEAD does not contain {current} on GitHub",
            action=action(f"Bring {current} up to date with GitHub.",
                          f"git pull --rebase origin {shlex.quote(current)}", PR_DOC),
            details=details)
    if not own and run(["git", "rev-parse", "HEAD"], cwd=root).strip() == ctx.default_tip:
        return make_step(  # nothing to put in a pull request
            "pull_request", "todo", f"{title}; nothing is committed beyond {ctx.default_branch}",
            action=action("Commit the bootstrap files first, then open the pull request.", None,
                          PR_DOC), details=details)
    return _offer("pull_request", f"{title}{note}", "Push the branch and open the pull request.",
                  ctx, root, run, details, repo, origin)


def _update_default_branch(default):
    """The command that checks out the default branch and fast-forwards it to GitHub's."""
    quoted = shlex.quote(default)
    return f"git switch {quoted} && git pull --ff-only origin {quoted}"


def detect_merge(ctx, prs):
    """Waiting while the bootstrap PR is open; todo when there is none to merge (D2)."""
    if ctx.bootstrap_merged:
        return make_step("merge", "done", f"Bootstrap is on {ctx.default_branch}")
    if prs.error:
        return _not_checked("merge", prs.error, PR_DOC)
    if prs.open:
        number, branch = prs.open["number"], prs.open["headRefName"]
        failing = _failing_checks(prs.open)
        title = f"PR #{number} is open and waiting to be merged"
        if failing:
            title = f"{title}; failing checks: {', '.join(failing)}"
        if ctx.default_branch is None:  # gh could not tell the default branch
            merge_action = action(f"Review and merge PR #{number} on GitHub.", None, PR_DOC)
        else:
            merge_action = action(
                f"Review and merge PR #{number} on GitHub, then run the command below.",
                _update_default_branch(ctx.default_branch), PR_DOC)
        return make_step(
            "merge", "waiting", title,
            action=merge_action,
            details={"pull_request": {"number": number, "branch": branch},
                     "failing_checks": failing})
    if prs.merged and ctx.bootstrap_merged is False:
        return make_step(
            "merge", "todo",
            f"PR #{prs.merged['number']} was merged but the bootstrap files are not on "
            f"{ctx.default_branch}; check what it contained",
            action=action("Check what the merged pull request contained.", None, PR_DOC))
    return make_step(
        "merge", "todo", "There is no open bootstrap pull request to merge",
        action=action("Open the pull request first: see the pull_request step.", None, PR_DOC))


SYNC_DOC = "docs/bootstrap.md#how-the-syncs-behave"
SYNC_SCRIPT = "scripts/sync-roadmap-issues.py"
# What the dispatched workflow reads from GitHub: the roadmap, the specs, the sync's code, the
# workflow itself and the local actions its credential steps use, the board settings
# (`project-context.toml`) and the constitution (the bootstrap state,
# `project_context.bootstrap_state`). A local dry run only equals it when these are committed. Nothing else in `.specify/` or the repository root is read by the run.
SYNC_INPUT_PATHS = ("ROADMAP.md", "specs/", "scripts/", ".github/workflows/",
                    ".github/actions/", CONTEXT_FILE, ".specify/memory/constitution.md")
BLOCKED_OUTPUT = "Roadmap sync left blocked entries unchanged"


def _load_roadmap_sync():
    """The sync script (its file name has a hyphen), loaded once as `sync_roadmap_issues`."""
    module = sys.modules.get("sync_roadmap_issues")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "sync_roadmap_issues", Path(__file__).with_name("sync-roadmap-issues.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["sync_roadmap_issues"] = module
    return module


NO_PENDING = {"pending_changes": None}  # the dry run did not run: nothing is known to be pending


def detect_first_sync(root, run, repo, ctx, skip_project=False, check_origin=False):
    """Every `planned` roadmap entry has its Epic, planning issue and Phase 1 issue (research D5).

    The lookups reuse the sync's own code, which ends the process on bad input; that is caught
    here and becomes `not_checked`. `skip_project` mirrors a missing board credential in the dry
    run behind `details.pending_changes`, which is information only. With `check_origin`, a step
    that would be performable is not when `start` would stop on the `origin` URL (`origin_url_stop`);
    a step that is done also skips the informational dry run when `start` would stop on origin
    (an unreadable origin then leaves it done with `pending_changes` None).
    """
    if ctx.bootstrap_merged is None:
        return _not_checked("first_sync", "the default branch could not be read", SYNC_DOC,
                            details=dict(NO_PENDING))
    if not ctx.bootstrap_merged:
        return make_step(
            "first_sync", "todo", "The first sync waits for the bootstrap to be merged",
            action=action("Merge the bootstrap pull request first (the merge step).", None,
                          SYNC_DOC), details=dict(NO_PENDING))
    try:
        return _first_sync_step(root, run, repo, ctx, skip_project, check_origin)
    except SystemExit as error:
        message = error.code if isinstance(error.code, str) else f"exit code {error.code}"
        return _not_checked("first_sync", bootstrap_check.first_line(message), SYNC_DOC,
                            details=dict(NO_PENDING))
    except (bootstrap_github.CommandError, ValueError) as error:  # incl. JSONDecodeError
        return _not_checked("first_sync", bootstrap_check.first_line(error), SYNC_DOC,
                            details=dict(NO_PENDING))


def _sync_gh_through(run):
    """A stand-in for `project_board.sync.gh` that goes through `run`.

    Only `load_issues` calls it, and it never passes `check=False`, so a failure always ends the
    process the way the sync's own `gh` does. `check` and `env` are unused but kept so the
    signature matches the function it replaces.
    """
    def gh(*args, check=True, env=None):
        try:
            return SimpleNamespace(stdout=run(["gh", *args]), returncode=0, stderr="")
        except bootstrap_github.CommandError as error:
            sys.exit(f"gh {' '.join(args)} failed: {str(error).strip()}")
    return gh


def _missing_issues(run, repo, text):
    """{roadmap ID: [what is missing]} for every planned entry, by the sync's own lookups.

    The sync's `load_issues` runs with its `gh` swapped for one that goes through `run`, so
    every call stays injectable; the swap lasts only for that call.
    """
    entries = roadmap_issues.parse_roadmap(text)
    planned = [entry for entry in entries.values() if entry["status"] == "planned"]
    if not planned:
        return {}, 0
    original = project_board.sync.gh
    project_board.sync.gh = _sync_gh_through(run)
    try:
        issues = project_board.sync.load_issues(f"repos/{repo}")
    finally:
        project_board.sync.gh = original
    roadmap_sync = _load_roadmap_sync()
    missing = {}
    for entry in planned:
        epic, planning = roadmap_issues.index_roadmap_issues(issues, entry["id"])
        phase1 = roadmap_sync.find_phase1(issues, roadmap_issues.feature_path(entry))
        lacking = [name for name, found in (
            ("Epic", epic), ("planning issue", planning.get("spec-plan")),
            ("Phase 1", phase1)) if not found]
        if lacking:
            missing[entry["id"]] = lacking
    return missing, len(planned)


def _first_sync_step(root, run, repo, ctx, skip_project, check_origin=False):
    text = read_file(run, repo, ctx.default_branch, "ROADMAP.md", raw=True)
    if text is None:
        return _not_checked("first_sync", f"ROADMAP.md was not found on {ctx.default_branch}",
                            SYNC_DOC, details=dict(NO_PENDING))
    missing, planned = _missing_issues(run, repo, text)
    # the preview must read what the dispatched workflow will check out (D5)
    head = run(["git", "rev-parse", "HEAD"], cwd=root).strip()
    dirty = changed_paths(run(
        ["git", "status", "--porcelain", "-z", "--", *SYNC_INPUT_PATHS], cwd=root))
    on_tip = ctx.default_tip is not None and head == ctx.default_tip
    pending, dry_error, origin_reason = None, None, None
    skip_dry_run = False
    if check_origin and on_tip and not dirty:
        # as `start` does: a bad origin never runs the sync, whose own error would show the URL
        try:
            origin_reason = origin_url_stop(run, root, repo, MISMATCH_SYNC)
        except bootstrap_github.CommandError:
            if missing:
                raise
            skip_dry_run = True  # done: only the informational dry run is skipped
    if on_tip and not dirty and not origin_reason and not skip_dry_run:
        argv = ["python3", SYNC_SCRIPT] + (["--skip-project"] if skip_project else [])
        try:
            output = run(argv, cwd=root)
            pending = sum(1 for line in output.splitlines() if line.startswith("[dry-run]"))
        except bootstrap_github.CommandError as error:
            # the sync prints its blocked-entry report to stdout, so read both streams
            streams = f"{error.stdout or ''}\n{error}".strip()
            blocked = next((line for line in streams.splitlines() if BLOCKED_OUTPUT in line), None)
            shown = blocked or bootstrap_check.first_line(
                str(error).strip() or streams or "the dry run failed")
            dry_error = (f"{shown} (exit code {error.returncode})"
                         if error.returncode is not None else shown)
    details = {"pending_changes": pending}
    if not missing:
        title = (f"All {planned} planned roadmap entries have their issues" if planned
                 else "No planned roadmap entries: nothing to sync yet")
        return make_step("first_sync", "done", title, details=details)
    count = len(missing)
    title = (f"{count} planned roadmap {'entry lacks' if count == 1 else 'entries lack'} issues: "
             + "; ".join(f"{roadmap_id} ({', '.join(names)})"
                       for roadmap_id, names in missing.items()))
    default = ctx.default_branch
    if ctx.default_tip is None:
        step_action = action(
            f"The tip of {default} could not be read: run the check again.", RERUN, SYNC_DOC)
    elif not on_tip:
        step_action = action(
            f"Check out the tip of {default}: the sync reads exactly what is on GitHub.",
            _update_default_branch(default), SYNC_DOC)
    elif dirty:
        step_action = action(f"Commit or stash these paths first: {', '.join(dirty)}.", None,
                             SYNC_DOC)
    elif dry_error and BLOCKED_OUTPUT in dry_error:
        step_action = action(
            f"The dry run found blocked entries: fix the roadmap entries named above. "
            f"({bootstrap_check.first_line(dry_error)})", None, SYNC_DOC)
    elif dry_error:
        step_action = action(f"The preview failed, so no apply is offered: {dry_error}", None,
                             SYNC_DOC)
    else:
        step_action = action("Preview the sync, then apply it.", None, SYNC_DOC)
    if origin_reason:
        return make_step("first_sync", "todo", f"{title}; {origin_reason['title']}",
                         action=action(origin_reason["text"], origin_reason["command"],
                                       SYNC_DOC), details=details)
    performable = on_tip and not dirty and dry_error is None
    return make_step("first_sync", "todo", title, performable=performable,
                     action=step_action, details=details)


FORMAT_VERSION = 1


def _unchecked_details(step_id):
    """first_sync always reports `pending_changes` (contracts/check-json.md); null when unknown."""
    return dict(NO_PENDING) if step_id == "first_sync" else None


def _guarded(step_id, detect, doc):
    """The step `detect()` returns; an error that escapes it makes the step `not_checked`."""
    try:
        return detect()
    except StepError:
        raise
    except (bootstrap_github.CommandError, ValueError, OSError) as error:
        return _not_checked(step_id, bootstrap_check.first_line(error), doc,
                            details=_unchecked_details(step_id))


def _result(steps):
    """data-model.md "Status report": blocking `todo`, `waiting` or `not_checked` is incomplete."""
    if any(step.blocking and step.state in ("todo", "waiting", "not_checked") for step in steps):
        return "incomplete"
    if any(step.state == "manual" for step in steps):
        return "complete_with_manual_settings"
    return "complete"


def build_report(root, run, remote_default, push_remote_default=None):
    """The `check --json` document: every step recomputed, nothing written (contract check-json).

    Reads the repository context and the pull requests once, and the ruleset warning once for
    `project_context` and `github_settings`. A failing `gh` or `git` makes the steps that need
    it `not_checked`, and a missing `project-context.toml` is that step's own state, so only an
    error outside the steps (for example an unreadable `root`) propagates.
    """
    prerequisites = _guarded(
        "prerequisites", lambda: detect_prerequisites(root, run), "docs/bootstrap.md#prerequisites")
    # `project_scope` is only set once `gh auth status` succeeded; without a usable gh every
    # GitHub step is not_checked for that reason and no further gh call is made
    gh_usable = "project_scope" in (prerequisites.details or {})
    if not gh_usable:
        repo, reason = None, prerequisites.title.removeprefix(NOT_CHECKED_PREFIX)
    else:
        try:
            repo = bootstrap_github.repository_name(run, root, remote_default)
            reason = None
        except ValueError as error:
            repo, reason = None, bootstrap_check.first_line(error)
    if repo is None:
        ctx = RepositoryContext(None, None, None, error=reason)
        warning, prs = None, PullRequests(error=reason)
    else:
        ctx = read_repository_context(repo, run)
        warning, prs = ruleset_warning(repo, run), read_pull_requests(root, run, repo)

    # an unusable gh is fixed by the prerequisites step's action; other causes keep the generic one
    fix = None if gh_usable else prerequisites.action
    # the same reads and the same check `start` makes before it acts on `origin`
    origin = OriginReads(remote_default, push_remote_default)

    def needing_repo(step_id, doc, detect):
        if repo is None:
            return _not_checked(step_id, reason, doc, fix, _unchecked_details(step_id))
        return _guarded(step_id, detect, doc)

    credential = needing_repo("board_credential", CREDENTIALS_DOC,
                              lambda: detect_board_credential(repo, run))
    found = {
        "prerequisites": prerequisites,
        "bootstrap_issue": needing_repo(
            "bootstrap_issue", ISSUE_DOC,
            lambda: detect_bootstrap_issue(root, run, repo, ctx, origin=origin)),
        "project_context": _guarded(
            "project_context", lambda: detect_project_context(root, ctx, warning), CONTEXT_DOC),
        "constitution": _guarded(
            "constitution", lambda: detect_constitution(root, ctx), CONSTITUTION_DOC),
        "roadmap": _guarded("roadmap", lambda: detect_roadmap(root, ctx), ROADMAP_DOC),
        "github_settings": needing_repo(
            "github_settings", SETTINGS_DOC,
            lambda: detect_github_settings(root, run, repo, ctx, warning)),
        "board_credential": credential,
        # needs only the owner and number from the context, not the repository
        "board_workflows": (
            _guarded("board_workflows", lambda: detect_board_workflows(root, run, ctx),
                     SETTINGS_DOC) if gh_usable
            else _not_checked("board_workflows", reason, SETTINGS_DOC, fix)),
        "pull_request": needing_repo(
            "pull_request", PR_DOC,
            lambda: detect_pull_request(root, run, repo, ctx, prs, origin=origin)),
        "merge": needing_repo("merge", PR_DOC, lambda: detect_merge(ctx, prs)),
        # without a configured board credential the sync runs with --skip-project
        "first_sync": needing_repo(
            "first_sync", SYNC_DOC, lambda: detect_first_sync(
                root, run, repo, ctx, skip_project=credential.state != "done",
                check_origin=True)),
    }
    steps = [found[entry.id] for entry in CATALOGUE]
    next_step = next((step.id for step in steps if step.blocking and step.state != "done"), None)
    return {
        "format_version": FORMAT_VERSION,
        "result": _result(steps),
        "next_step": next_step,
        "bootstrap_merged": ctx.bootstrap_merged,
        "default_branch": ctx.default_branch,
        "steps": [asdict(step) for step in steps],
    }
