#!/usr/bin/env python3
"""Synchronize a Spec Kit feature's Phase issues with its tasks.md.

Usage: scripts/sync-task-issues.py [--roadmap-only] [--feature specs/008-example-feature]
       [--apply | --check]
       [--skip-project] [--project-owner OWNER] [--project-number N]

Without --apply, the script only prints the changes it would make; --check does the same and
exits 1 when any change is pending. The hierarchy is: roadmap Epic -> Phase issues. Only Phases
are GitHub issues: T### tasks stay in tasks.md (their IDs are stable) and are never turned into
issues; other issues under the epic are neither read nor changed.

tasks.md is authoritative for Phase issue titles and bodies. The provisional Phase 1 issue that
sync-roadmap-issues.py created is reused (found by its `phase-1` marker) and updated from
tasks.md; the other Phases are created under the epic and added to the GitHub Project as
Planned. Phase numbers are immutable identities: headings may be renamed, but a number is never
renumbered or reused. A Phase issue closed as not planned is retired, so tasks.md defining that
number again stops the sync.

A Phase is complete when every task in its section is checked. Open complete Phases are closed
as completed; a completed Phase with unchecked work in tasks.md is reopened. The roadmap epic
closes once its spec-plan issue is completed and every currently defined Phase is complete.
A Phase removed from tasks.md while still Planned is closed as not planned; if it is In
progress, In review or Done, the sync stops and explains how to resolve it.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_conventions  # noqa: E402
import project_context  # noqa: E402
import roadmap_issues  # noqa: E402
import github_issue_comments  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ENV = dict(os.environ, GH_PAGER="cat", GH_PROMPT_DISABLED="1")
# Changes a dry run or --check would have made; --check fails when this is not empty.
PENDING = []
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_ERROR = re.compile(r"HTTP\s+429|rate limit", re.IGNORECASE)


def dry_run(message):
    PENDING.append(message)
    print(f"[dry-run] {message}")


def gh(*args, check=True, env=None):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        result = subprocess.run(
            ["gh", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=ENV if env is None else env,
            stdin=subprocess.DEVNULL,
        )
        error_text = f"{result.stderr}\n{result.stdout}"
        if result.returncode == 0 or not RATE_LIMIT_ERROR.search(error_text):
            break
        if attempt == RATE_LIMIT_RETRIES:
            break
        delay = 60
        print(
            f"GitHub rate limit response; retrying in {delay}s "
            f"({attempt + 1}/{RATE_LIMIT_RETRIES})",
            file=sys.stderr,
        )
        time.sleep(delay)
    if check and result.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def origin_repo():
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    match = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+)/(.+?)(?:\.git)?$", url)
    if not match:
        sys.exit(f"Remote is not a GitHub URL: {url!r}")
    return match.group(1), match.group(2)


def resolve_feature_path(value):
    if value:
        candidate = Path(value)
        candidate = candidate if candidate.is_absolute() else ROOT / candidate
        feature_dir = candidate.parent if candidate.name == "tasks.md" else candidate
    else:
        task_files = sorted((ROOT / "specs").glob("*/tasks.md"))
        if len(task_files) != 1:
            sys.exit(
                "Pass --feature when the repository does not contain exactly one specs/*/tasks.md"
            )
        feature_dir = task_files[0].parent

    feature_dir = feature_dir.resolve()
    try:
        feature_path = feature_dir.relative_to(ROOT)
    except ValueError:
        sys.exit(f"Feature directory must be inside the repository: {feature_dir}")
    if not (feature_dir / "tasks.md").is_file():
        sys.exit(f"Feature has no tasks.md: {feature_path}")
    return feature_path.as_posix()


def parse_feature(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    feature_title = None
    phases = []
    phase_by_key = {}
    tasks = []
    current_phase = None

    for line in lines:
        title_match = re.match(r"# Tasks:\s*(.+)", line)
        if title_match and feature_title is None:
            feature_title = title_match.group(1).strip()

        phase_match = re.match(r"## Phase (\d+):\s*(.+)", line)
        if phase_match:
            number, name = phase_match.groups()
            current_phase = {
                "key": f"phase-{int(number)}",
                "number": int(number),
                "title": f"Phase {int(number)}: {name.strip()}",
                "summary": [],
            }
            phases.append(current_phase)
            phase_by_key[current_phase["key"]] = current_phase
            continue

        if current_phase and re.match(r"\*\*(Purpose|Goal|Checkpoint)\*\*:", line):
            current_phase["summary"].append(line)

        task_match = re.match(r"- \[([ xX])\] (T\d{3,})\s+(.*)", line)
        if not task_match:
            continue
        if current_phase is None:
            sys.exit(f"Task appears before the first phase in {path}: {line}")

        checkbox, task_id, rest = task_match.groups()
        tasks.append(
            {
                "id": task_id,
                "text": f"{task_id} {rest}",
                "phase_key": current_phase["key"],
                "done": checkbox != " ",
            }
        )

    if not feature_title:
        sys.exit(f"Could not find '# Tasks: <feature>' in {path}")
    if not phases:
        sys.exit(f"Could not find any '## Phase N:' headings in {path}")
    if not tasks:
        sys.exit(f"Could not find any task checklist items in {path}")
    if len({task["id"] for task in tasks}) != len(tasks):
        sys.exit(f"Duplicate task IDs in {path}")
    if len(phase_by_key) != len(phases):
        sys.exit(f"Duplicate phase numbers in {path}")

    return {"title": feature_title, "phases": phases, "tasks": tasks}


PHASE_PAUSED_MARKER = "<!-- speckit-phase-paused -->"
PHASE_RETIRED_MARKER = "<!-- speckit-phase-retired -->"


def phase_marker(feature_path, phase_key):
    return roadmap_issues.phase_marker(feature_path, phase_key)


def load_issues(api):
    output = gh(
        "api",
        "--paginate",
        f"{api}/issues?state=all&per_page=100",
        "--jq",
        ".[] | select(.pull_request == null) | "
        "{number, id, node_id, title, body, parent, state, state_reason, "
        "author: .user.login, author_association, "
        "assignees: [.assignees[]?.login], labels: [.labels[]?.name]}",
    ).stdout
    return [json.loads(line) for line in output.splitlines()]


def find_phase_issues(issues, feature_path=None, roadmap_id=None):
    """Return {phase key: issue} for every Phase issue of a feature or roadmap entry."""
    patterns = []
    if feature_path:
        patterns.append(
            re.compile(rf"<!-- speckit-phase: {re.escape(feature_path)}#(phase-\d+) -->")
        )
        roadmap_id = roadmap_issues.roadmap_id_for_path(feature_path)
    subject = feature_path or f"roadmap entry {roadmap_id}"
    if roadmap_id:
        patterns.append(
            re.compile(rf"<!-- speckit-roadmap-phase: {roadmap_id}#(phase-\d+) -->")
        )
    found = {}
    for issue in roadmap_issues.trusted_issues(issues):
        body = issue["body"] or ""
        phase_keys = {
            match.group(1)
            for pattern in patterns
            for match in pattern.finditer(body)
        }
        if not phase_keys:
            continue
        if len(phase_keys) > 1:
            sys.exit(
                f"Issue #{issue['number']} has conflicting Phase markers for {subject}: "
                + ", ".join(sorted(phase_keys))
            )
        key = next(iter(phase_keys))
        if key in found and found[key]["number"] != issue["number"]:
            marker = (
                roadmap_issues.roadmap_phase_marker(roadmap_id, key)
                if roadmap_id
                else phase_marker(feature_path, key)
            )
            sys.exit(
                f"Multiple issues match {subject} {key}: "
                f"#{found[key]['number']}, #{issue['number']}. "
                + roadmap_issues.duplicate_hint(marker)
            )
        found[key] = issue
    return found


def is_retired(issue):
    body = issue.get("body") or ""
    return (
        issue.get("state") == "closed"
        and issue.get("state_reason") == "not_planned"
        and (PHASE_RETIRED_MARKER in body or PHASE_PAUSED_MARKER not in body)
    )


def find_removed_phases(phase_issues, feature):
    """Return (key, issue) for Phase issues no longer defined in tasks.md and not yet retired."""
    defined = {phase["key"] for phase in feature["phases"]}
    return [
        (key, issue)
        for key, issue in sorted(phase_issues.items(), key=lambda item: int(item[0][6:]))
        if key not in defined and not is_retired(issue)
    ]


def reused_retired_phases(phase_issues, feature):
    """Return (key, issue) for retired Phase issues whose number tasks.md defines again."""
    return [
        (phase["key"], phase_issues[phase["key"]])
        for phase in feature["phases"]
        if phase_issues.get(phase["key"]) and is_retired(phase_issues[phase["key"]])
    ]


def active_phase_work(api):
    """Return (phase branches as (issue, phase number) pairs, open pull requests)."""
    branch_names = gh(
        "api", "--paginate", f"{api}/branches?per_page=100", "--jq", ".[].name"
    ).stdout.split()
    branches = set()
    for name in branch_names:
        try:
            metadata = pr_conventions.parse_branch(name)
        except pr_conventions.ConventionError:
            continue
        if metadata.branch_type == "phase":
            branches.add((metadata.issue_number, metadata.phase_number))
    pages = json.loads(
        gh("api", "--paginate", "--slurp", f"{api}/pulls?state=open&per_page=100").stdout or "[]"
    )
    pull_requests = [pr for page in pages for pr in page]
    return branches, pull_requests


def load_status_sync():
    """Load sync-project-status.py, whose rules decide a Phase's status and PR trust."""
    spec = importlib.util.spec_from_file_location(
        "sync_project_status", Path(__file__).resolve().parent / "sync-project-status.py"
    )
    status = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(status)
    return status


def phase_in_review(pull_requests, issue, repository):
    """True when a trusted open PR closes the Phase or comes from its Phase branch."""
    status = load_status_sync()
    return issue["number"] in status.open_pr_closing_issues(
        pull_requests, repository
    ) or status.has_active_phase_branch(
        issue, status.open_pr_phase_branches(pull_requests, repository)
    )


def blocked_removed_phases(api, repository, removed):
    """Return (key, issue, status) for removed Phases that are In progress, In review or Done."""
    blocked = []
    pending = []
    for key, issue in removed:
        if issue.get("state") == "closed":
            if issue.get("state_reason") == "not_planned":
                continue
            blocked.append((key, issue, "Done"))
        else:
            pending.append((key, issue))
    if pending:
        branches, pull_requests = active_phase_work(api)
        for key, issue in pending:
            if phase_in_review(pull_requests, issue, repository):
                blocked.append((key, issue, "In review"))
            elif (issue["number"], int(key[6:])) in branches:
                blocked.append((key, issue, "In progress"))
    return blocked


def stop_for_removed_phases(feature_path, blocked):
    sys.exit(
        f"{feature_path}/tasks.md no longer defines "
        + ", ".join(f"{key} (#{issue['number']}, {status})" for key, issue, status in blocked)
        + ". Phase issues that are In progress, In review or Done are never closed or reused "
        "silently. Resolve it, then re-run the sync:\n"
        "  - restore the Phase heading with the same number in tasks.md, or\n"
        "  - if the Phase is intentionally retired, close its issue as not planned "
        "(and finish or close any branch or pull request for it); its number is then "
        "retired and must not be reused."
    )


def wait_for_phase_issues(api, feature_path, feature, attempts=6, delay=2):
    for attempt in range(attempts):
        phase_issues = find_phase_issues(load_issues(api), feature_path)
        missing = [phase["key"] for phase in feature["phases"] if phase["key"] not in phase_issues]
        if not missing:
            return phase_issues
        if attempt < attempts - 1:
            time.sleep(delay)
    sys.exit(f"Created issues did not become visible in time: {', '.join(missing)}")


def create_issue(repository, title, body, labels):
    """Create an issue with one label (str) or several (iterable of str); returns its URL."""
    label_args = []
    for label in [labels] if isinstance(labels, str) else labels:
        label_args += ["--label", label]
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as file:
            file.write(body)
            path = file.name
        return gh(
            "issue",
            "create",
            "--repo",
            repository,
            "--title",
            title,
            "--body-file",
            path,
            *label_args,
        ).stdout.strip()
    finally:
        if path:
            os.unlink(path)


def phase_title(phase):
    return phase["title"]


def phase_tasks(feature, phase):
    return [task for task in feature["tasks"] if task["phase_key"] == phase["key"]]


def phase_body(feature_path, feature, phase):
    summary = "\n\n".join(phase["summary"])
    if summary:
        summary += "\n\n"
    markers = [phase_marker(feature_path, phase["key"])]
    roadmap_id = roadmap_issues.roadmap_id_for_path(feature_path)
    if roadmap_id:
        markers.append(roadmap_issues.roadmap_phase_marker(roadmap_id, phase["key"]))
    checklist = "\n".join(
        f"- [{'x' if task['done'] else ' '}] {task['text']}" for task in phase_tasks(feature, phase)
    )
    marker_text = "\n".join(markers)
    return (
        f"{marker_text}\n\n"
        f"{summary}"
        f"Generated from `{feature_path}/tasks.md`; edit the file, not this list. "
        "Checked tasks there decide when this Phase is complete.\n\n"
        f"{checklist}\n\n"
        f"Source: [{phase['title']}]({feature_path}/tasks.md)\n"
    )


def phase_complete(feature, phase):
    tasks = phase_tasks(feature, phase)
    return bool(tasks) and all(task["done"] for task in tasks)


def current_parent_number(api, issue):
    result = gh(
        "api",
        f"{api}/issues/{issue['number']}/parent",
        "--jq",
        ".number",
        check=False,
    )
    if result.returncode == 0:
        return int(result.stdout.strip())
    if "HTTP 404" in result.stderr:
        return None
    sys.exit(
        f"Could not read parent for issue #{issue['number']}: {result.stderr.strip()}"
    )


def current_blocker_numbers(api, issue):
    result = gh(
        "api",
        f"{api}/issues/{issue['number']}/dependencies/blocked_by",
        "--jq",
        ".[].number",
    )
    return {int(number) for number in result.stdout.split()}


def sync_parent(api, parent, child, parent_label, child_label, apply, parent_cache):
    existing_parent = None
    if child:
        if child["number"] not in parent_cache:
            parent_cache[child["number"]] = current_parent_number(api, child)
        existing_parent = parent_cache[child["number"]]
    if parent and existing_parent == parent["number"]:
        return "skipped"
    replace = bool(existing_parent)
    if replace and not (parent and child):
        sys.exit(f"Missing issue while moving {child_label} to {parent_label}")
    if not apply:
        action = "move" if replace else "parent"
        dry_run(f"{action}: {parent_label} <- {child_label}")
        return "planned"
    if not parent or not child:
        sys.exit(f"Missing issue while linking {parent_label} <- {child_label}")
    gh(
        "api",
        "-X",
        "POST",
        f"{api}/issues/{parent['number']}/sub_issues",
        "-F",
        f"sub_issue_id={child['id']}",
        *(["-F", "replace_parent=true"] if replace else []),
        "--jq",
        ".number",
    )
    if replace:
        print(f"{child_label} moved from #{existing_parent} into {parent_label}")
    else:
        print(f"{parent_label} now contains {child_label}")
    parent_cache[child["number"]] = parent["number"]
    return "moved" if replace else "added"


def completed_issues(feature, epic, phase_issues, planning_issues=None):
    """Return (label, issue) pairs for issues whose tasks.md work is complete.

    A Phase is done when all its tasks are checked; the feature is done when every
    currently defined Phase is done. Phases are listed before the feature. A roadmap
    epic (planning_issues given) additionally needs its spec-plan issue closed as
    completed; see roadmap_issues.epic_close_blockers.
    """
    completed = []
    for phase in feature["phases"]:
        issue = phase_issues.get(phase["key"])
        if issue and phase_complete(feature, phase):
            completed.append((phase["title"], issue))
    all_complete = all(phase_complete(feature, phase) for phase in feature["phases"])
    if planning_issues is None:
        feature_done = all_complete
    else:
        feature_done = not roadmap_issues.epic_close_blockers(all_complete, planning_issues)
    if epic and feature_done:
        completed.append(("epic", epic))
    return completed


def close_completed_issues(api, completed, apply, state_changes=None):
    closed = already_closed = 0
    for label, issue in completed:
        if issue.get("state") == "closed":
            already_closed += 1
            continue
        if label != "epic" and state_changes is not None:
            state_changes[issue["number"]] = ("closed", "completed")
        if not apply:
            dry_run(f"close #{issue['number']} ({label})")
            continue
        gh(
            "api",
            "-X",
            "PATCH",
            f"{api}/issues/{issue['number']}",
            "-f",
            "state=closed",
            "-f",
            "state_reason=completed",
            "--jq",
            ".number",
        )
        issue["state"] = "closed"
        issue["state_reason"] = "completed"
        closed += 1
        print(f"#{issue['number']} ({label}) closed as completed")
    return closed, already_closed


def reopen_incomplete_phases(api, feature, phase_issues, apply, state_changes=None):
    """Reopen Phases closed as completed whose tasks.md section has unchecked work again."""
    reopened = 0
    for phase in feature["phases"]:
        issue = phase_issues.get(phase["key"])
        if (
            not issue
            or issue.get("state") != "closed"
            or issue.get("state_reason") != "completed"
            or phase_complete(feature, phase)
        ):
            continue
        reopened += 1
        if state_changes is not None:
            state_changes[issue["number"]] = ("open", None)
        if not apply:
            dry_run(f"reopen #{issue['number']} ({phase['title']} has unchecked tasks)")
            continue
        gh(
            "api",
            "-X",
            "PATCH",
            f"{api}/issues/{issue['number']}",
            "-f",
            "state=open",
            "--jq",
            ".number",
        )
        issue["state"] = "open"
        issue["state_reason"] = None
        print(f"#{issue['number']} ({phase['title']}) reopened: tasks.md has unchecked tasks")
    return reopened


def reopen_incomplete_epic(api, epic, all_complete, apply):
    """Reopen a completed Epic when its current tasks.md has incomplete Phases again."""
    if (
        not epic
        or epic.get("state") != "closed"
        or epic.get("state_reason") != "completed"
        or all_complete
    ):
        return False
    if not apply:
        dry_run(f"reopen epic #{epic['number']} (tasks.md has incomplete Phases)")
        return True
    gh(
        "api",
        "-X",
        "PATCH",
        f"{api}/issues/{epic['number']}",
        "-f",
        "state=open",
        "--jq",
        ".number",
    )
    epic["state"] = "open"
    epic["state_reason"] = None
    print(f"epic #{epic['number']} reopened: tasks.md has incomplete Phases")
    return True


def update_issue_content(api, issue, label, title, body, apply, allow_closed=False):
    """Patch an issue whose title or body no longer matches tasks.md. Returns True if changed."""
    if issue.get("state") == "closed" and not allow_closed:
        return False
    if issue["title"] == title and (issue["body"] or "").strip() == body.strip():
        return False
    if not apply:
        dry_run(f"update #{issue['number']} ({label})")
        return True
    gh(
        "api",
        "-X",
        "PATCH",
        f"{api}/issues/{issue['number']}",
        "-f",
        f"title={title}",
        "-f",
        f"body={body}",
        "--jq",
        ".number",
    )
    print(f"#{issue['number']} ({label}) updated")
    return True


def update_existing_issues(
    api, feature_path, feature, epic, phase_issues, apply, epic_content
):
    """Update open issues; epic_content=(title, body) is the rendered roadmap Epic."""
    updated = 0
    if epic:
        title, body = epic_content
        updated += update_issue_content(api, epic, "epic", title, body, apply)
    for phase in feature["phases"]:
        issue = phase_issues.get(phase["key"])
        if issue:
            updated += update_issue_content(
                api,
                issue,
                phase["title"],
                phase_title(phase),
                phase_body(feature_path, feature, phase),
                apply,
                allow_closed=(
                    issue.get("state") == "closed"
                    and issue.get("state_reason") == "completed"
                    and issue.get("title", "").startswith("Phase 1: Kickoff")
                ),
            )
    return updated


def retire_removed_phases(api, removed, apply):
    """Close open Planned Phase issues deleted from tasks.md as not planned (retiring the number)."""
    closed = 0
    for key, issue in removed:
        if issue.get("state") == "closed":
            if issue.get("state_reason") == "not_planned":
                body = (issue.get("body") or "").replace(PHASE_PAUSED_MARKER, "").rstrip()
                if PHASE_RETIRED_MARKER not in body:
                    body = f"{body}\n\n{PHASE_RETIRED_MARKER}\n"
                    if not apply:
                        dry_run(f"mark #{issue['number']} ({key}) as retired")
                    else:
                        gh(
                            "api",
                            "-X",
                            "PATCH",
                            f"{api}/issues/{issue['number']}",
                            "-f",
                            f"body={body}",
                            "--jq",
                            ".number",
                        )
                        issue["body"] = body
                        print(f"#{issue['number']} ({key}) marked as retired")
            continue
        closed += 1
        if not apply:
            dry_run(f"close #{issue['number']} ({key} removed from tasks.md)")
            continue
        comment = (
            f"{key} was removed from tasks.md while still Planned, so this issue is closed "
            "as not planned. Phase numbers are never reused."
        )
        github_issue_comments.ensure_issue_comment(
            gh,
            api,
            issue["number"],
            comment,
            f"<!-- speckit-phase-retirement: {key} -->",
            apply,
        )
        gh(
            "api",
            "-X",
            "PATCH",
            f"{api}/issues/{issue['number']}",
            "-f",
            f"body={(issue.get('body') or '').rstrip()}\n\n{PHASE_RETIRED_MARKER}\n",
            "-f",
            "state=closed",
            "-f",
            "state_reason=not_planned",
            "--jq",
            ".number",
        )
        issue["body"] = f"{(issue.get('body') or '').rstrip()}\n\n{PHASE_RETIRED_MARKER}\n"
        issue["state"] = "closed"
        issue["state_reason"] = "not_planned"
        print(f"#{issue['number']} ({key}) closed as not planned")
    return closed


def sync_project(owner, repo, args, phase_issues, feature, apply, removed=()):
    """Add every open Phase to the Project as Planned; later statuses are never changed here.

    Retired Phases leave the Project (the issue stays closed as not planned): a Phase retired
    in an earlier run is removed on any later run, and `removed` Phases are previewed in a dry run.
    """
    import project_board  # noqa: PLC0415  (project_board loads this module; import lazily)

    project = project_board.Project(args.project_owner, args.project_number)
    changed = 0
    for phase in feature["phases"]:
        issue = phase_issues.get(phase["key"])
        if not issue:
            if not apply:
                dry_run(
                    f"add {phase['title']} to project "
                    f"{args.project_owner}/{args.project_number} as Planned"
                )
                changed += 1
            continue
        changed += project_board.ensure_planned(owner, repo, project, issue, apply, dry_run)
    # `removed` holds the issues this run retires: open ones are closed, paused ones (closed as
    # not planned) swap their marker, so both count for the dry-run preview.
    retiring = {
        issue["number"]: issue
        for _, issue in removed
        if issue.get("state") != "closed" or issue.get("state_reason") == "not_planned"
    }
    retired = {issue["number"]: issue for issue in phase_issues.values() if is_retired(issue)}
    for number, issue in sorted({**retiring, **retired}.items()):
        changed += project_board.ensure_removed(owner, repo, project, issue, apply, dry_run)
    return changed


def sync_phase_statuses(owner, repo, args, state_changes, apply):
    """Set the board Status of Phases this run closed or reopened, using the status sync's rules.

    Changes made with GITHUB_TOKEN do not trigger the status workflow, so the board would stay
    stale until its daily run. A dry run previews the status the new state will get.
    """
    status = load_status_sync()
    for number in sorted(state_changes):
        status.reconcile(
            owner,
            repo,
            issue_number=number,
            apply=apply,
            project_owner=args.project_owner,
            project_number=args.project_number,
            state_overrides={number: state_changes[number]},
        )


def project_arguments(args):
    if args.skip_project:
        return
    if args.project_owner is None or args.project_number is None:
        try:
            board = project_context.load_context(project_context.DEFAULT_CONFIG)["github_project"]
        except (OSError, ValueError) as error:
            sys.exit(f"Invalid project context TOML {project_context.DEFAULT_CONFIG}: {error}")
        if args.project_owner is None:
            args.project_owner = board["owner"]
        if args.project_number is None:
            args.project_number = board["number"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--feature",
        help="Feature directory or tasks.md path; auto-detected when exactly one exists",
    )
    parser.add_argument(
        "--roadmap-only",
        action="store_true",
        help="Skip features without a matching ROADMAP.md entry and require its managed Epic",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--apply", action="store_true")
    mode_group.add_argument(
        "--check",
        action="store_true",
        help="Make no changes; exit 1 when any change is pending, 0 when issues are in sync",
    )
    parser.add_argument("--skip-project", action="store_true", help="Skip GitHub Project changes")
    parser.add_argument("--project-owner", help="default: github_project.owner in project-context.toml")
    parser.add_argument(
        "--project-number", type=int, help="default: github_project.number in project-context.toml"
    )
    args = parser.parse_args(argv)
    PENDING.clear()
    project_arguments(args)

    feature_path = resolve_feature_path(args.feature)
    feature_dir = ROOT / feature_path
    feature = parse_feature(feature_dir / "tasks.md")
    roadmap_id = roadmap_issues.roadmap_id_for_path(feature_path)
    roadmap_entry = None
    if args.roadmap_only:
        if not roadmap_id:
            print(f"Skipping Phase sync for {feature_path}: no roadmap ID in its directory name.")
            return 0
        roadmap_entry = roadmap_issues.find_roadmap_entry(ROOT, roadmap_id)
        if roadmap_entry is None:
            print(f"Skipping Phase sync for {feature_path}: no matching ROADMAP.md entry.")
            return 0
        if roadmap_entry["status"] in {"undecided", "deferred", "needs-info", "abandoned"}:
            print(
                f"Skipping Phase sync for roadmap entry {roadmap_id} "
                f"({roadmap_entry['status']})."
            )
            return 0

    if not roadmap_id:
        sys.exit(
            f"No epic issue tracks {feature_path}: its directory name has no roadmap ID prefix."
        )

    owner, repo = origin_repo()
    repository = f"{owner}/{repo}"
    api = f"repos/{owner}/{repo}"
    issues = load_issues(api)
    phase_issues = find_phase_issues(issues, feature_path)
    roadmap_epic, planning_issues = roadmap_issues.index_roadmap_issues(issues, roadmap_id)
    if roadmap_entry is None:
        roadmap_entry = roadmap_issues.load_roadmap_entry(ROOT, roadmap_id)
    if roadmap_entry["status"] in {"undecided", "deferred", "needs-info", "abandoned"}:
        print(
            f"Skipping Phase sync for roadmap entry {roadmap_id} "
            f"({roadmap_entry['status']})."
        )
        return 0
    if not roadmap_epic:
        if args.roadmap_only and not (args.apply or args.check):
            warning = (
                f"no managed roadmap Epic for {roadmap_id} yet; "
                f"run sync-roadmap-issues.py --apply first. Skipping {feature_path}."
            )
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::warning::{warning}")
            else:
                print(f"Warning: {warning}")
            return 0
        sys.exit(
            f"No managed roadmap Epic found for entry {roadmap_id}. "
            f"Run `python3 scripts/sync-roadmap-issues.py --id {roadmap_id} --apply` first."
        )

    retired = reused_retired_phases(phase_issues, feature)
    if retired:
        sys.exit(
            f"{feature_path}/tasks.md defines "
            + ", ".join(f"{key} (#{issue['number']})" for key, issue in retired)
            + ", but that Phase number is retired (its issue is closed as not planned). "
            "Phase numbers are never reused: give the Phase a new number, or reopen the issue "
            "if retiring it was a mistake, then re-run the sync."
        )
    removed = find_removed_phases(phase_issues, feature)
    blocked = blocked_removed_phases(api, repository, removed) if removed else []
    if blocked:
        stop_for_removed_phases(feature_path, blocked)

    print(
        f"Synchronizing {feature['title']} from {feature_path}: "
        f"{len(feature['phases'])} phases, {len(feature['tasks'])} tasks"
    )
    print(f"Using roadmap epic #{roadmap_epic['number']} for roadmap entry {roadmap_id}")

    missing = [phase for phase in feature["phases"] if not phase_issues.get(phase["key"])]
    for phase in missing:
        title = phase_title(phase)
        if args.apply:
            print(
                f"{phase['key']} created "
                f"{create_issue(repository, title, phase_body(feature_path, feature, phase), 'Story')}"
            )
        else:
            dry_run(f"create phase: {title}")
    if args.apply and missing:
        phase_issues = wait_for_phase_issues(api, feature_path, feature)

    state_changes = {}
    phases_reopened = reopen_incomplete_phases(
        api, feature, phase_issues, args.apply, state_changes
    )

    planning_numbers = {key: issue["number"] for key, issue in planning_issues.items() if issue}
    epic_content = (
        roadmap_epic["title"],
        roadmap_issues.roadmap_epic_body(
            roadmap_entry,
            planning_numbers,
            roadmap_issues.feature_path(roadmap_entry, feature_path),
            roadmap_issues.existing_artifacts(ROOT, feature_path),
        ),
    )
    issues_updated = update_existing_issues(
        api, feature_path, feature, roadmap_epic, phase_issues, args.apply, epic_content
    )
    removed_closed = retire_removed_phases(api, removed, args.apply)

    hierarchy_results = []
    parent_cache = {}
    for phase in feature["phases"]:
        hierarchy_results.append(
            sync_parent(
                api,
                roadmap_epic,
                phase_issues.get(phase["key"]),
                "epic",
                phase["title"],
                args.apply,
                parent_cache,
            )
        )

    project_changes = 0
    if not args.skip_project:
        project_changes = sync_project(
            owner, repo, args, phase_issues, feature, args.apply, removed
        )

    all_complete = all(phase_complete(feature, phase) for phase in feature["phases"])
    epic_reopened = reopen_incomplete_epic(api, roadmap_epic, all_complete, args.apply)
    closed, already_closed = close_completed_issues(
        api,
        completed_issues(
            feature,
            roadmap_epic,
            phase_issues,
            planning_issues,
        ),
        args.apply,
        state_changes,
    )
    if not args.skip_project and state_changes:
        sync_phase_statuses(owner, repo, args, state_changes, args.apply)
    if roadmap_epic.get("state") != "closed":
        reasons = roadmap_issues.epic_close_blockers(all_complete, planning_issues)
        if reasons:
            print(f"Epic #{roadmap_epic['number']} stays open: {'; '.join(reasons)}")
    hierarchy_added = hierarchy_results.count("added")
    hierarchy_moved = hierarchy_results.count("moved")
    hierarchy_skipped = hierarchy_results.count("skipped")
    mode = "applied" if args.apply else "dry run"
    print(
        f"\nHierarchy: {len(hierarchy_results)} relationships "
        f"({hierarchy_added} added, {hierarchy_moved} moved, {hierarchy_skipped} already present; {mode})"
    )
    print(
        f"Content: {issues_updated} issues updated, {phases_reopened} phases reopened, "
        f"{removed_closed} removed phases retired; {mode}"
    )
    if epic_reopened:
        print(f"Epic: reopened because {feature_path}/tasks.md has incomplete Phases; {mode}")
    if not args.skip_project:
        print(f"Project: {project_changes} phases set to Planned; {mode}")
    print(f"Completed issues: {closed} closed, {already_closed} already closed; {mode}")

    if args.check:
        if PENDING:
            print(
                f"\nGitHub issues are out of sync with {feature_path}: "
                f"{len(PENDING)} pending change(s). Re-run the sync with --apply."
            )
            return 1
        print(f"\nGitHub issues are in sync with {feature_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
