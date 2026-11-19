#!/usr/bin/env python3
"""Synchronize ROADMAP.md entries with GitHub issues.

Without --apply, the command is read-only and prints the changes it would make.
Roadmap epics, the spec-plan planning issue and the provisional Phase 1 issue are always found
by their markers, never by title. Once tasks.md exists, the Phase issues belong to
sync-task-issues.py, which replaces the provisional Phase 1 title and body from tasks.md.
"""

import argparse
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_board  # noqa: E402
import project_context  # noqa: E402
import roadmap_issues  # noqa: E402
import github_issue_comments  # noqa: E402

sync = project_board.sync

PLANNING_LABELS = ("Planning",)
NOT_PLANNED_COMMENTS = {
    "deferred": "This roadmap entry is deferred. Its issues are closed as not planned.",
    "abandoned": "This roadmap entry is abandoned. Its issues are closed as not planned.",
    "removed": "This roadmap entry was removed from ROADMAP.md. Its issues are closed as not planned.",
    "needs-info": "This roadmap entry needs more information. Its managed issues are closed as not planned.",
}
CREATE_PAUSE_SECONDS = 0.25
def entry_spec_paths(root, entry):
    paths = set()
    if entry.get("spec_dir"):
        paths.add(entry["spec_dir"].strip("/"))
    for path in (root / "specs").glob(f"{entry['id']}-*"):
        if path.is_dir():
            paths.add(path.relative_to(root).as_posix())
    return sorted(paths)


def feature_path_for(root, entry):
    paths = entry_spec_paths(root, entry)
    preferred_path = paths[0] if len(paths) == 1 else None
    return roadmap_issues.feature_path(entry, preferred_path)


def find_phase1(issues, feature_path):
    """Return the Phase 1 issue of the feature directory (provisional or generated), or None."""
    roadmap_id = roadmap_issues.roadmap_id_for_path(feature_path)
    path_match = sync.find_phase_issues(issues, feature_path).get("phase-1")
    if not roadmap_id:
        return path_match
    stable_marker = roadmap_issues.roadmap_phase_marker(roadmap_id, "phase-1")
    stable_matches = [
        issue
        for issue in roadmap_issues.trusted_issues(issues)
        if stable_marker in (issue.get("body") or "")
    ]
    matches = {issue["number"]: issue for issue in stable_matches}
    if path_match:
        matches[path_match["number"]] = path_match
    return roadmap_issues.only_match(
        list(matches.values()),
        f"Phase 1 for roadmap entry {roadmap_id}",
        roadmap_issues.duplicate_hint(stable_marker),
    )


def is_provisional(root, feature_path):
    """Phase 1 is provisional until tasks.md exists; afterwards sync-task-issues.py owns it."""
    return not (Path(root) / feature_path / "tasks.md").is_file()


def provisional_phase1(root, issues, entry):
    """Return (feature path, Phase 1 issue) when the Phase 1 issue is still provisional."""
    feature_path = feature_path_for(root, entry)
    if not is_provisional(root, feature_path):
        return feature_path, None
    return feature_path, find_phase1(issues, feature_path)


def dry_run(message):
    print(f"[dry-run] {message}")


def phase_state_body(issue, marker):
    body = (issue.get("body") or "").replace(sync.PHASE_PAUSED_MARKER, "")
    body = body.replace(sync.PHASE_RETIRED_MARKER, "").rstrip()
    return f"{body}\n\n{marker}\n" if marker else body


def close_as_not_planned(api, issue, reason, apply, phase_marker=None):
    if not issue:
        return False
    body = issue.get("body") or ""
    if phase_marker:
        body = phase_state_body(issue, phase_marker)
    if issue.get("state") == "closed":
        if (
            phase_marker != sync.PHASE_RETIRED_MARKER
            or issue.get("state_reason") != "not_planned"
            or body == (issue.get("body") or "")
        ):
            return False
        if not apply:
            dry_run(f"mark Phase #{issue['number']} as retired")
        else:
            sync.gh(
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
            print(f"Phase #{issue['number']} marked as retired")
        return True
    if not apply:
        dry_run(f"close #{issue['number']} as not planned ({reason})")
    github_issue_comments.ensure_issue_comment(
        sync.gh,
        api,
        issue["number"],
        NOT_PLANNED_COMMENTS[reason],
        f"<!-- speckit-roadmap-close: {reason} -->",
        apply,
    )
    if not apply:
        return True
    sync.gh(
        "api",
        "-X",
        "PATCH",
        f"{api}/issues/{issue['number']}",
        "-f",
        f"body={body}",
        "-f",
        "state=closed",
        "-f",
        "state_reason=not_planned",
    )
    issue["body"] = body
    issue["state"] = "closed"
    issue["state_reason"] = "not_planned"
    print(f"#{issue['number']} closed as not planned ({reason})")
    return True


def phase_close_blockers(api, repository, phase_issues):
    """Return Phase issues with active or completed work that needs explicit resolution."""
    if not phase_issues:
        return []
    branches, pull_requests = sync.active_phase_work(api)
    blocked = []
    for key, issue in phase_issues.items():
        if issue.get("state") == "closed":
            if issue.get("state_reason") == "completed":
                blocked.append((key, issue, "Done"))
            continue
        if sync.phase_in_review(pull_requests, issue, repository):
            blocked.append((key, issue, "In review"))
        elif (issue["number"], int(key.removeprefix("phase-"))) in branches:
            blocked.append((key, issue, "In progress"))
    return blocked


def require_phases_resolved(api, repository, phase_issues, roadmap_id, removed=False):
    blocked = phase_close_blockers(api, repository, phase_issues)
    if blocked:
        details = ", ".join(
            f"{key} (#{issue['number']}, {status})" for key, issue, status in blocked
        )
        if removed:
            open_work = any(status != "Done" for _, _, status in blocked)
            reminder = (
                " For In progress or In review Phases, close or retarget its open pull "
                "request and delete its branch, or finish that work, before retiring the Phase."
                if open_work
                else ""
            )
            return (
                f"Roadmap entry {roadmap_id} was removed from ROADMAP.md, but Phase work "
                f"needs a decision: {details}. Either restore the entry in ROADMAP.md, or "
                "retire each listed Phase by hand: close the Phase as not planned and label "
                "it abandoned." + reminder + " Sync keeps stopping until then."
            )
        return (
            f"Roadmap entry {roadmap_id} cannot be closed while Phase work is active: "
            f"{details}. Finish or close each branch/pull request, or reopen a completed "
            "Phase and resolve its tasks.md work before retrying."
        )
    return None


def reopen_paused_phase(repository, issue, apply):
    """Reopen only a Phase paused by roadmap lifecycle and remove its pause marker."""
    if (
        not issue
        or issue.get("state") != "closed"
        or issue.get("state_reason") != "not_planned"
        or sync.PHASE_PAUSED_MARKER not in (issue.get("body") or "")
        or "abandoned" in (issue.get("labels") or [])
    ):
        return False
    if not apply:
        dry_run(f"reopen deferred Phase #{issue['number']}")
    else:
        body = (issue.get("body") or "").replace(sync.PHASE_PAUSED_MARKER, "").rstrip()
        sync.gh(
            "api",
            "-X",
            "PATCH",
            f"repos/{repository}/issues/{issue['number']}",
            "-f",
            "state=open",
            "-f",
            f"body={body}",
        )
        issue["state"] = "open"
        issue["state_reason"] = None
        issue["body"] = body
        print(f"Phase #{issue['number']} reopened")
    return True


def update_labels(repository, issue, add=(), remove=(), apply=False):
    if not issue or (issue.get("state") == "closed" and issue.get("state_reason") == "completed"):
        return False
    labels = set(issue.get("labels") or [])
    additions = [label for label in add if label not in labels]
    removals = [label for label in remove if label in labels]
    if not additions and not removals:
        return False
    if not apply:
        for label in additions:
            dry_run(f"add label {label} to #{issue['number']}")
        for label in removals:
            dry_run(f"remove label {label} from #{issue['number']}")
        return True
    args = ["issue", "edit", str(issue["number"]), "--repo", repository]
    for label in additions:
        args.extend(["--add-label", label])
    for label in removals:
        args.extend(["--remove-label", label])
    sync.gh(*args)
    issue["labels"] = sorted((labels | set(additions)) - set(removals))
    return True


def reopen_if_deferred(repository, issue, apply):
    if not issue or issue.get("state") != "closed":
        return False
    if issue.get("state_reason") == "completed":
        return False
    if "abandoned" in (issue.get("labels") or []):
        print(f"warning: #{issue['number']} is abandoned; it will not be reopened automatically")
        return False
    if issue.get("state_reason") != "not_planned":
        print(
            f"warning: #{issue['number']} is closed as "
            f"{issue.get('state_reason') or 'unknown'}; it will not be reopened automatically"
        )
        return False
    if not apply:
        dry_run(f"reopen deferred issue #{issue['number']}")
    else:
        sync.gh("issue", "reopen", str(issue["number"]), "--repo", repository)
        issue["state"] = "open"
        issue["state_reason"] = None
        print(f"#{issue['number']} reopened")
    return True


def create_if_missing(repository, title, body, labels, apply):
    if not apply:
        dry_run(f"create {title}")
        return True
    print(f"created {sync.create_issue(repository, title, body, labels)}")
    time.sleep(CREATE_PAUSE_SECONDS)
    return True


def sync_roadmap_entry(root, api, repository, entry, issues, apply):
    roadmap_id = entry["id"]
    epic, planning = roadmap_issues.index_roadmap_issues(issues, roadmap_id)
    status = entry["status"]
    feature_path, phase1 = provisional_phase1(root, issues, entry)
    phase_issues = sync.find_phase_issues(issues, feature_path)
    managed = [epic, *planning.values(), *phase_issues.values()]

    if status == "undecided":
        return

    if status in {"deferred", "abandoned"}:
        reason = status
        blocked = require_phases_resolved(api, repository, phase_issues, roadmap_id)
        if blocked:
            print(f"[skip] {blocked}")
            return blocked
        if status == "abandoned":
            for issue in managed:
                update_labels(repository, issue, add=("abandoned",), apply=apply)
        for issue in managed:
            close_as_not_planned(
                api,
                issue,
                reason,
                apply,
                phase_marker=(
                    sync.PHASE_RETIRED_MARKER
                    if status == "abandoned" and issue in phase_issues.values()
                    else sync.PHASE_PAUSED_MARKER
                    if issue in phase_issues.values()
                    else None
                ),
            )
        return

    if status == "needs-info":
        blocked = require_phases_resolved(api, repository, phase_issues, roadmap_id)
        if blocked:
            print(f"[skip] {blocked}")
            return blocked
        if not epic:
            body = roadmap_issues.roadmap_epic_body(entry, {}, feature_path, ())
            create_if_missing(
                repository,
                roadmap_issues.epic_title(entry),
                body,
                ("Epic", "needs-info"),
                apply,
            )
        else:
            reopen_if_deferred(repository, epic, apply)
            body = roadmap_issues.roadmap_epic_body(
                entry,
                {key: issue["number"] for key, issue in planning.items() if issue},
                feature_path,
                roadmap_issues.existing_artifacts(root, feature_path),
            )
            sync.update_issue_content(api, epic, "epic", roadmap_issues.epic_title(entry), body, apply)
        update_labels(repository, epic, add=("Epic", "needs-info"), apply=apply)
        for issue in planning.values():
            close_as_not_planned(api, issue, "needs-info", apply)
        for issue in phase_issues.values():
            close_as_not_planned(
                api, issue, "needs-info", apply, phase_marker=sync.PHASE_PAUSED_MARKER
            )
        return

    if status != "planned":
        sys.exit(f"Roadmap entry {roadmap_id} has unsupported status {status!r}")
    if epic and epic.get("state") == "closed" and epic.get("state_reason") == "completed":
        print(f"skip {roadmap_id}: epic #{epic['number']} is closed as completed")
        return
    if epic and "abandoned" in (epic.get("labels") or []):
        print(f"warning: {roadmap_id} is abandoned; it will not be reopened automatically")
        return

    if not epic:
        body = roadmap_issues.roadmap_epic_body(entry, {}, feature_path, roadmap_issues.existing_artifacts(root, feature_path))
        create_if_missing(repository, roadmap_issues.epic_title(entry), body, "Epic", apply)
    else:
        reopen_if_deferred(repository, epic, apply)
        body = roadmap_issues.roadmap_epic_body(
            entry,
            {key: issue["number"] for key, issue in planning.items() if issue},
            feature_path,
            roadmap_issues.existing_artifacts(root, feature_path),
        )
        sync.update_issue_content(api, epic, "epic", roadmap_issues.epic_title(entry), body, apply)
    update_labels(repository, epic, add=("Epic",), remove=("needs-info",), apply=apply)
    for key, _ in roadmap_issues.PLANNING:
        if planning[key]:
            reopen_if_deferred(repository, planning[key], apply)
            continue
        create_if_missing(
            repository,
            roadmap_issues.planning_title(roadmap_id, key),
            roadmap_issues.spec_plan_body(entry, feature_path),
            PLANNING_LABELS,
            apply,
        )
    for issue in planning.values():
        update_labels(repository, issue, add=PLANNING_LABELS, apply=apply)

    for issue in phase_issues.values():
        reopen_paused_phase(repository, issue, apply)

    if not is_provisional(root, feature_path):
        return
    if phase1:
        sync.update_issue_content(
            api,
            phase1,
            "provisional Phase 1",
            roadmap_issues.provisional_phase_title(entry),
            roadmap_issues.provisional_phase_body(entry, feature_path),
            apply,
        )
        update_labels(repository, phase1, add=("Story",), apply=apply)
    else:
        create_if_missing(
            repository,
            roadmap_issues.provisional_phase_title(entry),
            roadmap_issues.provisional_phase_body(entry, feature_path),
            "Story",
            apply,
        )


def reload_issues(root, api, entries, attempts=6, delay=2):
    missing = []
    for attempt in range(attempts):
        issues = sync.load_issues(api)
        indexed = {
            entry["id"]: roadmap_issues.index_roadmap_issues(issues, entry["id"])
            for entry in entries
        }
        missing = []
        for entry in entries:
            epic, planning = indexed[entry["id"]]
            if not epic:
                missing.append(f"{entry['id']} epic")
            elif entry["status"] == "planned":
                missing.extend(
                    f"{entry['id']} {key} planning issue"
                    for key, issue in planning.items()
                    if not issue
                )
                if provisional_phase1(root, issues, entry)[1] is None and is_provisional(
                    root, feature_path_for(root, entry)
                ):
                    missing.append(f"{entry['id']} provisional Phase 1 issue")
        if not missing:
            return issues
        if attempt + 1 < attempts:
            time.sleep(delay)
    sys.exit(
        "Created roadmap issues did not become visible in time: " + ", ".join(missing)
    )


def sync_planning_relationships(root, api, entry, epic, planning, phase1, apply):
    if entry["status"] != "planned":
        return
    provisional = is_provisional(root, feature_path_for(root, entry))
    if not epic:
        if not apply:
            dry_run(
                f"make spec-plan planning issue for {entry['id']} a sub-issue of its epic"
            )
            if provisional:
                dry_run(f"make provisional Phase 1 for {entry['id']} a sub-issue of its epic")
                dry_run(
                    f"make provisional Phase 1 for {entry['id']} blocked by its spec-plan issue"
                )
        return
    if epic.get("state") == "closed" and epic.get("state_reason") == "completed":
        return
    if "abandoned" in (epic.get("labels") or []):
        return
    parent_cache = {}
    spec_plan = planning["spec-plan"]
    children = [("spec-plan planning issue", spec_plan)]
    if provisional or phase1:
        children.append(("provisional Phase 1", phase1 if provisional else None))
    for label, child in children:
        if not child and not apply:
            if label != "provisional Phase 1" or provisional:
                dry_run(f"make {label} for {entry['id']} a sub-issue of its epic")
            continue
        if not child or child.get("state") == "closed" and child.get("state_reason") == "completed":
            continue
        sync.sync_parent(api, epic, child, "epic", label, apply, parent_cache)

    if not phase1:
        if provisional and not apply:
            dry_run(
                f"make provisional Phase 1 for {entry['id']} blocked by its spec-plan issue"
            )
        return
    if phase1.get("state") == "closed" or not spec_plan:
        return
    blockers = sync.current_blocker_numbers(api, phase1)
    if spec_plan.get("state") == "closed" and spec_plan.get("state_reason") == "completed":
        if spec_plan["number"] not in blockers:
            return
        message = (
            f"remove blocker #{spec_plan['number']} (spec-plan, merged) from "
            f"#{phase1['number']} (Phase 1)"
        )
        if not apply:
            dry_run(message)
        else:
            sync.gh(
                "api",
                "-X",
                "DELETE",
                f"{api}/issues/{phase1['number']}/dependencies/blocked_by/{spec_plan['id']}",
            )
            print(message)
        return
    if not provisional or spec_plan["number"] in blockers:
        return
    message = f"#{phase1['number']} (Phase 1) blocked by #{spec_plan['number']} (spec-plan)"
    if not apply:
        dry_run(message)
    else:
        sync.gh(
            "api",
            "-X",
            "POST",
            f"{api}/issues/{phase1['number']}/dependencies/blocked_by",
            "-F",
            f"issue_id={spec_plan['id']}",
            "--jq",
            ".number",
        )
        print(message)


def ensure_project_item(owner, repo, project, order_field, issue, order, apply):
    if not issue or (issue.get("state") == "closed" and issue.get("state_reason") == "completed"):
        return
    if "abandoned" in (issue.get("labels") or []):
        return
    item = project_board.project_item_details(
        owner, repo, issue["number"], project.owner, project.number
    )
    if item:
        item_id, _, current = item
    elif apply:
        item_id = project_board.add_issue_to_project(owner, repo, issue["number"], project)
        current = None
    else:
        dry_run(f"add #{issue['number']} to project {project.owner}/{project.number}")
        item_id = None
        current = None
    if order_field is None:
        return
    if item_id is None:
        dry_run(f"set #{issue['number']} Roadmap order to {order}")
        return
    if current == order:
        return
    if not apply:
        dry_run(f"set #{issue['number']} Roadmap order to {order}")
    else:
        project.set_number(item_id, order)


def sync_project(
    root, owner, repo, entries, all_entries, issue_map, phase_map, project_owner, project_number,
    apply,
):
    project = project_board.Project(project_owner, project_number)
    order_field = project.number_field()
    if order_field is None:
        print(
            f"Project {project_owner}/{project_number} has no 'Roadmap order' number field; "
            "create that number field to enable roadmap ordering."
        )
    position = {entry["id"]: index for index, entry in enumerate(all_entries, start=1)}
    for entry in entries:
        if entry["status"] not in {"planned", "needs-info"}:
            continue
        epic, planning = issue_map.get(entry["id"], (None, {}))
        provisional = is_provisional(root, feature_path_for(root, entry))
        if not apply and not epic:
            issues_to_add = "the roadmap Epic"
            dry_run(
                f"add {issues_to_add} for {entry['id']} "
                f"to project {project_owner}/{project_number}"
            )
            if order_field is not None:
                dry_run(
                    f"set Roadmap order for {entry['id']} roadmap Epic to "
                    f"{position[entry['id']]}"
                )
            if entry["status"] != "needs-info":
                dry_run(
                    f"add planning issues for {entry['id']} "
                    f"to project {project_owner}/{project_number}"
                )
                if provisional:
                    dry_run(
                        f"add provisional Phase 1 for {entry['id']} to project "
                        f"{project_owner}/{project_number} as Planned"
                    )
            continue
        ensure_project_item(
            owner, repo, project, order_field, epic, position[entry["id"]], apply
        )
        if entry["status"] != "needs-info":
            for issue in planning.values():
                ensure_project_item(owner, repo, project, None, issue, None, apply)
            phase1 = phase_map.get(entry["id"])
            if provisional and phase1 is None and not apply:
                dry_run(
                    f"add provisional Phase 1 for {entry['id']} to project "
                    f"{project_owner}/{project_number} as Planned"
                )
            elif provisional and phase1 and "abandoned" not in (phase1.get("labels") or []):
                project_board.ensure_planned(owner, repo, project, phase1, apply, dry_run)


def sync_dependencies(api, source_ids, entries_by_id, issue_map, apply):
    ids_by_number = {
        epic["number"]: roadmap_id
        for roadmap_id, (epic, _) in issue_map.items()
        if epic and roadmap_issues.roadmap_marker(roadmap_id) in (epic.get("body") or "")
    }
    epic_by_id = {
        roadmap_id: epic
        for roadmap_id, (epic, _) in issue_map.items()
        if epic
    }
    managed_issue_by_number = {issue["number"]: issue for issue in epic_by_id.values()}

    # sorted: the dry run's stdout must not depend on set order (hash randomisation), because
    # `bootstrap.py start` hashes it and recomputes the hash in a later process
    for roadmap_id in sorted(source_ids):
        epic = issue_map.get(roadmap_id, (None, {}))[0]
        entry = entries_by_id.get(roadmap_id)
        if not epic:
            if not apply and entry and entry["status"] in {
                "planned", "needs-info"
            }:
                for dependency in sorted(set(
                    re.findall(r"\b\d{3}\b", entry["fields"].get("Depends on", ""))
                )):
                    if dependency in epic_by_id:
                        dry_run(
                            f"make new epic for {roadmap_id} blocked by "
                            f"epic for {dependency}"
                        )
                    elif (
                        dependency in source_ids
                        and dependency in entries_by_id
                        and entries_by_id[dependency]["status"]
                        in {"planned", "needs-info"}
                    ):
                        dry_run(
                            f"make new epic for {roadmap_id} blocked by "
                            f"new epic for {dependency}"
                        )
                    else:
                        dry_run(
                            f"no tracked epic for dependency {dependency}; "
                            f"no blocker link for {roadmap_id}"
                        )
            continue
        if roadmap_issues.roadmap_marker(roadmap_id) not in (epic.get("body") or ""):
            continue
        if epic.get("state") == "closed" and epic.get("state_reason") == "completed":
            continue
        if entry and entry["status"] in {"planned", "needs-info"}:
            dependencies = set(re.findall(r"\b\d{3}\b", entry["fields"].get("Depends on", "")))
        else:
            dependencies = set()
        desired = {
            epic_by_id[dependency]["number"]
            for dependency in dependencies
            if dependency in epic_by_id
        }
        current = sync.current_blocker_numbers(api, epic)
        if not apply:
            for dependency in sorted(dependencies - set(epic_by_id)):
                if (
                    dependency in source_ids
                    and dependency in entries_by_id
                    and entries_by_id[dependency]["status"]
                    in {"planned", "needs-info"}
                ):
                    dry_run(
                        f"make epic for {roadmap_id} blocked by new epic for {dependency}"
                    )
                else:
                    dry_run(
                        f"no tracked epic for dependency {dependency}; "
                        f"no blocker link for {roadmap_id}"
                    )
        for dependency in sorted(desired - current):
            target = next(item for item in epic_by_id.values() if item["number"] == dependency)
            message = f"#{epic['number']} blocked by #{target['number']} ({roadmap_id} depends on its entry)"
            if not apply:
                dry_run(message)
            else:
                sync.gh(
                    "api",
                    "-X",
                    "POST",
                    f"{api}/issues/{epic['number']}/dependencies/blocked_by",
                    "-F",
                    f"issue_id={target['id']}",
                    "--jq",
                    ".number",
                )
                print(message)
        for dependency in sorted(current - desired):
            if dependency not in ids_by_number:
                continue
            message = f"remove managed blocker #{dependency} from epic #{epic['number']}"
            if not apply:
                dry_run(message)
            else:
                sync.gh(
                    "api",
                    "-X",
                    "DELETE",
                    f"{api}/issues/{epic['number']}/dependencies/blocked_by/"
                    f"{managed_issue_by_number[dependency]['id']}",
                )
                print(message)


def marker_roadmap_ids(issues):
    """Roadmap IDs carried by the Epic markers of trusted issues; an outsider's marker adds none."""
    ids = set()
    for issue in roadmap_issues.trusted_issues(issues):
        marker = re.search(r"<!-- speckit-roadmap: (\d{3}) -->", issue.get("body") or "")
        if marker:
            ids.add(marker.group(1))
    return ids


def close_removed_entries(root, api, repository, entries, issues, apply):
    present = set(entries)
    removed = {}
    # An outsider's pasted marker must never get an issue closed, labelled or commented on.
    issues = roadmap_issues.trusted_issues(issues)
    for issue in issues:
        body = issue.get("body") or ""
        epic_match = re.search(r"<!-- speckit-roadmap: (\d{3}) -->", body)
        planning_match = re.search(r"<!-- speckit-planning: (\d{3})#spec-plan -->", body)
        match = epic_match or planning_match
        if match and match.group(1) not in present:
            removed.setdefault(match.group(1), []).append(issue)
        # Phases are collected below by find_phase_issues; here they only reveal an entry
        # whose Epic and planning issues are already gone.
        for phase_match in re.finditer(r"<!-- speckit-roadmap-phase: (\d{3})#phase-\d+ -->", body):
            if phase_match.group(1) not in present:
                removed.setdefault(phase_match.group(1), [])
    blocked_entries = set()
    for roadmap_id, removed_issues in removed.items():
        print(f"entry {roadmap_id} was removed from ROADMAP.md; treating it as abandoned")
        phase_issues = sync.find_phase_issues(issues, roadmap_id=roadmap_id)
        blocked = require_phases_resolved(
            api, repository, phase_issues, roadmap_id, removed=True
        )
        if blocked:
            print(f"[skip] {blocked}")
            blocked_entries.add(roadmap_id)
            continue
        removed_issues.extend(phase_issues.values())
        phase_numbers = {issue["number"] for issue in phase_issues.values()}
        for issue in removed_issues:
            update_labels(repository, issue, add=("abandoned",), apply=apply)
            close_as_not_planned(
                api,
                issue,
                "removed",
                apply,
                phase_marker=(
                    sync.PHASE_RETIRED_MARKER
                    if issue["number"] in phase_numbers
                    else None
                ),
            )
    return blocked_entries


def apply_sync(args, root):
    roadmap_path = root / "ROADMAP.md"
    try:
        all_entries = roadmap_issues.parse_roadmap(roadmap_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"{project_context.ROADMAP_MISSING_HINT}: {roadmap_path}")
    except OSError as error:
        sys.exit(f"Cannot read {roadmap_path}: {error}")
    for entry in all_entries.values():
        if entry["status"] not in roadmap_issues.PLAN_STATUSES:
            sys.exit(
                f"Roadmap entry {entry['id']} has invalid plan status {entry['status']!r}; "
                "progress belongs on GitHub, not in ROADMAP.md"
            )
    if args.roadmap_id:
        roadmap_id = roadmap_issues.validate_roadmap_id(args.roadmap_id)
        if roadmap_id not in all_entries:
            sys.exit(f"Roadmap entry {roadmap_id} not found in ROADMAP.md")
        entries = [all_entries[roadmap_id]]
    else:
        entries = list(all_entries.values())
    owner, repo = sync.origin_repo()
    repository = f"{owner}/{repo}"
    api = f"repos/{owner}/{repo}"
    issues = sync.load_issues(api)
    mode = "apply" if args.apply else "dry run"
    print(f"Roadmap issue sync ({mode}; {len(entries)} entr{'y' if len(entries) == 1 else 'ies'})")

    issue_map = {}
    phase_map = {}
    blocked_entries = set()
    for entry in entries:
        epic, planning = roadmap_issues.index_roadmap_issues(issues, entry["id"])
        issue_map[entry["id"]] = (epic, planning)
        blocked = sync_roadmap_entry(root, api, repository, entry, issues, args.apply)
        if blocked:
            blocked_entries.add(entry["id"])

    sync_entries = []
    for entry in entries:
        epic = issue_map[entry["id"]][0]
        if (
            entry["id"] not in blocked_entries
            and entry["status"] in {"planned", "needs-info"}
            and not (epic and epic.get("state") == "closed" and epic.get("state_reason") == "completed")
            and not (epic and "abandoned" in (epic.get("labels") or []))
        ):
            sync_entries.append(entry)
    if args.apply and sync_entries:
        issues = reload_issues(root, api, sync_entries)
        issue_map.update(
            {
                entry["id"]: roadmap_issues.index_roadmap_issues(issues, entry["id"])
                for entry in sync_entries
            }
        )
        for entry in sync_entries:
            blocked = sync_roadmap_entry(root, api, repository, entry, issues, True)
            if blocked:
                blocked_entries.add(entry["id"])
        issues = sync.load_issues(api)
        issue_map.update(
            {
                entry["id"]: roadmap_issues.index_roadmap_issues(issues, entry["id"])
                for entry in sync_entries
            }
        )

    for entry in entries:
        phase_map[entry["id"]] = find_phase1(issues, feature_path_for(root, entry))
        if entry["id"] not in blocked_entries:
            sync_planning_relationships(
                root, api, entry, *issue_map[entry["id"]], phase_map[entry["id"]], args.apply
            )

    if not args.roadmap_id:
        blocked_entries.update(
            close_removed_entries(root, api, repository, all_entries, issues, args.apply)
        )

    all_issue_map = {
        roadmap_id: roadmap_issues.index_roadmap_issues(issues, roadmap_id)
        for roadmap_id in all_entries
    }
    roadmap_ids = set(all_entries)
    for roadmap_id in sorted(marker_roadmap_ids(issues)):
        roadmap_ids.add(roadmap_id)
        all_issue_map[roadmap_id] = roadmap_issues.index_roadmap_issues(issues, roadmap_id)
    source_ids = (
        {entry["id"] for entry in entries} if args.roadmap_id else roadmap_ids
    ) - blocked_entries
    sync_dependencies(
        api,
        source_ids,
        all_entries,
        all_issue_map,
        args.apply,
    )

    if not args.skip_project:
        sync_project(
            root,
            owner,
            repo,
            [entry for entry in entries if entry["id"] not in blocked_entries],
            list(all_entries.values()),
            all_issue_map,
            phase_map,
            args.project_owner,
            args.project_number,
            args.apply,
        )

    if blocked_entries:
        print(
            "Roadmap sync left blocked entries unchanged: "
            + ", ".join(sorted(blocked_entries))
        )
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synchronize roadmap entries to GitHub issues.")
    parser.add_argument("--apply", action="store_true", help="Make changes (default: dry run)")
    parser.add_argument("--id", dest="roadmap_id", help="Synchronize one roadmap entry")
    parser.add_argument("--skip-project", action="store_true", help="Skip GitHub Project changes")
    parser.add_argument("--project-owner", help="default: github_project.owner in project-context.toml")
    parser.add_argument(
        "--project-number", type=int, help="default: github_project.number in project-context.toml"
    )
    args = parser.parse_args(argv)
    try:
        state = project_context.bootstrap_state(sync.ROOT)
    except ValueError as error:
        sys.exit(f"Cannot determine the bootstrap state: {error}")
    if state == "uninitialized":
        print(project_context.NOT_INITIALIZED_MESSAGE)
        return 0
    sync.project_arguments(args)
    return apply_sync(args, sync.ROOT)


if __name__ == "__main__":
    sys.exit(main())
