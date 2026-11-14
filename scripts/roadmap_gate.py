#!/usr/bin/env python3
"""Gate Phase pull requests on live roadmap prerequisites."""

import json
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys

import check_roadmap
import check_roadmap_dependencies as dependencies
import pr_conventions
import roadmap_issues
import roadmap_status


ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
ACKNOWLEDGEMENT_LABEL = "prerequisites-acknowledged"


def load_issue_chain(issue_number, fetch_issue, fetch_parent):
    """Load a child-to-parent issue chain and reject missing data and cycles."""
    chain = []
    seen = set()
    current = issue_number
    while current is not None:
        if isinstance(current, bool) or not isinstance(current, int) or current <= 0:
            raise dependencies.InvalidData(
                f"Roadmap gate: invalid parent issue number {current!r}"
            )
        if current in seen:
            raise dependencies.InvalidData(
                f"Roadmap gate: parent cycle detected at issue #{current}"
            )
        seen.add(current)
        issue = fetch_issue(current)
        if not isinstance(issue, dict) or issue.get("number") != current:
            raise dependencies.InvalidData(
                f"Roadmap gate: GitHub returned invalid data for issue #{current}"
            )
        body = issue.get("body")
        if body is not None and not isinstance(body, str):
            raise dependencies.InvalidData(
                f"Roadmap gate: invalid body for issue #{current}"
            )
        chain.append(issue)
        parent = fetch_parent(current)
        if parent is not None and (
            isinstance(parent, bool) or not isinstance(parent, int) or parent <= 0
        ):
            raise dependencies.InvalidData(
                f"Roadmap gate: invalid parent for issue #{current}: {parent!r}"
            )
        current = parent
    return chain


def roadmap_id_from_issue_chain(issue_chain, entries):
    """Resolve one roadmap entry from the Phase issue's roadmap Epic ancestry."""
    if len(issue_chain) < 2:
        raise dependencies.InvalidData(
            "Roadmap gate: Phase issue has no parent roadmap Epic"
        )

    ancestors = roadmap_status.labelled_epics(issue_chain[1:])
    candidate_ids = set()
    for issue in ancestors:
        body = issue.get("body") or ""
        marker_starts = body.count("<!-- speckit-roadmap:")
        known_marker_count = 0
        for value in entries:
            marker_count = body.count(roadmap_issues.roadmap_marker(value))
            if marker_count > 1:
                raise dependencies.InvalidData(
                    f"Roadmap gate: duplicate roadmap marker for {value} on "
                    f"issue #{issue.get('number')}"
                )
            known_marker_count += marker_count
            if marker_count:
                candidate_ids.add(value)
        if marker_starts != known_marker_count:
            raise dependencies.InvalidData(
                f"Roadmap gate: malformed or unknown roadmap marker on issue "
                f"#{issue.get('number')}"
            )

    if not candidate_ids:
        raise dependencies.InvalidData(
            "Roadmap gate: parent chain has no roadmap marker"
        )
    if len(candidate_ids) != 1:
        raise dependencies.InvalidData(
            "Roadmap gate: ambiguous roadmap identity in the parent chain: "
            + ", ".join(sorted(candidate_ids))
        )

    roadmap_id = next(iter(candidate_ids))
    try:
        epics = roadmap_status.resolve_epics(
            {roadmap_id: entries[roadmap_id]}, ancestors
        )
    except (dependencies.InvalidData, SystemExit) as error:
        raise dependencies.InvalidData(
            f"Roadmap gate: cannot resolve roadmap Epic for {roadmap_id}: {error}"
        ) from error
    if epics.get(roadmap_id) is None:
        raise dependencies.InvalidData(
            f"Roadmap gate: no roadmap Epic for roadmap entry {roadmap_id} "
            "is present in the parent chain"
        )
    return roadmap_id


def parse_gate_branch(branch):
    """Parse a PR head branch, failing closed on names outside the conventions."""
    try:
        return pr_conventions.parse_branch(branch)
    except pr_conventions.ConventionError as error:
        raise dependencies.InvalidData(
            f"Roadmap gate: invalid branch name: {error}"
        ) from error


def is_gated_branch(metadata):
    """Only Phase branches are gated."""
    return metadata.branch_type == "phase"


def non_phase_decision(metadata):
    """Pass decision for spec and repository-work branches, which the gate does not check."""
    return {
        "result": "pass",
        "branch_type": metadata.branch_type,
        "reason": "not a Phase implementation branch",
        "override": False,
    }


def evaluate_phase_status(roadmap_id, status, labels, verify_acknowledgement):
    """Judge a resolved entry's live status and fresh PR labels.

    `verify_acknowledgement()` returns (valid, reason) and is called only when the
    status is blocked and the acknowledgement label is present; the label waives
    blockers only when it returns valid.
    """
    if not isinstance(status, dict) or status.get("result") not in {
        "ready",
        "blocked",
        "error",
    }:
        raise dependencies.InvalidData(
            "Roadmap gate: unexpected status result from roadmap_status.py"
        )
    if not isinstance(status.get("entry"), dict) or status["entry"].get("id") != roadmap_id:
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned a different or missing entry identity"
        )
    diagnostics = status.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(
        isinstance(item, str) for item in diagnostics
    ):
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned invalid diagnostics"
        )
    if status["result"] == "error":
        return {
            "result": "error",
            "branch_type": "phase",
            "roadmap_id": roadmap_id,
            "message": "; ".join(diagnostics)
            or "roadmap status could not be evaluated",
            "override": False,
        }
    evaluated = status.get("dependencies")
    if not isinstance(evaluated, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not roadmap_issues.ROADMAP_ID.fullmatch(item["id"])
        or not isinstance(item.get("satisfied"), bool)
        for item in evaluated
    ):
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned invalid dependency data"
        )
    if diagnostics:
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned diagnostics with a non-error result"
        )
    unsatisfied = [item for item in evaluated if not item["satisfied"]]
    if (status["result"] == "ready" and unsatisfied) or (
        status["result"] == "blocked" and not unsatisfied
    ):
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned inconsistent dependency data"
        )
    if status["result"] == "ready":
        return {
            "result": "pass",
            "branch_type": "phase",
            "roadmap_id": roadmap_id,
            "reason": "all prerequisites are complete",
            "override": False,
        }
    blockers = [item["id"] for item in unsatisfied]
    decision = {
        "result": "blocked",
        "branch_type": "phase",
        "roadmap_id": roadmap_id,
        "blockers": blockers,
        "override": False,
    }
    if ACKNOWLEDGEMENT_LABEL in labels:
        valid, reason = verify_acknowledgement()
        if valid is True:
            return {
                **decision,
                "result": "pass",
                "reason": "human acknowledgement label waives the reported blockers",
                "override": True,
            }
        decision["acknowledgement_rejected"] = reason or (
            f"who applied {ACKNOWLEDGEMENT_LABEL} cannot be verified"
        )
    return decision


def github_issue(repository, number):
    """Read one issue body from GitHub without trusting shell-interpolated inputs."""
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repository}/issues/{number}",
            "--jq",
            "{number, body, state, state_reason, labels: [.labels[].name]}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise dependencies.InvalidData(
            f"Roadmap gate: cannot read issue #{number}: {result.stderr.strip()}"
        )
    try:
        issue = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise dependencies.InvalidData(
            f"Roadmap gate: GitHub returned invalid issue data for #{number}"
        ) from error
    return issue


def github_parent(repository, number):
    """Read a parent issue number; distinguish no parent from API failures."""
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repository}/issues/{number}/parent",
            "--jq",
            ".number",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        try:
            parent = json.loads(result.stdout)
            if isinstance(parent, bool) or not isinstance(parent, int) or parent <= 0:
                raise ValueError("expected a positive issue number")
            return parent
        except ValueError as error:
            raise dependencies.InvalidData(
                f"Roadmap gate: invalid parent response for issue #{number}"
            ) from error
    if "HTTP 404" in result.stderr:
        return None
    raise dependencies.InvalidData(
        f"Roadmap gate: cannot read parent for issue #{number}: "
        f"{result.stderr.strip()}"
    )


def live_roadmap_status(roadmap_id):
    """Ask the shared live status command to evaluate the resolved roadmap entry."""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "roadmap_status.py"),
            "--id",
            roadmap_id,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.returncode not in {0, 1, 2}:
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py failed: " + result.stderr.strip()
        )
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned invalid JSON: "
            + result.stderr.strip()
        ) from error
    if not isinstance(status, dict) or not isinstance(status.get("entry"), dict):
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned invalid status data"
        )
    if status["entry"].get("id") != roadmap_id:
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned a different entry identity"
        )
    expected = {0: "ready", 1: "blocked", 2: "error"}[result.returncode]
    if status.get("result") != expected:
        raise dependencies.InvalidData(
            "Roadmap gate: roadmap_status.py returned inconsistent status data"
        )
    return status


def github_pr_labels(repository, number):
    """Read fresh pull request labels across all pages, never from the event snapshot."""
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp",
         f"repos/{repository}/issues/{number}/labels?per_page=100"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode:
        raise dependencies.InvalidData(
            f"Roadmap gate: cannot read pull request labels: {result.stderr.strip()}"
        )
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise dependencies.InvalidData(
            "Roadmap gate: GitHub returned invalid label JSON"
        ) from error
    if not isinstance(pages, list) or not pages or any(
        not isinstance(page, list) or any(
            not isinstance(label, dict) or not isinstance(label.get("name"), str)
            for label in page
        ) for page in pages
    ):
        raise dependencies.InvalidData(
            "Roadmap gate: pull request has invalid label data"
        )
    return {label["name"] for page in pages for label in page}


def github_pr_events(repository, number):
    """Read every issue event of the pull request across all pages."""
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp",
         f"repos/{repository}/issues/{number}/events?per_page=100"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode:
        raise dependencies.InvalidData(
            f"Roadmap gate: cannot read pull request events: {result.stderr.strip()}"
        )
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise dependencies.InvalidData(
            "Roadmap gate: GitHub returned invalid event JSON"
        ) from error
    if not isinstance(pages, list) or any(
        not isinstance(page, list) or any(not isinstance(item, dict) for item in page)
        for page in pages
    ):
        raise dependencies.InvalidData(
            "Roadmap gate: pull request has invalid event data"
        )
    return [item for page in pages for item in page]


def event_order_key(item):
    """Return (created_at, id) for ordering label events, or None if either is invalid."""
    created_at, event_id = item.get("created_at"), item.get("id")
    if (not isinstance(created_at, str) or isinstance(event_id, bool)
            or not isinstance(event_id, int)):
        return None
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    return timestamp, event_id


def acknowledgement_label_events(events):
    """Return the `labeled` events for the acknowledgement label."""
    return [
        item for item in events
        if item.get("event") == "labeled" and isinstance(item.get("label"), dict)
        and item["label"].get("name") == ACKNOWLEDGEMENT_LABEL
    ]


def acknowledgement_by_human(events):
    """Accept the label only if its latest `labeled` event names a User actor (fail closed)."""
    labeled = acknowledgement_label_events(events)
    if not labeled:
        return False, (
            f"no labeled event for {ACKNOWLEDGEMENT_LABEL} was found, so who "
            "applied it cannot be verified"
        )
    keys = [event_order_key(item) for item in labeled]
    if None in keys:
        return False, (
            f"a labeled event for {ACKNOWLEDGEMENT_LABEL} has no valid created_at "
            "or id, so the latest application cannot be determined"
        )
    latest = max(zip(keys, labeled), key=lambda pair: pair[0])[1]
    actor = latest.get("actor")
    actor_type = actor.get("type") if isinstance(actor, dict) else None
    if actor_type == "Bot":
        return False, f"{ACKNOWLEDGEMENT_LABEL} label was applied by a bot account"
    if latest.get("performed_via_github_app"):
        return False, (
            f"{ACKNOWLEDGEMENT_LABEL} label was applied through a GitHub App"
        )
    if actor_type != "User":
        return False, (
            f"the account that applied {ACKNOWLEDGEMENT_LABEL} cannot be "
            "determined to be a human user"
        )
    return True, None


def acknowledgement_in_payload(event):
    """True when this run was triggered by a User applying the acknowledgement label.

    The webhook payload has no performed_via_github_app field, so it is trusted only
    while the events API shows no `labeled` event for the label yet.
    """
    label, sender = event.get("label"), event.get("sender")
    return (
        event.get("action") == "labeled"
        and isinstance(label, dict) and label.get("name") == ACKNOWLEDGEMENT_LABEL
        and isinstance(sender, dict) and sender.get("type") == "User"
    )


def run_event(event, repository):
    """Evaluate one pull_request_target payload from trusted workflow context."""
    if not isinstance(event, dict) or not isinstance(event.get("pull_request"), dict):
        raise dependencies.InvalidData(
            "Roadmap gate: GitHub event has no pull_request object"
        )
    pull_request = event["pull_request"]
    head = pull_request.get("head")
    branch = head.get("ref") if isinstance(head, dict) else None
    if not isinstance(branch, str):
        raise dependencies.InvalidData(
            "Roadmap gate: pull request has no valid head branch"
        )
    metadata = parse_gate_branch(branch)
    if not is_gated_branch(metadata):
        return non_phase_decision(metadata)

    if not REPOSITORY.fullmatch(repository or ""):
        raise dependencies.InvalidData(
            "Roadmap gate: invalid repository name from workflow context"
        )
    number = pull_request.get("number")
    event_number = event.get("number")
    if (isinstance(number, bool) or not isinstance(number, int) or number <= 0
            or isinstance(event_number, bool) or not isinstance(event_number, int)
            or event_number != number):
        raise dependencies.InvalidData(
            "Roadmap gate: invalid or inconsistent pull request number"
        )
    entries = check_roadmap.validate_roadmap(ROOT)
    issue_chain = load_issue_chain(
        metadata.issue_number,
        lambda number: github_issue(repository, number),
        lambda number: github_parent(repository, number),
    )
    roadmap_id = roadmap_id_from_issue_chain(issue_chain, entries)
    status = live_roadmap_status(roadmap_id)
    labels = github_pr_labels(repository, number)

    def verify_acknowledgement():
        # The events API is authoritative; API errors raise and fail closed.
        events = github_pr_events(repository, number)
        if not acknowledgement_label_events(events) and acknowledgement_in_payload(event):
            return True, None
        return acknowledgement_by_human(events)

    return evaluate_phase_status(roadmap_id, status, labels, verify_acknowledgement)


def main():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not event_path:
        print("Roadmap gate: GITHUB_EVENT_PATH is required.", file=sys.stderr)
        return 1
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        decision = run_event(event, repository)
    except (
        dependencies.InvalidData,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        print(f"Roadmap gate failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(decision, ensure_ascii=False))
    if decision["result"] == "pass":
        if decision.get("override"):
            print(
                f"Roadmap gate: waived by {ACKNOWLEDGEMENT_LABEL}: "
                + ", ".join(decision["blockers"]),
                file=sys.stderr,
            )
        return 0
    if decision["result"] == "blocked":
        blockers = ", ".join(decision["blockers"])
        message = (
            f"Roadmap prerequisites for {decision['roadmap_id']} remain unfinished: "
            f"{blockers}."
        )
        if decision.get("acknowledgement_rejected"):
            message += (
                f" The {ACKNOWLEDGEMENT_LABEL} label is not accepted: "
                f"{decision['acknowledgement_rejected']}. A human must apply it."
            )
    else:
        message = decision.get(
            "message", "Roadmap gate encountered an invalid status or identity."
        )
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
