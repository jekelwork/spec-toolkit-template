"""The `bootstrap.py github` subcommand: preview, apply or check the GitHub settings.

It manages labels, the Projects v2 board (Status options, `Roadmap order`, repository link) and
the default-branch ruleset, and only reports on secrets and variables. Every `gh` call goes
through the injected `run`, so tests never reach GitHub.
"""

import copy
import json
import os
import shlex
import tempfile
from typing import NamedTuple

import display_text
import project_context


class CommandError(Exception):
    """An external command (git, gh) failed or is not installed.

    `stdout` and `returncode` keep what the command printed and how it exited (None when it
    never ran); the message stays the stderr text.
    """

    def __init__(self, message="", stdout="", returncode=None):
        super().__init__(message)
        self.stdout = stdout
        self.returncode = returncode


# (name, color, description): the labels the sync scripts apply; test_bootstrap_github fails when a
# sync script applies a label that is not listed here.
LABELS = (
    ("Epic", "6A0DAD", "Roadmap entry - parent for Phase issues"),
    ("Story", "0075CA", "Phase issue under a roadmap Epic"),
    ("Planning", "D93F0B", "Spec-and-plan planning issue for a roadmap Epic"),
    ("needs-info", "FBCA04", "Roadmap entry needs additional information"),
    ("abandoned", "CFD3D7", "Roadmap entry was abandoned or removed"),
    ("combined-spec-pr-approved", "0E8A16",
     "Human-only: a person approved the combined spec-and-plan PR"),
    ("prerequisites-acknowledged", "B60205",
     "Human-only: a person acknowledged the known roadmap prerequisite blockers"),
)
STATUS_OPTIONS = (
    ("Backlog", "GRAY"), ("Planned", "BLUE"), ("In progress", "YELLOW"),
    ("In review", "PURPLE"), ("Done", "GREEN"),
)
AREAS = ("Labels", "Board", "Ruleset", "Actions")
ORDER_FIELD = "Roadmap order"
RULESET_NAME = "Default branch protection"
GITHUB_ACTIONS_APP_ID = 15368
REQUIRED_CHECKS = (
    "Spec and plan merge before tasks",
    "Validate branch and link issue",
    "Repository tools",
    "Roadmap prerequisites",
)
APP_SECRETS = ("PROJECT_STATUS_APP_ID", "PROJECT_BOARD_APP_PRIVATE_KEY")
TOKEN_SECRET = "PROJECT_BOARD_TOKEN"
CREDENTIALS_GUIDE = "docs/bootstrap.md#board-credentials"
ADMIN_HINT = (
    "writing the ruleset needs repository admin rights and, for a private repository, a plan "
    "that supports rulesets: ask a repository admin to run `bootstrap.py github --apply`, "
    "or make the repository public or upgrade the plan"
)

PLAN_HINT = (
    "rulesets need a public repository or a GitHub plan that supports them (Pro, Team or "
    "Enterprise for a private repository): make the repository public or upgrade the plan"
)

BOARD_QUERY = """
query($login: String!, $number: Int!) {
  repositoryOwner(login: $login) {
    __typename
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id
        url
        repositories(first: 100) { nodes { nameWithOwner } }
        fields(first: 50) {
          nodes {
            ... on ProjectV2Field { id name dataType }
            ... on ProjectV2SingleSelectField {
              id name dataType options { id name color description }
            }
          }
        }
      }
    }
  }
}
"""
BOARD_WORKFLOWS_QUERY = """
query($login: String!, $number: Int!) {
  repositoryOwner(login: $login) {
    ... on ProjectV2Owner {
      projectV2(number: $number) { workflows(first: 20) { nodes { name enabled } } }
    }
  }
}
"""
BOARD_WORKFLOWS = ("Pull request linked to issue", "Auto-add sub-issues to project")
REPOSITORY_ID_QUERY = """
query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }
"""
UPDATE_FIELD = """
mutation($input: UpdateProjectV2FieldInput!) {
  updateProjectV2Field(input: $input) {
    projectV2Field { ... on ProjectV2SingleSelectField { id } }
  }
}
"""
CREATE_FIELD = """
mutation($input: CreateProjectV2FieldInput!) {
  createProjectV2Field(input: $input) { projectV2Field { ... on ProjectV2Field { id } } }
}
"""
LINK_REPOSITORY = """
mutation($input: LinkProjectV2ToRepositoryInput!) {
  linkProjectV2ToRepository(input: $input) { repository { id } }
}
"""


class Plan:
    """Planned changes: one printed line each (grouped by area), the calls that make them, and
    notes (information that is not a change and never counts as drift). Warnings are weakening
    changes that were refused without --allow-weaken: never applied, but drift under --check."""

    def __init__(self):
        self.lines = []
        self.actions = []  # (area, callable)
        self.notes = []
        self.warnings = []

    def add(self, area, text, action=None):
        self.lines.append((area, text))
        if action:
            self.actions.append((area, action))

    def extend(self, other):
        self.lines += other.lines
        self.actions += other.actions
        self.notes += other.notes
        self.warnings += other.warnings


def gh_json(run, argv, body=None, cwd=None):
    """Run gh, optionally sending a JSON body from a temporary file, and parse the JSON reply."""
    path = None
    if body is not None:
        handle, path = tempfile.mkstemp(prefix="bootstrap-github-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(body, file)
        argv = [*argv, "--input", path]
    try:
        output = run(argv, cwd=cwd) if cwd else run(argv)
    finally:
        if path:
            os.unlink(path)
    try:
        return json.loads(output) if output.strip() else {}
    except ValueError as error:
        raise ValueError(
            f"unexpected output from {' '.join(argv[:3])}: {display_text.quoted_line(output.strip()[:80])}"
        ) from error


def graphql(run, query, variables):
    return gh_json(run, ["gh", "api", "graphql"], {"query": query, "variables": variables})


def is_denied(error):
    return "HTTP 403" in str(error) or "HTTP 404" in str(error)


def denied_hint(error):
    """Why a ruleset call was refused: the plan does not support rulesets, else missing rights."""
    text = str(error)
    if "Upgrade to GitHub Pro" in text or "make this repository public" in text:
        return PLAN_HINT
    return ADMIN_HINT


def repository_name(run, root, remote_default):
    """owner/name from `gh repo view` run in the repository root, else from the origin remote."""
    try:
        return gh_json(run, ["gh", "repo", "view", "--json", "nameWithOwner"], cwd=root)[
            "nameWithOwner"]
    except (CommandError, ValueError, KeyError):
        remote = remote_default()
        if remote:
            return "/".join(remote)
        raise ValueError(
            "cannot determine the repository: run inside a clone whose `gh repo view` works "
            "or that has a GitHub `origin` remote"
        ) from None


def plan_labels(repo, run):
    plan = Plan()
    existing = {
        label["name"].casefold(): label  # GitHub label names are case-insensitive
        for label in gh_json(run, ["gh", "label", "list", "--repo", repo, "--limit", "500",
                                    "--json", "name,color,description"])
    }
    for name, color, description in LABELS:
        current = existing.get(name.casefold())
        if current is None:
            text, argv = f"create {name}", ["gh", "label", "create", name]
        elif current["name"] != name:
            text = f"rename {current['name']} → {name}"
            argv = ["gh", "label", "edit", current["name"]]
        elif (current["color"].lower() != color.lower()
              or (current.get("description") or "") != description):
            text, argv = f"update {name}", ["gh", "label", "edit", name]
        else:
            continue
        if argv[2] == "edit":
            argv += ["--name", name]
        argv += ["--repo", repo, "--color", color, "--description", description]
        plan.add("Labels", text, lambda argv=argv: run(argv))
    return plan


def option_input(option):
    return {"id": option["id"], "name": option["name"], "color": option["color"],
            "description": option.get("description") or ""}


def load_board(owner, number, name, run):
    try:
        data = graphql(run, BOARD_QUERY, {"login": owner, "number": number})
    except CommandError as error:
        if "Could not resolve" not in str(error):
            raise ValueError(f"cannot read the board: {error}{scope_hint(error)}") from error
        data = {}
    project = ((data.get("data") or {}).get("repositoryOwner") or {}).get("projectV2")
    if not project:
        # The message is escaped where it is printed, and a printed command is never escaped (an
        # escaped one would not run as shown): a name with an unsafe character or a backslash
        # (which the escaping doubles) gets no command.
        command = (f"`gh project create --owner {shlex.quote(owner)} --title {shlex.quote(name)}`"
                   if not (display_text.has_unsafe(name) or "\\" in name) else
                   "a project titled like project.name (its characters cannot be shown safely, "
                   "so run `gh project create` by hand)")
        raise ValueError(
            f"no GitHub Project #{number} found for {owner}; create one with {command} and "
            "set github_project.number in project-context.toml to its number"
        )
    return project


def load_board_workflows(run, owner, number):
    """{name: True|False|None} for the board workflows the toolkit needs; None means unknown."""
    result = dict.fromkeys(BOARD_WORKFLOWS)
    try:
        data = graphql(run, BOARD_WORKFLOWS_QUERY, {"login": owner, "number": number})
        project = ((data.get("data") or {}).get("repositoryOwner") or {}).get("projectV2")
        nodes = (((project or {}).get("workflows") or {}).get("nodes")) or []
        for node in nodes:
            if node.get("name") in result and isinstance(node.get("enabled"), bool):
                result[node["name"]] = node["enabled"]
    except (CommandError, ValueError, AttributeError, TypeError):
        # D6: any error, including a malformed node, makes the whole result None on purpose.
        return dict.fromkeys(BOARD_WORKFLOWS)
    return result


def scope_hint(error):
    return " (run `gh auth refresh -s project`)" if "scope" in str(error) else ""


def plan_status_options(status, plan, run):
    """Keep every option (and id); rename case variants and Todo; append what is missing."""
    options = status["options"]
    used = set()

    def take(name):
        for same in (lambda n: n == name, lambda n: n.casefold() == name.casefold()):
            for index, option in enumerate(options):
                if index not in used and same(option["name"]):
                    used.add(index)
                    return option
        return None

    managed, changed = [], False
    matches = {name: take(name) for name, _ in STATUS_OPTIONS}
    if matches["Backlog"] is None:
        matches["Backlog"] = take("Todo")  # the default option of a fresh board
    for name, color in STATUS_OPTIONS:
        option = matches[name]
        if option is None:
            plan.lines.append(("Board", f"add Status option {name}"))
            managed.append({"name": name, "color": color, "description": ""})
            changed = True
            continue
        if option["name"] != name:
            plan.lines.append(("Board", f"rename Status option {option['name']} → {name}"))
            changed = True
        managed.append({**option_input(option), "name": name})
    unmanaged = [option for index, option in enumerate(options) if index not in used]
    for option in unmanaged:
        if option["name"].casefold() in {name.casefold() for name, _ in STATUS_OPTIONS}:
            raise ValueError(
                f"the board has two Status options named {display_text.quoted_line(option['name'])} apart from case; "
                "delete or rename the extra one on the board, then run this command again"
            )
        plan.notes.append(f"Status option {option['name']} is not managed and stays as it is")
    if changed:
        plan.actions.append(("Board", lambda: graphql(run, UPDATE_FIELD, {"input": {
            "fieldId": status["id"],
            "singleSelectOptions": managed + [option_input(o) for o in unmanaged]}})))


def plan_board(repo, owner, number, name, run):
    project = load_board(owner, number, name, run)
    plan = Plan()
    fields = {f["name"]: f for f in project["fields"]["nodes"] if f and f.get("name")}
    status = fields.get("Status")
    if status and status.get("options") is not None:
        plan_status_options(status, plan, run)
    else:
        plan.add("Board", "create field Status with the options "
                 + ", ".join(option for option, _ in STATUS_OPTIONS),
                 lambda: graphql(run, CREATE_FIELD, {"input": {
                     "projectId": project["id"], "dataType": "SINGLE_SELECT", "name": "Status",
                     "singleSelectOptions": [{"name": n, "color": c, "description": ""}
                                             for n, c in STATUS_OPTIONS]}}))
    order = fields.get(ORDER_FIELD)
    if order and order.get("dataType") != "NUMBER":
        raise ValueError(
            f"the board field {ORDER_FIELD!r} is of type {order.get('dataType')}, not NUMBER; "
            "delete or rename it in the board settings, then run this command again"
        )
    if order is None:
        plan.add("Board", f"create field {ORDER_FIELD}",
                 lambda: graphql(run, CREATE_FIELD, {"input": {
                     "projectId": project["id"], "dataType": "NUMBER", "name": ORDER_FIELD}}))
    linked = {node["nameWithOwner"].casefold() for node in project["repositories"]["nodes"]}
    if repo.casefold() not in linked:
        def link():
            repo_owner, repo_name = repo.split("/", 1)
            data = graphql(run, REPOSITORY_ID_QUERY, {"owner": repo_owner, "name": repo_name})
            graphql(run, LINK_REPOSITORY, {"input": {
                "projectId": project["id"], "repositoryId": data["data"]["repository"]["id"]}})

        plan.add("Board", f"link to {repo}", link)
    return plan, project["url"]


def new_ruleset(approvals):
    return {
        "name": RULESET_NAME, "target": "branch", "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            new_pull_request_rule(approvals),
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "do_not_enforce_on_create": False,
                "required_status_checks": [
                    {"context": name, "integration_id": GITHUB_ACTIONS_APP_ID}
                    for name in REQUIRED_CHECKS],
            }},
        ],
        "bypass_actors": [],
    }


def new_pull_request_rule(approvals):
    return {"type": "pull_request", "parameters": {
        "required_approving_review_count": approvals,
        "dismiss_stale_reviews_on_push": approvals >= 2,
        "required_reviewers": [],
        "require_code_owner_review": False,
        "dismissal_restriction": {"enabled": False, "allowed_actors": []},
        "require_last_push_approval": approvals >= 1,
        "required_review_thread_resolution": False,
        "require_extra_approval_for_unattributed_changes": False,
        "allowed_merge_methods": ["merge", "squash", "rebase"],
    }}


def is_weakening(key, have, want):
    """True when changing a pull_request parameter from have to want lowers the protection."""
    if key == "required_approving_review_count":
        return isinstance(have, int) and want < have
    return have is True and want is False


def merge_ruleset(current, approvals, plan, allow_weaken=False):
    """Return current with only the managed values changed; add one plan line per change.

    A change that lowers the protection is only planned with `allow_weaken`; otherwise it is
    left as it is and reported as a warning."""
    merged = copy.deepcopy(current)
    wanted = new_ruleset(approvals)
    if merged.get("enforcement") != "active":
        plan.add("Ruleset", f"enforcement {merged.get('enforcement')} → active")
        merged["enforcement"] = "active"
    conditions = merged.get("conditions") or {}
    ref_name = conditions.get("ref_name") or {}
    include = list(ref_name.get("include") or [])
    if "~DEFAULT_BRANCH" not in include:
        plan.add("Ruleset", "include ~DEFAULT_BRANCH")
        include.append("~DEFAULT_BRANCH")
    merged["conditions"] = {**conditions, "ref_name": {
        **ref_name, "include": include, "exclude": ref_name.get("exclude") or []}}
    rules = {rule["type"]: rule for rule in merged["rules"]}
    for wanted_rule in wanted["rules"]:
        kind = wanted_rule["type"]
        if kind not in rules:
            plan.add("Ruleset", f"add rule {kind}")
            merged["rules"].append(wanted_rule)
            continue
        parameters = rules[kind].setdefault("parameters", {})
        if kind == "pull_request":
            for key, label in (("required_approving_review_count", "required approvals"),
                               ("require_last_push_approval", "last-push approval"),
                               ("dismiss_stale_reviews_on_push", "dismiss stale reviews on push")):
                have = parameters.get(key)
                want = wanted_rule["parameters"][key]
                if key == "dismiss_stale_reviews_on_push":
                    # Up to one approval keeps whatever is set; two or more dismiss stale approvals.
                    want = True if approvals >= 2 else bool(have)
                    have = bool(have)
                # A missing dismissal key means false, so it is no drift while false is wanted.
                if have == want and (
                    parameters.get(key) is not None or key == "dismiss_stale_reviews_on_push"
                ):
                    continue
                change = f"{label} {format_value(have)} → {format_value(want)}"
                if is_weakening(key, have, want) and not allow_weaken:
                    plan.warnings.append(f"Warning: {change} (rerun with --allow-weaken to apply)")
                    continue
                plan.add("Ruleset", change)
                parameters[key] = want
        elif kind == "required_status_checks":
            checks = parameters.setdefault("required_status_checks", [])
            have = {check["context"] for check in checks}
            for check in wanted_rule["parameters"]["required_status_checks"]:
                if check["context"] not in have:
                    plan.add("Ruleset", f"add required check {check['context']}")
                    checks.append(check)
    return merged


def format_value(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def ruleset_body(ruleset):
    keys = ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")
    return {key: ruleset[key] for key in keys if key in ruleset}


def plan_ruleset(repo, approvals, run, allow_weaken=False):
    plan = Plan()
    try:
        # Only the repository's own rulesets: an organization ruleset of the same name is not ours.
        listed = gh_json(
            run, ["gh", "api", f"repos/{repo}/rulesets?includes_parents=false&per_page=100"])
        found = next((r for r in listed if r.get("name") == RULESET_NAME
                      and r.get("source_type", "Repository") == "Repository"), None)
        current = None
        if found:
            current = gh_json(run, ["gh", "api", f"repos/{repo}/rulesets/{found['id']}"])
    except CommandError as error:
        if "HTTP 404" in str(error):
            raise ValueError(
                f"repository or ruleset not found ({error}); check `gh repo view`") from error
        if is_denied(error):
            raise ValueError(f"cannot read the rulesets ({error}); {denied_hint(error)}") from error
        raise
    if current is None:
        text = f"create {RULESET_NAME} (required approvals {approvals})"
        body, method, path = new_ruleset(approvals), "POST", f"repos/{repo}/rulesets"
    else:
        merged = merge_ruleset(current, approvals, plan, allow_weaken)
        if current.get("bypass_actors"):
            plan.notes.append(
                f"{RULESET_NAME} has bypass_actors ({len(current['bypass_actors'])}); anyone or any "
                "App listed there can merge around the review requirement"
            )
        if not plan.lines:
            return plan
        body, method = ruleset_body(merged), "PUT"
        path = f"repos/{repo}/rulesets/{found['id']}"

    def write():
        try:
            gh_json(run, ["gh", "api", "-X", method, path], body)
        except CommandError as error:
            if is_denied(error):
                raise ValueError(f"{error}; {denied_hint(error)}") from error
            raise

    if current is None:
        plan.add("Ruleset", text)
        if approvals == 0:
            plan.notes.append(
                "the new ruleset requires no independent approval for the main branch "
                "(required_approvals = 0)"
            )
    plan.actions.append(("Ruleset", write))
    return plan


FORK_APPROVAL_PATH = "actions/permissions/fork-pr-contributor-approval"
FORK_APPROVAL_WANTED = "all_external_contributors"
# Weakest to strictest; a value not listed (one GitHub adds later) is never changed.
FORK_APPROVAL_POLICIES = (
    "first_time_contributors_new_to_github",
    "first_time_contributors",
    FORK_APPROVAL_WANTED,
)
FORK_APPROVAL_HINT = (
    "this setting needs repository admin rights: ask a repository admin to run "
    "`bootstrap.py github --apply`"
)


FORK_APPROVAL_PRIVATE_NOTE = (
    "fork PR approval applies to public repositories only (GitHub manages fork pull request "
    "workflows of private repositories separately); not checked"
)


def plan_fork_approval(repo, run):
    """Require approval before workflows run for fork pull requests from any outside contributor.

    The PR workflows run the PR's code on `pull_request`; GitHub's default only asks for first-time
    contributors. A fork run of `pull_request` gets a read-only token and no secrets, so this approval
    is about running untrusted code on runners, not about secret access. Never weakens: a stricter
    or unknown policy is left alone. Private repositories have no such policy (GitHub answers 422).
    """
    plan = Plan()
    path = f"repos/{repo}/{FORK_APPROVAL_PATH}"
    if gh_json(run, ["gh", "api", f"repos/{repo}", "--jq", ".private"]) is True:
        plan.notes.append(FORK_APPROVAL_PRIVATE_NOTE)
        return plan
    try:
        current = gh_json(run, ["gh", "api", path]).get("approval_policy")
    except CommandError as error:
        if "not allowed for private repositories" in str(error):
            plan.notes.append(FORK_APPROVAL_PRIVATE_NOTE)
            return plan
        if is_denied(error):
            raise ValueError(
                f"cannot read the fork pull request approval policy ({error}); "
                f"{FORK_APPROVAL_HINT}"
            ) from error
        raise
    if current == FORK_APPROVAL_WANTED:
        return plan
    if current not in FORK_APPROVAL_POLICIES:
        plan.notes.append(
            f"fork PR approval policy {display_text.quoted_line(current)} is not one this tool knows; left unchanged"
        )
        return plan

    def write():
        try:
            gh_json(run, ["gh", "api", "-X", "PUT", path], {"approval_policy": FORK_APPROVAL_WANTED})
        except CommandError as error:
            if is_denied(error):
                raise ValueError(f"{error}; {FORK_APPROVAL_HINT}") from error
            raise

    plan.add("Actions", f"fork PR approval {current} → {FORK_APPROVAL_WANTED}", write)
    return plan


def organization_names(repo, kind, run):
    """Names of organization-level secrets or variables the repository can use; None on failure."""
    try:
        data = gh_json(run, ["gh", "api", f"repos/{repo}/actions/organization-{kind}?per_page=100"])
        return {item["name"] for item in data.get(kind, [])}
    except (CommandError, ValueError, KeyError, TypeError, AttributeError):
        return None


def report_credentials(repo, run):
    """Information and warnings only; nothing here is a change and values are never read."""
    lines = []
    found = read_credential_secrets(repo, run)
    organization_unchecked = found.error is None and not found.organization_checked
    variables = None
    try:
        repository = {n["name"] for n in gh_json(
            run, ["gh", "variable", "list", "--repo", repo, "--json", "name"])}
    except (CommandError, ValueError) as error:
        lines.append(f"Warning: cannot read variable names ({error})")
    except (KeyError, TypeError):
        lines.append("Warning: cannot read variable names (the variable list could not be read)")
    else:
        organization = organization_names(repo, "variables", run)
        organization_unchecked = organization_unchecked or organization is None
        variables = repository | (organization or set())
    if found.error:
        lines.insert(0, f"Warning: cannot read secret names ({found.error})")
    if organization_unchecked:
        lines.append("Note: organization-level secrets and variables were not checked "
                     "(not readable, or the repository is not in an organization)")
    if not found.error:
        kind, app = classify_credential(found.names)
        if kind == "app":
            lines.append("Board credential: GitHub App configured")
        elif kind == "token":
            lines.append(f"Board credential: {TOKEN_SECRET} configured")
        else:
            lines.append("Board credential: not configured; board sync is skipped until it is "
                         f"(see {CREDENTIALS_GUIDE})")
        if app and len(app) < len(APP_SECRETS):
            lines.append(f"Warning: only {app[0]} is set; the GitHub App needs both "
                         f"{' and '.join(APP_SECRETS)} (see {CREDENTIALS_GUIDE})")
    if variables is not None and "PHASE_SYNC_AUTO_APPLY" in variables:
        lines.append("PHASE_SYNC_AUTO_APPLY: set (Phase issues sync without a dry-run review)")
    return lines


def classify_credential(names):
    """('app' | 'token' | 'none', the App secrets present) for a set of secret names.

    The one place that decides which board credential wins: a complete App, then the token.
    """
    app = [name for name in APP_SECRETS if name in names]
    if len(app) == len(APP_SECRETS):
        return "app", app
    return ("token" if TOKEN_SECRET in names else "none"), app


class CredentialSecrets(NamedTuple):
    """The toolkit's board credential secrets found by name (never a value)."""
    names: tuple  # only names in APP_SECRETS or TOKEN_SECRET
    error: str | None  # why the repository's secret names could not be read, or None
    organization_checked: bool  # False when organization-level secrets were not read (or not tried)


def read_credential_secrets(repo, run):
    """The one read of the repository and organization secret names, as structured data."""
    try:
        repository = {n["name"] for n in gh_json(
            run, ["gh", "secret", "list", "--repo", repo, "--json", "name"])}
    except (CommandError, ValueError) as error:
        return CredentialSecrets((), str(error), False)
    except (KeyError, TypeError):
        return CredentialSecrets((), "the secret list could not be read", False)
    organization = organization_names(repo, "secrets", run)
    wanted = (*APP_SECRETS, TOKEN_SECRET)
    present = repository | (organization or set())
    return CredentialSecrets(tuple(name for name in wanted if name in present), None,
                             organization is not None)


def apply_plan(plan):
    """Apply area by area; on failure say which areas were applied and which one failed."""
    applied = []
    for area in AREAS:
        actions = [action for action_area, action in plan.actions if action_area == area]
        try:
            for action in actions:
                action()
        except (CommandError, ValueError) as error:
            done = ", ".join(applied) or "nothing"
            hint = scope_hint(error) if area == "Board" else ""
            raise ValueError(
                f"Applied completely: {done}; failed: {area}: {error}{hint}"
                " (a failed area may be partly applied; fix the cause and run the command again:"
                " it only changes what still differs)"
            ) from error
        if actions:
            applied.append(area)


def plan_settings(repo, context, run, allow_weaken=False):
    """Plan labels, board, ruleset and fork approval; return (plan, board url). Reads only."""
    project_context.validate_repository_owner(context, repo.split("/", 1)[0])
    board = context["github_project"]
    plan = plan_labels(repo, run)
    board_plan, board_url = plan_board(
        repo, board["owner"], board["number"], context["project"]["name"], run)
    plan.extend(board_plan)
    plan.extend(plan_ruleset(repo, project_context.required_approvals(context), run,
                             allow_weaken))
    plan.extend(plan_fork_approval(repo, run))
    return plan, board_url


def run_github(args, root, run, remote_default, out=print, hint=True, plan_out=None):
    """Return the exit code: 0 clean or applied, 1 drift under --check (errors raise ValueError).

    `hint=False` leaves out the "run with --apply" advice of a preview, for a caller that applies
    the plan itself (`bootstrap.py start`). `plan_out(plan)` is called with the plan as soon as it
    is read, so that caller can use the same read for what it shows and counts.

    Every line goes through `out` escaped (`display_text.printable`): the lines hold board option
    names, the board URL, ruleset values and `gh` error text, which GitHub controls. `out`
    receives them escaped once, so a caller must not escape them again."""
    write = out

    def emit(text=""):
        write(display_text.printable(str(text)))

    path = root / project_context.DEFAULT_CONFIG.name
    if not path.exists():
        raise ValueError(project_context.missing_context_message(path))
    context = project_context.load_context(path)
    repo = repository_name(run, root, remote_default)
    plan, board_url = plan_settings(repo, context, run, getattr(args, "allow_weaken", False))
    if plan_out is not None:
        plan_out(plan)

    emit(f"Repository: {repo} · Board: {board_url}")
    for area in AREAS:
        for line_area, text in plan.lines:
            if line_area == area:
                emit(f"{area}: {text}")
    for warning in plan.warnings:
        emit(warning)
    if not plan.lines and not plan.warnings:
        emit("Nothing to change: labels, board, ruleset and fork PR approval match.")
    for note in plan.notes:
        emit(f"Note: {note}")
    manual = (f"Manual step (the API cannot read or set it): in {board_url} open Workflows and "
              'check that "Pull request linked to issue" is enabled with Status = In review; '
              "set it if not.")
    # The step names a Status option that exists only after the apply, so a preview with changes
    # leaves it out and --apply prints it last (research D18).
    # Ruleset warnings are not changes the apply makes, so they do not hide it.
    emit("")
    if args.check or (not plan.lines and not args.apply):
        emit(manual)
    for line in report_credentials(repo, run):
        emit(line)

    if args.check:
        return 1 if plan.lines or plan.warnings else 0
    if not args.apply:
        if plan.lines and hint:
            emit("")
            emit("Preview only; run with --apply to make these changes.")
        return 0
    apply_plan(plan)
    if plan.lines:
        emit("")
        emit(f"Applied {len(plan.lines)} change(s).")
    emit("")
    emit(manual)
    return 0
