#!/usr/bin/env python3
"""Read roadmap progress and ordinary dependency completion from GitHub without mutations."""

import json
from pathlib import Path
import subprocess
import sys

import check_roadmap_dependencies as dependencies
import project_board
import project_context
import roadmap_issues

sync = project_board.sync
ROOT = Path(__file__).resolve().parent.parent


def labelled_epics(issues):
    """Trust marker identity only with the maintainer-controlled exact Epic label."""
    epics = []
    for issue in issues:
        labels = issue.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise dependencies.InvalidData(
                f"Roadmap status: invalid label metadata for #{issue.get('number')}"
            )
        if "Epic" in labels:
            epics.append(issue)
        elif "<!-- speckit-roadmap:" in (issue.get("body") or ""):
            print(
                f"Warning: ignoring marker-bearing issue #{issue.get('number')} "
                "without the exact Epic label.",
                file=sys.stderr,
            )
    return epics


def resolve_epics(entries, issues):
    """Map each roadmap number to its single Epic-labelled, marker-owned issue (or None).

    Raises InvalidData on an invalid completion state and exits on duplicate Epics.
    """
    # This reader (and the roadmap gate) is read-only, so an unlabelled copy of an Epic
    # marker is ignored with a warning. The sync scripts fail closed instead because they
    # create issues; see roadmap_issues.index_roadmap_issues.
    issues = labelled_epics(issues)
    epics = {}
    for number in entries:
        marker = roadmap_issues.roadmap_marker(number)
        issue = roadmap_issues.only_match(
            [issue for issue in issues if marker in (issue.get("body") or "")],
            f"roadmap epic {number}", roadmap_issues.duplicate_hint(marker),
        )
        if issue:
            state, reason = issue.get("state"), issue.get("state_reason")
            if state not in {"open", "closed"} or (
                state == "closed" and reason not in {"completed", "not_planned"}
            ):
                raise dependencies.InvalidData(
                    f"Roadmap status: invalid completion state for #{issue['number']}: "
                    f"{state!r}, {reason!r}"
                )
        epics[number] = issue
    return epics


def evaluate_status(entries, number, epics):
    """Evaluate dependencies using closed-as-completed epics, never plan or board status."""
    def assess(prerequisite):
        issue = epics.get(prerequisite)
        state = (
            f"#{issue['number']} {issue['state']}"
            + (f" {issue['state_reason']}" if issue["state"] == "closed" else "")
            if issue else "epic missing"
        )
        return {
            "satisfied": bool(
                issue and issue["state"] == "closed" and issue["state_reason"] == "completed"
            ),
            "condition": f"ordinary completion: epic must be closed as completed ({state})",
        }

    return dependencies.evaluate(entries, number, assess=assess)


def status_rows(entries, epics, owner, repo, board_statuses=None):
    """Return file-ordered progress rows; board status is optional display data only."""
    rows = []
    for number, entry in entries.items():
        issue = epics.get(number)
        evaluation = evaluate_status(entries, number, epics)
        rows.append({
            "id": number,
            "title": entry["title"],
            "plan_status": entry["status"],
            "epic": issue["number"] if issue else None,
            "epic_state": (
                issue["state"] + (
                    f" {issue['state_reason']}" if issue["state"] == "closed" else ""
                ) if issue else "missing"
            ),
            "board_status": (board_statuses or {}).get(number),
            "url": f"https://github.com/{owner}/{repo}/issues/{issue['number']}" if issue else None,
            "open_blockers": [item["id"] for item in evaluation["dependencies"] if not item["satisfied"]],
        })
    return rows


def load_board_statuses(epics, owner, repo):
    """Read optional Project status; warn explicitly when board access is unavailable."""
    statuses = {}
    try:
        context = sync.project_context.load_context(sync.project_context.DEFAULT_CONFIG)
    except (OSError, ValueError) as error:
        print(f"Warning: roadmap board configuration unavailable: {error}", file=sys.stderr)
        return statuses
    project = context["github_project"]
    for number, issue in epics.items():
        if not issue:
            continue
        try:
            item = project_board.project_item_details(
                owner, repo, issue["number"], project["owner"], project["number"]
            )
        except (SystemExit, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            print(f"Warning: roadmap board status unavailable: {error}", file=sys.stderr)
            break
        statuses[number] = item[1] if item else None
    return statuses


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = dependencies.Parser(description=__doc__)
    parser.add_argument("--id", help="evaluate prerequisites for one canonical three-digit ID")
    parser.add_argument("--json", action="store_true", help="output dependency object or progress rows")
    parser.add_argument("--roadmap", type=Path, default=None,
                        help="alternate roadmap file for synthetic tests only")
    parser.add_argument("--skip-project", action="store_true", help="omit optional board status")
    entries, args = {}, None
    try:
        args = parser.parse_args(argv)
        # The bootstrap state belongs to the repository that holds the roadmap file.
        root = ROOT if args.roadmap is None else args.roadmap.parent
        args.roadmap = args.roadmap or ROOT / "ROADMAP.md"
        if (
            args.id is None
            and not args.roadmap.exists()
            and project_context.bootstrap_state(root) == "uninitialized"
        ):
            # Nothing to report yet. A lookup for one ID stays an error below: it cannot exist.
            if args.json:
                print("[]")
                print(project_context.NOT_INITIALIZED_MESSAGE, file=sys.stderr)
            else:
                print(project_context.NOT_INITIALIZED_MESSAGE)
            return 0
        if args.id is not None and not dependencies.ID.fullmatch(args.id):
            raise dependencies.InvalidData("Roadmap status: expected a canonical three-digit roadmap ID")
        if not args.roadmap.exists():
            hint = (
                project_context.NOT_INITIALIZED_MESSAGE
                if project_context.bootstrap_state(root) == "uninitialized"
                else project_context.ROADMAP_MISSING_HINT
            )
            raise dependencies.InvalidData(f"Roadmap status: {hint}")
        dependencies.parse_roadmap(
            args.roadmap.read_text(encoding="utf-8"), entries, statuses=roadmap_issues.PLAN_STATUSES
        )
        if args.id and args.id not in entries:
            raise dependencies.InvalidData(f"Roadmap status: entry {args.id} not found")
        owner, repo = sync.origin_repo()
        issues = sync.load_issues(f"repos/{owner}/{repo}")
        relevant = (
            {number: entries[number] for number in [args.id, *entries[args.id]["depends"]]}
            if args.id else entries
        )
        epics = resolve_epics(relevant, issues)
        if args.id:
            data = evaluate_status(entries, args.id, epics)
            if args.json:
                print(json.dumps(data, ensure_ascii=False))
            else:
                dependencies.print_human(data)
            return 0 if data["result"] == "ready" else 1
        statuses = {} if args.skip_project else load_board_statuses(epics, owner, repo)
        rows = status_rows(entries, epics, owner, repo, statuses)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            print("ID\tTitle\tPlan status\tEpic\tEpic state\tBoard Status\tURL\tOpen blockers")
            for row in rows:
                print("\t".join(str(value) if value is not None else "-" for value in (
                    row["id"], row["title"], row["plan_status"], row["epic"], row["epic_state"],
                    row["board_status"], row["url"], ", ".join(row["open_blockers"]) or "none",
                )))
        return 0
    except (dependencies.InvalidData, OSError, UnicodeError, subprocess.SubprocessError,
            ValueError, KeyError, TypeError, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        item = entries.get(args.id) if args and args.id else None
        data = {
            "result": "error",
            "entry": (
                {key: item[key] for key in ("id", "title", "status", "notes")} if item else
                {"id": args.id, "title": None, "status": None, "notes": None}
                if args and args.id else None
            ),
            "dependencies": [], "baseline": dependencies.BASELINE, "diagnostics": [str(error)],
        }
        if "--json" in argv:
            print(json.dumps(data, ensure_ascii=False))
        else:
            dependencies.print_human(data)
        return 2


if __name__ == "__main__":
    sys.exit(main())
