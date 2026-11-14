#!/usr/bin/env python3
"""Reconcile GitHub Project statuses for Phase issues.

Only Phase (Story) issues are tracked; all other issues are left untouched. A retired Phase
(closed as not planned by the Phase sync, or by hand) is removed from the board instead of getting
a Status; the dry run prints that as `[dry-run] remove #N from project ...`.

Usage: scripts/sync-project-status.py (--issue N | --all)
       [--project-owner OWNER] [--project-number N] [--apply]

Without --apply, the script prints the status changes it would make.
"""

import argparse
import json
import re
import sys

import project_board
import project_context
import pr_conventions

sync = project_board.sync
REPO_API = "repos/{owner}/{repo}"


def api_json(*args):
    result = sync.gh("api", *args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        sys.exit(f"Could not parse GitHub API response: {error}")


def api_collection(owner, repo, endpoint):
    pages = api_json(
        f"{REPO_API.format(owner=owner, repo=repo)}/{endpoint}",
        "--paginate",
        "--slurp",
    )
    if not isinstance(pages, list):
        sys.exit(f"Expected a list from GitHub API endpoint {endpoint}")
    if pages and isinstance(pages[0], list):
        return [item for page in pages for item in page]
    return pages


def get_issue(owner, repo, number):
    return api_json(f"{REPO_API.format(owner=owner, repo=repo)}/issues/{number}")


def labels(issue):
    return {label["name"] for label in issue.get("labels", [])}


def issue_kind(issue):
    issue_labels = labels(issue)
    if "Planning" in issue_labels:
        return None
    if "Epic" in issue_labels:
        return "feature"
    if "Story" in issue_labels:
        return "phase"
    return None


def desired_status(
    issue_labels,
    state,
    state_reason=None,
    branch_exists=False,
    open_pr=False,
    current_status=None,
):
    """Return the reconciled Status, or None for issue types this script must leave alone."""
    issue_labels = set(issue_labels)
    if "Planning" in issue_labels or "Story" not in issue_labels:
        return None

    if state.lower() == "closed":
        desired = "Backlog" if state_reason == "not_planned" else "Done"
    elif open_pr:
        desired = "In review"
    elif branch_exists:
        desired = "In progress"
    else:
        desired = "Planned"

    return desired


def branch_phase_issues(branches):
    issue_phases = set()
    for branch in branches:
        try:
            metadata = pr_conventions.parse_branch(branch)
        except pr_conventions.ConventionError:
            continue
        if metadata.branch_type == "phase":
            issue_phases.add((metadata.issue_number, metadata.phase_number))
    return issue_phases


def has_active_phase_branch(issue, active_phase_branches):
    """Match phase branches only when their phase number agrees with the issue title."""
    return any(
        issue_number == issue["number"]
        and pr_conventions.phase_title_matches_number(
            issue.get("title", ""), phase_number
        )
        for issue_number, phase_number in active_phase_branches
    )


TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def is_trusted_pull_request(pull_request, repository):
    """True for a PR from a branch of this repository or by an owner, member or collaborator.

    Anyone can open a pull request from a fork of a public repository; their closing keyword
    must not move a Phase issue to In review. A PR without head information (hand-built data)
    is not judged; the API always sends it.
    """
    head = pull_request.get("head")
    if head is None and "author_association" not in pull_request:
        return True
    head_repo = ((head or {}).get("repo") or {}).get("full_name") or ""
    return (
        head_repo.casefold() == repository.casefold()
        or pull_request.get("author_association") in TRUSTED_ASSOCIATIONS
    )


def open_pr_closing_issues(pull_requests, repository):
    """Find closing references in open trusted PRs, including drafts, for this repository."""
    issue_numbers = set()
    for pull_request in pull_requests:
        if not is_trusted_pull_request(pull_request, repository):
            continue
        body = pull_request.get("body") or ""
        candidates = {int(number) for number in re.findall(r"#([1-9]\d*)", body)}
        issue_numbers.update(
            number
            for number in candidates
            if pr_conventions.has_closing_reference(body, number, repository)
        )
    return issue_numbers


def open_pr_phase_branches(pull_requests, repository):
    """Find (issue, phase) pairs of open trusted PRs from a Phase branch of this repository.

    Phase PRs carry no closing keyword (`Part of #N`), so the branch name links them.
    """
    pairs = set()
    for pull_request in pull_requests:
        if not is_trusted_pull_request(pull_request, repository):
            continue
        ref = (pull_request.get("head") or {}).get("ref")
        if not ref:
            continue
        pairs |= branch_phase_issues([ref])
    return pairs


def target_issues(owner, repo, issue_number=None):
    if issue_number is None:
        all_issues = api_collection(
            owner,
            repo,
            "issues?state=all&per_page=100",
        )
        return {issue["number"]: issue for issue in all_issues if "pull_request" not in issue}
    return {issue_number: get_issue(owner, repo, issue_number)}


def reconcile(
    owner,
    repo,
    issue_number=None,
    apply=False,
    *,
    project_owner,
    project_number,
    state_overrides=None,
):
    """Reconcile Phase statuses.

    `state_overrides` maps an issue number to the (state, state_reason) it has or is about to
    have, for a caller that just changed it (or previews the change in a dry run).
    """
    issues = target_issues(owner, repo, issue_number)
    for number, (state, state_reason) in (state_overrides or {}).items():
        if number in issues:
            issues[number] = {**issues[number], "state": state, "state_reason": state_reason}
    if not any(issue_kind(issue) == "phase" for issue in issues.values()):
        if issue_number is not None:
            print(f"#{issue_number} is not a Phase issue; nothing to do.")
        return

    branches = api_collection(owner, repo, "branches?per_page=100")
    pull_requests = api_collection(owner, repo, "pulls?state=open&per_page=100")
    repository = f"{owner}/{repo}"
    active_phase_branches = branch_phase_issues(branch["name"] for branch in branches)
    active_review_phases = open_pr_closing_issues(pull_requests, repository)
    review_phase_branches = open_pr_phase_branches(pull_requests, repository)
    project = project_board.Project(project_owner, project_number)

    for issue in sorted(issues.values(), key=lambda item: item["number"]):
        if issue_kind(issue) != "phase":
            continue
        if sync.is_retired(issue):
            # A retired Phase (closed as not planned by the Phase sync) leaves the board.
            project_board.ensure_removed(
                owner, repo, project, issue, apply,
                lambda message: print(f"[dry-run] {message}"),
            )
            continue
        item = project_board.project_item(
            owner, repo, issue["number"], project_owner, project_number
        )
        current_status = item[1] if item else None
        desired = desired_status(
            labels(issue),
            issue["state"],
            state_reason=issue.get("state_reason"),
            branch_exists=has_active_phase_branch(issue, active_phase_branches),
            open_pr=issue["number"] in active_review_phases
            or has_active_phase_branch(issue, review_phase_branches),
            current_status=current_status,
        )
        if desired is None:
            continue
        if item is None:
            print(f"[skip] #{issue['number']} is not in project {project_owner}/{project_number}")
            continue
        if current_status == desired:
            continue
        old_status = current_status or "(unset)"
        if not apply:
            print(f"[dry-run] #{issue['number']}: {old_status} -> {desired}")
            continue
        project.set_status(item[0], desired)
        print(f"#{issue['number']}: {old_status} -> {desired}")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", type=int, help="reconcile one Phase issue")
    target.add_argument("--all", action="store_true", help="reconcile all Phase issues")
    parser.add_argument("--apply", action="store_true", help="apply changes instead of dry-run")
    parser.add_argument("--project-owner", help="override github_project.owner from project-context.toml")
    parser.add_argument("--project-number", type=int, help="override github_project.number from project-context.toml")
    args = parser.parse_args()
    if args.project_owner is not None and (
        len(args.project_owner) > 39 or not project_context.OWNER_PATTERN.fullmatch(args.project_owner)
    ):
        parser.error("--project-owner must be a GitHub owner login")
    if args.project_number is not None and args.project_number < 1:
        parser.error("--project-number must be a positive integer")
    if args.project_owner is None or args.project_number is None:
        try:
            board = project_context.load_context(project_context.DEFAULT_CONFIG)["github_project"]
        except (OSError, ValueError) as error:
            parser.error(f"Invalid project context TOML {project_context.DEFAULT_CONFIG}: {error}")
        if args.project_owner is None:
            args.project_owner = board["owner"]
        if args.project_number is None:
            args.project_number = board["number"]
    return args


def main():
    args = parse_arguments()
    if args.issue is not None and args.issue < 1:
        sys.exit("--issue must be a positive issue number")
    owner, repo = sync.origin_repo()
    reconcile(
        owner,
        repo,
        issue_number=args.issue,
        apply=args.apply,
        project_owner=args.project_owner,
        project_number=args.project_number,
    )


if __name__ == "__main__":
    main()
