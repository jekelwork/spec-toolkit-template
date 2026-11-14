"""In-memory stand-in for the `gh` CLI, used by the issue-sync unit tests.

It understands only the `gh` invocations made by the issue-sync scripts, records every call,
and fails loudly on anything else so a test can never reach the real GitHub API. It also
provides a fixture ROADMAP.md with a made-up roadmap ID, so the tests never depend on live
repository data.
"""

import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

OWNER, REPO = "o", "r"
API = f"repos/{OWNER}/{REPO}"

# Made-up roadmap entry (ID 900) so the tests never depend on the live ROADMAP.md.
FIXTURE_ROADMAP = """# Roadmap

## Entries

### 900 — Issue Tracking (API)  [status: planned]

- **Description:** Resolvers to create and read tracked issues.
- **Outcome:** A member can manage tracked issues through the API.
- **Scope (in):** issue fields, resolvers.
- **Scope (out):** issue UI (919).
- **Depends on:** 899
- **Notes:** fixture entry.
"""


def write_fixture_repo(root):
    """Write the fixture ROADMAP.md under root; tests create their own specs/ trees."""
    root = Path(root)
    (root / "ROADMAP.md").write_text(FIXTURE_ROADMAP, encoding="utf-8")
    return root


def is_mutating(args):
    """Return True when a recorded gh call would change GitHub state."""
    args = list(args)
    if args[:1] == ["api"]:
        if "-X" in args and args[args.index("-X") + 1].upper() != "GET":
            return True
        query = next((arg for arg in args if arg.startswith("query=")), "")
        return query[len("query="):].lstrip().startswith("mutation")
    if args[:1] == ["issue"]:
        return args[1] in {"create", "edit", "close", "reopen", "develop", "delete", "comment"}
    if args[:1] == ["project"]:
        return args[1] not in {"view", "field-list", "list", "item-list"}
    return True


def typed(value):
    """Convert a `gh api -F` value like gh does: integers, true/false/null become JSON types."""
    if value in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[value]
    return int(value) if re.fullmatch(r"-?\d+", value) else value


class FakeGitHub:
    def __init__(self, issues=(), project_owner="example-org", project_number=123, login="tester",
                 project_owner_kind="organization"):
        self.issues = {}
        self.comments = {}
        self.parents = {}
        self.blockers = {}
        self.project_items = {}
        self.pull_requests = {}
        self.branches = []
        self.calls = []
        self.fail_on = None
        self.project_owner = project_owner
        self.project_number = project_number
        self.project_owner_kind = project_owner_kind
        self.login = login
        self.has_roadmap_order = True
        names = ("Backlog", "Planned", "In progress", "In review", "Done")
        self.option_ids = {name: f"opt-{name}" for name in names}
        self._check = True
        for issue in issues:
            self.add_issue(**issue)

    # -- state helpers -------------------------------------------------------------
    def add_issue(self, number, body, title="Issue", state="open", state_reason=None,
                  parent=None, assignees=(), label=None, status=None, in_project=False,
                  labels=None, author="tester", author_association="OWNER"):
        labels = list(labels) if labels is not None else ([label] if label else [])
        self.issues[number] = {
            "number": number,
            "id": number * 1000,
            "node_id": f"I_{number}",
            "title": title,
            "body": body,
            "state": state,
            "state_reason": state_reason,
            "assignees": list(assignees),
            "label": labels[0] if labels else None,
            "labels": labels,
            "author": author,
            "author_association": author_association,
        }
        self.comments.setdefault(number, [])
        if parent is not None:
            self.parents[number] = parent
        if in_project or status is not None:
            self.project_items[number] = {"id": f"PVTI_{number}", "status": status, "order": None}
        return self.issues[number]

    def add_pull_request(self, number, title, body="", head_ref="topic", head_repo=None,
                         author_association="OWNER"):
        self.pull_requests[number] = {
            "number": number,
            "title": title,
            "body": body,
            "head": {"ref": head_ref, "repo": {"full_name": head_repo or f"{OWNER}/{REPO}"}},
            "author_association": author_association,
        }

    def by_id(self, issue_id):
        return next(issue for issue in self.issues.values() if issue["id"] == int(issue_id))

    def mutations(self):
        return [call for call in self.calls if is_mutating(call)]

    def created(self):
        return [call for call in self.calls if call[:2] == ("issue", "create")]

    # -- gh entry point ------------------------------------------------------------
    def __call__(self, *args, check=True):
        self.calls.append(tuple(args))
        if self.fail_on and self.fail_on(args):
            return self._result(1, stderr="simulated failure", check=check, args=args)
        self._check = check
        handler = self._dispatch(list(args))
        if handler is None:
            raise AssertionError(f"Unexpected gh call: {args}")
        return handler

    def _result(self, returncode=0, stdout="", stderr="", check=True, args=()):
        if check and returncode != 0:
            sys.exit(f"gh {' '.join(args)} failed: {stderr}")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def _option(self, args, flag):
        return args[args.index(flag) + 1] if flag in args else None

    def _options(self, args, flag):
        return [args[index + 1] for index, arg in enumerate(args) if arg == flag]

    def _fields(self, args):
        fields = {}
        for index, arg in enumerate(args):
            if arg in ("-f", "-F"):
                key, _, value = args[index + 1].partition("=")
                fields[key] = typed(value) if arg == "-F" else value
        return fields

    def _dispatch(self, args):
        if args[0] == "api":
            return self._api(args)
        if args[:2] == ["issue", "create"]:
            number = max(self.issues, default=0) + 1
            body = Path(self._option(args, "--body-file")).read_text(encoding="utf-8")
            self.add_issue(number, body, title=self._option(args, "--title"),
                           labels=self._options(args, "--label"))
            return self._result(stdout=f"https://github.com/{OWNER}/{REPO}/issues/{number}\n")
        if args[:2] == ["issue", "edit"]:
            issue = self.issues[int(args[2])]
            if "--add-assignee" in args:
                issue["assignees"].append(self._option(args, "--add-assignee"))
            for name in self._options(args, "--add-label"):
                if name not in issue["labels"]:
                    issue["labels"].append(name)
                    issue["label"] = issue["labels"][0]
            for name in self._options(args, "--remove-label"):
                if name in issue["labels"]:
                    issue["labels"].remove(name)
                    issue["label"] = issue["labels"][0] if issue["labels"] else None
            return self._result(stdout="")
        if args[:2] == ["issue", "reopen"]:
            issue = self.issues[int(args[2])]
            issue["state"] = "open"
            issue["state_reason"] = None
            return self._result(stdout="")
        if args[:1] == ["project"]:
            # gh checks token scopes client-side for every `gh project` subcommand.
            return self._result(
                1,
                stderr="error: your authentication token is missing required scopes "
                       "[read:org read:discussion]",
                check=self._check, args=args,
            )
        return None

    def _project_lookup(self, query, fields):
        """Answer the per-owner-type projectV2 lookup like GitHub does (never a combined one)."""
        if "user(login" in query and "organization(login" in query:
            return self._result(1, stderr="combined owner lookup needs read:org", check=self._check)
        kind = "user" if "user(login" in query else "organization"
        found = (
            kind == self.project_owner_kind
            and fields["owner"].lower() == self.project_owner.lower()
            and int(fields["number"]) == self.project_number
        )
        if not found:
            body = {"data": {kind: None}, "errors": [{"type": "NOT_FOUND", "path": [kind]}]}
            return self._result(1, stdout=json.dumps(body), stderr="not found", check=False)
        options = [{"id": option_id, "name": name} for name, option_id in self.option_ids.items()]
        nodes = [{"id": "PVTSSF_status", "name": "Status", "options": options}]
        if self.has_roadmap_order:
            nodes.append({"id": "PVTF_order", "name": "Roadmap order"})
        project = {"id": "PVT_project", "fields": {"nodes": nodes}}
        return self._result(stdout=json.dumps({"data": {kind: {"projectV2": project}}}))

    def _api(self, args):
        method = self._option(args, "-X") or "GET"
        path = next(arg for arg in args[1:] if arg.startswith(("repos/", "user", "graphql")))
        fields = self._fields(args)
        if path == "user":
            return self._result(stdout=self.login + "\n")
        if path == "graphql":
            query = fields.get("query", "")
            if "addProjectV2ItemById" in query:
                assert fields["projectId"] == "PVT_project"
                number = int(fields["contentId"].rsplit("_", 1)[1])
                item = self.project_items.setdefault(
                    number, {"id": f"PVTI_{number}", "status": None, "order": None}
                )
                return self._result(stdout=json.dumps(
                    {"data": {"addProjectV2ItemById": {"item": {"id": item["id"]}}}}))
            if "deleteProjectV2Item" in query:
                assert fields["projectId"] == "PVT_project"
                item_id = fields["itemId"]
                number = next(n for n, i in self.project_items.items() if i["id"] == item_id)
                del self.project_items[number]
                return self._result(stdout=json.dumps(
                    {"data": {"deleteProjectV2Item": {"deletedItemId": item_id}}}))
            if "projectV2(number" in query:
                return self._project_lookup(query, fields)
            if "issue(number: $number) { id }" in query:
                number = int(fields["number"])
                if number not in self.issues:
                    body = {"data": {"repository": {"issue": None}},
                            "errors": [{"type": "NOT_FOUND", "path": ["repository", "issue"]}]}
                    return self._result(1, stdout=json.dumps(body), stderr="not found", check=False)
                return self._result(stdout=json.dumps(
                    {"data": {"repository": {"issue": {"id": f"I_{number}"}}}}))
            if "singleSelectOptionId" in query:
                item_id = fields["itemId"]
                if not isinstance(fields["optionId"], str):
                    body = {"errors": [{"message": "Variable $optionId of type String! was "
                                                   "provided invalid value"}]}
                    return self._result(1, stdout=json.dumps(body), check=False)
                item = next(item for item in self.project_items.values() if item["id"] == item_id)
                item["status"] = next(
                    name for name, option_id in self.option_ids.items()
                    if option_id == fields["optionId"]
                )
                return self._result(stdout='{"data":{"updateProjectV2ItemFieldValue":'
                                    '{"projectV2Item":{"id":"' + item_id + '"}}}}')
            if query.lstrip().startswith("mutation"):
                item_id = fields["itemId"]
                item = next(item for item in self.project_items.values() if item["id"] == item_id)
                item["order"] = float(fields["value"])
                return self._result(stdout='{"data":{"updateProjectV2ItemFieldValue":'
                                    '{"projectV2Item":{"id":"' + item_id + '"}}}}')
            number = int(fields["number"])
            nodes = []
            if number in self.project_items:
                item = self.project_items[number]
                status = {"name": item["status"]} if item["status"] else None
                order = {"number": item["order"]} if item["order"] is not None else None
                nodes.append({
                    "id": item["id"],
                    "project": {
                        "number": self.project_number,
                        "owner": {"login": self.project_owner},
                    },
                    "status": status,
                    "order": order,
                })
            data = {"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}}
            return self._result(stdout=json.dumps(data))
        if re.match(rf"{API}/branches\?", path):
            if "--slurp" in args:
                return self._result(stdout=json.dumps([[{"name": n} for n in self.branches]]))
            return self._result(stdout="".join(f"{name}\n" for name in self.branches))
        pull_list = re.match(rf"{API}/pulls\?state=open", path)
        if pull_list:
            if "--slurp" in args:
                return self._result(stdout=json.dumps([[
                    {key: pr[key] for key in ("number", "title", "body", "head",
                                              "author_association")}
                    for pr in self.pull_requests.values()
                ]]))
            return self._result(
                stdout="".join(json.dumps({key: pr[key] for key in (
                    "number", "title", "body", "head", "author_association")}) + "\n"
                               for pr in self.pull_requests.values())
            )
        if path.startswith(f"{API}/issues?state=all"):
            lines = [json.dumps({key: issue[key] for key in (
                "number", "id", "node_id", "title", "body", "state", "state_reason", "assignees",
                "labels", "author", "author_association")}
                | {"parent": None}) for issue in self.issues.values()]
            return self._result(stdout="\n".join(lines) + ("\n" if lines else ""))
        match = re.match(rf"{API}/issues/(\d+)(/.*)?$", path)
        if not match:
            return None
        number, rest = int(match.group(1)), match.group(2) or ""
        if rest == "/parent":
            if number in self.parents:
                return self._result(stdout=f"{self.parents[number]}\n")
            return self._result(1, stderr="gh: Not Found (HTTP 404)", check=False)
        if rest == "/sub_issues" and method == "POST":
            child = self.by_id(fields["sub_issue_id"])
            if child["number"] in self.parents and fields.get("replace_parent") != "true":
                return self._result(1, stderr="already has a parent (HTTP 422)", args=args)
            self.parents[child["number"]] = number
            return self._result(stdout=f"{number}\n")
        if rest == "/dependencies/blocked_by":
            if method == "POST":
                blocker = self.by_id(fields["issue_id"])["number"]
                self.blockers.setdefault(number, set()).add(blocker)
                return self._result(stdout=f"{number}\n")
            blockers = sorted(self.blockers.get(number, ()))
            return self._result(stdout="".join(f"{n}\n" for n in blockers))
        if rest.startswith("/dependencies/blocked_by/") and method == "DELETE":
            self.blockers.get(number, set()).discard(self.by_id(rest.rsplit("/", 1)[1])["number"])
            return self._result(stdout="")
        if rest.startswith("/comments") and method == "GET":
            comments = self.comments.get(number, [])
            return self._result(
                stdout="".join(json.dumps({"body": body}) + "\n" for body in comments)
            )
        if rest == "/comments" and method == "POST":
            self.comments.setdefault(number, []).append(fields["body"])
            return self._result(stdout="1\n")
        if rest == "" and method == "GET":
            issue = self.issues[number]
            return self._result(stdout=json.dumps(
                {key: issue[key] for key in ("number", "title", "body", "state", "state_reason")}
                | {"labels": [{"name": name} for name in issue["labels"]]}))
        if rest == "" and method == "PATCH":
            issue = self.issues[number]
            for key in ("title", "body", "state", "state_reason"):
                if key in fields:
                    issue[key] = fields[key]
            if fields.get("state") == "open":
                issue["state_reason"] = None
            return self._result(stdout=f"{number}\n")
        return None
