"""GitHub Project (v2) board helpers for issue syncs.

Calls `gh` through sync-task-issues.py's helper, so tests patch `project_board.sync.gh`.
"""

import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import time

SCRIPTS = Path(__file__).resolve().parent


def _load_sync():
    """Load sync-task-issues.py once per process so every importer shares one `gh`."""
    module = sys.modules.get("sync_task_issues")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "sync_task_issues", SCRIPTS / "sync-task-issues.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["sync_task_issues"] = module
        spec.loader.exec_module(module)
    return module


sync = _load_sync()


def project_gh(*args, **kwargs):
    token = os.environ.get("BOARD_GH_TOKEN")
    if token:
        return sync.gh(*args, env=dict(sync.ENV, GH_TOKEN=token), **kwargs)
    return sync.gh(*args, **kwargs)


# gh exits non-zero and GitHub's GraphQL error text is the only signal that the item already
# exists in the project.
ALREADY_IN_PROJECT = re.compile(r"content already exists in this project", re.IGNORECASE)
LOOKUP_ATTEMPTS = 3
LOOKUP_DELAY_SECONDS = 3

PROJECT_ITEMS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 50) {
        nodes {
          id
          project { number owner { ... on Organization { login } ... on User { login } } }
          status: fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          order: fieldValueByName(name: "Roadmap order") {
            ... on ProjectV2ItemFieldNumberValue { number }
          }
        }
      }
    }
  }
}
"""

def project_item(owner, repo, number, project_owner, project_number):
    """Return (item id, status name or None), or None when the issue is not in the project."""
    item = project_item_details(owner, repo, number, project_owner, project_number)
    return item[:2] if item else None


def project_item_details(owner, repo, number, project_owner, project_number):
    """Return (item id, status name or None, roadmap order or None) for the project item."""
    result = project_gh(
        "api",
        "graphql",
        "-f",
        f"query={PROJECT_ITEMS_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"repo={repo}",
        "-F",
        f"number={number}",
    )
    nodes = json.loads(result.stdout)["data"]["repository"]["issue"]["projectItems"]["nodes"]
    for node in nodes:
        project = node.get("project") or {}
        login = ((project.get("owner") or {}).get("login") or "").lower()
        if project.get("number") == project_number and login == project_owner.lower():
            return (
                node["id"],
                (node.get("status") or {}).get("name"),
                (node.get("order") or {}).get("number"),
            )
    return None


ISSUE_ID_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) { issue(number: $number) { id } }
}
"""

ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) { item { id } }
}
"""

# first: 100 covers the 50-field Project limit; single-select options are not paginated.
PROJECT_FIELDS = """
  id
  fields(first: 100) {
    nodes {
      ... on ProjectV2Field { id name }
      ... on ProjectV2SingleSelectField { id name options { id name } }
    }
  }
"""

# User and organization are queried separately: gh's own owner lookup combines them, and the
# organization half fails for a token without `read:org` (not needed for a user-owned board).
OWNER_KINDS = ("user", "organization")


def project_lookup_query(kind):
    return (
        "query($owner: String!, $number: Int!) {\n"
        f"  {kind}(login: $owner) {{ projectV2(number: $number) {{{PROJECT_FIELDS}}} }}\n"
        "}"
    )


def parse_body(stdout):
    """Parse gh's JSON stdout; anything that is not a JSON object counts as an empty body."""
    try:
        body = json.loads(stdout or "{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def error_cause(body, result):
    """Name the real cause of a failed call: an HTTP error message, GraphQL errors, or stderr."""
    errors = [e for e in body.get("errors") or [] if isinstance(e, dict)]
    return body.get("message") or "; ".join(
        e.get("message", "") for e in errors if e.get("type") != "NOT_FOUND"
    ) or (result.stderr or "").strip()


def find_project(owner, number):
    """Return (project node id, fields) for the user- or organization-owned project."""
    problems = []
    # User first: an organization lookup is unverified for a token without `read:org`.
    for kind in OWNER_KINDS:
        result = project_gh(
            "api", "graphql", "-f", f"query={project_lookup_query(kind)}",
            "-f", f"owner={owner}", "-F", f"number={number}", check=False,
        )
        body = parse_body(result.stdout)
        project = ((body.get("data") or {}).get(kind) or {}).get("projectV2")
        if project:
            return project["id"], [f for f in project["fields"]["nodes"] if f]
        if result.returncode != 0:
            errors = [e for e in body.get("errors") or [] if isinstance(e, dict)]
            if not errors or any(e.get("type") != "NOT_FOUND" for e in errors):
                # HTTP errors (bad token, SSO, rate limit) arrive as {"message": ...} on stdout.
                problems.append(f"{kind}: {error_cause(body, result)}")
    detail = f" ({'; '.join(p for p in problems if p)})" if any(problems) else ""
    sys.exit(f"No project {number} found for user or organization {owner!r}{detail}")


def _sleep(seconds):
    time.sleep(seconds)


def add_issue_to_project(owner, repo, number, project):
    """Add the issue to the Project (its id is looked up once and cached) and return the item id."""
    project_id, _ = project.project_fields()
    issue = project_gh(
        "api", "graphql", "-f", f"query={ISSUE_ID_QUERY}",
        "-f", f"owner={owner}", "-f", f"repo={repo}", "-F", f"number={number}", check=False,
    )
    body = parse_body(issue.stdout)
    found = ((body.get("data") or {}).get("repository") or {}).get("issue")
    if not found:
        errors = [e for e in body.get("errors") or [] if isinstance(e, dict)]
        if issue.returncode == 0 or (errors and all(e.get("type") == "NOT_FOUND" for e in errors)):
            if any(e.get("path") == ["repository"] for e in errors):
                sys.exit(
                    f"Repository {owner}/{repo} was not found or is not visible to the token used "
                    "for the board (a classic PAT needs `repo` for a private repository)"
                )
            sys.exit(f"Issue #{number} not found in {owner}/{repo}")
        sys.exit(f"Cannot look up issue #{number} in {owner}/{repo}: {error_cause(body, issue)}")
    added = project_gh(
        "api", "graphql", "-f", f"query={ADD_ITEM_MUTATION}",
        "-f", f"projectId={project_id}", "-f", f"contentId={found['id']}", check=False,
    )
    if added.returncode != 0:
        # GitHub's built-in "Auto-add sub-issues to project" workflow may have added the issue
        # already; that is the goal, so return its item. Any other refusal still fails.
        cause = error_cause(parse_body(added.stdout), added)
        # The match is on text because there is no error code to test for.
        if ALREADY_IN_PROJECT.search(f"{cause}\n{added.stderr}"):
            for attempt in range(LOOKUP_ATTEMPTS):
                if attempt:
                    _sleep(LOOKUP_DELAY_SECONDS)
                item = project_item(owner, repo, number, project.owner, project.number)
                if item:
                    return item[0]
            sys.exit(
                f"GitHub reports issue #{number} is already in project "
                f"{project.owner}/{project.number}, but it is not visible yet; rerun the sync"
            )
        sys.exit(f"Cannot add issue #{number} to project {project.owner}/{project.number}: {cause}")
    return json.loads(added.stdout)["data"]["addProjectV2ItemById"]["item"]["id"]


def ensure_removed(owner, repo, project, issue, apply, say):
    """Take the issue's item off the project; True when a change was made (or would be)."""
    item = project_item(owner, repo, issue["number"], project.owner, project.number)
    if not item:
        return False
    if not apply:
        say(f"remove #{issue['number']} from project {project.owner}/{project.number}")
        return True
    project.remove_item(item[0])
    print(f"#{issue['number']} removed from project {project.owner}/{project.number}")
    return True


def ensure_planned(owner, repo, project, issue, apply, say):
    """Put an open issue on the project with Status Planned.

    Backlog and unset statuses are initial states; later statuses belong to
    sync-project-status.py.
    Returns True when a change was made (or, in a dry run, would be made).
    """
    if not issue or issue.get("state") == "closed":
        return False
    item = project_item_details(owner, repo, issue["number"], project.owner, project.number)
    if item and item[1] not in {None, "Backlog"}:
        return False
    if not apply:
        say(
            f"set #{issue['number']} Status to Planned in project {project.owner}/{project.number}"
            if item
            else f"add #{issue['number']} to project {project.owner}/{project.number} as Planned"
        )
        return True
    item_id = item[0] if item else add_issue_to_project(owner, repo, issue["number"], project)
    project.set_status(item_id, "Planned")
    print(f"#{issue['number']} set to Planned in project {project.owner}/{project.number}")
    return True


SET_OPTION_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: { singleSelectOptionId: $optionId }
  }) { projectV2Item { id } }
}
"""

DELETE_ITEM_MUTATION = """
mutation($projectId: ID!, $itemId: ID!) {
  deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) { deletedItemId }
}
"""

SET_NUMBER_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: Float!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: { number: $value }
  }) { projectV2Item { id } }
}
"""


class Project:
    def __init__(self, owner, number):
        self.owner = owner
        self.number = number
        self._ids = None
        self._number_field = None
        self._number_field_loaded = False
        self._project_fields = None

    def project_fields(self):
        if self._project_fields is None:
            project_id, fields = find_project(self.owner, self.number)
            self._project_fields = project_id, fields
        return self._project_fields

    def status_ids(self):
        if self._ids is None:
            project_id, fields = self.project_fields()
            status = next((field for field in fields if field.get("name") == "Status"), None)
            if not status:
                sys.exit(f"Project {self.owner}/{self.number} has no Status field")
            options = {option["name"]: option["id"] for option in status.get("options", [])}
            self._ids = project_id, status["id"], options
        return self._ids

    def set_status(self, item_id, name):
        project_id, field_id, options = self.status_ids()
        if name not in options:
            sys.exit(f"Project {self.owner}/{self.number} has no Status option {name!r}")
        self._write(
            SET_OPTION_MUTATION, project_id, item_id, field_id, "-f", "optionId", options[name]
        )

    def _write(self, mutation, project_id, item_id, field_id, flag, value_name, value):
        """Update one item field with GraphQL: any `gh project` command demands `read:org` and
        `read:discussion`.

        `flag` is `-f` (always a string, for option ids: `-F` turns an all-digit id into an
        integer) or `-F` (typed, for the Float number: `-f` would send it as a string).
        """
        result = project_gh(
            "api", "graphql", "-f", f"query={mutation}",
            "-f", f"projectId={project_id}", "-f", f"itemId={item_id}",
            "-f", f"fieldId={field_id}", flag, f"{value_name}={value}", check=False,
        )
        if result.returncode != 0:
            cause = error_cause(parse_body(result.stdout), result)
            sys.exit(f"Cannot update item {item_id}: {cause}")

    def remove_item(self, item_id):
        """Delete the item from the project (the issue itself is untouched)."""
        project_id, _ = self.project_fields()
        result = project_gh(
            "api", "graphql", "-f", f"query={DELETE_ITEM_MUTATION}",
            "-f", f"projectId={project_id}", "-f", f"itemId={item_id}", check=False,
        )
        if result.returncode != 0:
            cause = error_cause(parse_body(result.stdout), result)
            sys.exit(f"Cannot remove item {item_id}: {cause}")

    def number_field(self):
        if not self._number_field_loaded:
            project_id, fields = self.project_fields()
            field = next((field for field in fields if field.get("name") == "Roadmap order"), None)
            self._number_field = (project_id, field["id"]) if field else None
            self._number_field_loaded = True
        return self._number_field

    def set_number(self, item_id, value):
        field = self.number_field()
        if not field:
            sys.exit(f"Project {self.owner}/{self.number} has no Roadmap order field")
        project_id, field_id = field
        self._write(SET_NUMBER_MUTATION, project_id, item_id, field_id, "-F", "value", value)
