"""Shared pieces for roadmap-tracked GitHub issues.

Used by sync-roadmap-issues.py (synchronizes roadmap entries, the spec-plan planning issue
and the provisional Phase 1 issue) and sync-task-issues.py (reconciles Phase issues from
tasks.md under the epic). T### tasks never become GitHub issues.
Everything here is pure: no GitHub calls.
"""

from pathlib import Path
import re
import sys

import check_roadmap_dependencies

ROADMAP_ID = re.compile(r"^\d{3}$")
EPIC_FIELDS = ("Description", "Outcome", "Scope (in)", "Scope (out)", "Depends on", "Governed by", "Notes")
ARTIFACTS = (("Specification", "spec.md"), ("Plan", "plan.md"), ("Tasks", "tasks.md"))

# Planning issues, in creation order. There is only one: the tasks are generated on the
# Phase 1 implementation branch, so no separate tasks-planning issue exists.
PLANNING = (("spec-plan", "Create and approve spec and plan"),)

# Progress lives on GitHub, not in the roadmap's plan status.
PLAN_STATUSES = ("undecided", "needs-info", "planned", "deferred", "abandoned")


def validate_roadmap_id(value):
    if not ROADMAP_ID.match(value or ""):
        sys.exit(
            f"Roadmap ID must be the canonical three-digit entry ID from ROADMAP.md "
            f"(e.g. 008), got {value!r}"
        )
    return value


def roadmap_marker(roadmap_id):
    return f"<!-- speckit-roadmap: {roadmap_id} -->"


def planning_marker(roadmap_id, key):
    return f"<!-- speckit-planning: {roadmap_id}#{key} -->"


def planning_title(roadmap_id, key):
    return f"SPEC: {roadmap_id} {dict(PLANNING)[key]}"


def slugify(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def feature_path(entry, preferred_path=None):
    return (
        entry.get("spec_dir")
        or preferred_path
        or f"specs/{entry['id']}-{slugify(entry['title'])}"
    )


def develop_command(roadmap_id, key):
    branch = f"<issue-number>-spec-{roadmap_id}-{slugify(dict(PLANNING)[key])}"
    return f"`gh issue develop <issue-number> --base main --name {branch} --checkout`"


def spec_plan_body(entry, feature_path):
    roadmap_id = entry["id"]
    return (
        f"{planning_marker(roadmap_id, 'spec-plan')}\n\n"
        f"**Purpose:** write and get approval for the specification and plan of roadmap entry "
        f"**{roadmap_id} — {entry['title']}**. No implementation happens here; that starts later "
        "on the Phase 1 issue.\n\n"
        "**What to do, in order**, in your coding agent in this repository:\n\n"
        "- [ ] Pull `main`, then create the branch from this issue with "
        f"{develop_command(roadmap_id, 'spec-plan')}, where `<issue-number>` is this issue's number\n"
        f"- [ ] Run `speckit-specify` with `SPECIFY_FEATURE_DIRECTORY={feature_path}` "
        f"(the directory prefix must be `{roadmap_id}`)\n"
        "- [ ] Run `speckit-clarify` and answer its questions\n"
        "- [ ] Ask the user to review and approve the spec before planning\n"
        "- [ ] Run `speckit-plan` (research, data model, contracts, quickstart)\n"
        "- [ ] Open the pull request with the spec and plan only, and run `code-review-and-quality` "
        "on the final diff (through `code-reviewer`)\n"
        "- [ ] Get the independent approvals the constitution requires, mirrored in "
        "`[review] required_approvals` in `project-context.toml` (required, no implicit "
        "default; the constitution wins; 0 is valid only where the constitution allows a solo "
        "project), then merge. Merging closes this issue and unblocks Phase 1.\n\n"
        "**Rules that apply along the way:**\n\n"
        "- Around each Spec Kit command, run every `before_` and `after_` hook that "
        "`.specify/extensions.yml` defines for it (for example `before_specify`; `speckit-clarify` "
        "is the `after_specify` hook)\n"
        "- Keep the roadmap plan-only; GitHub records specification and implementation progress\n"
        "- Do not run `speckit-tasks` on this branch: the initial `tasks.md` is generated on the "
        "Phase 1 implementation branch after this pull request is merged\n"
        "- `AGENTS.md` has the full rules\n"
    )


def phase_marker(feature_path, phase_key):
    return f"<!-- speckit-phase: {feature_path}#{phase_key} -->"


def roadmap_phase_marker(roadmap_id, phase_key):
    return f"<!-- speckit-roadmap-phase: {roadmap_id}#{phase_key} -->"


def provisional_phase_title(entry):
    return f"Phase 1: Kickoff — {entry['title']}"


def provisional_phase_body(entry, feature_path):
    roadmap_id = entry["id"]
    return (
        f"{phase_marker(feature_path, 'phase-1')}\n"
        f"{roadmap_phase_marker(roadmap_id, 'phase-1')}\n\n"
        f"Provisional Phase 1 for roadmap entry **{roadmap_id} — {entry['title']}**. It is blocked "
        "by the spec-and-plan planning issue until that pull request is merged. When automatic "
        "apply is enabled, the blocker is then removed; during dry-run rollout, an approved apply "
        "sync is required.\n\n"
        "Start the Phase 1 implementation branch from this issue. Generate the initial "
        f"`{feature_path}/tasks.md` there with `speckit-tasks`, then run `speckit-analyze` before "
        "coding, and include `tasks.md` in the implementation pull request. When that pull request "
        "is merged, the sync replaces this title and body with Phase 1 from `tasks.md` and creates "
        "the other Phase issues under the epic.\n"
    )


def epic_title(entry):
    return f"Feature: {entry['id']} {entry['title']}"


def roadmap_id_for_path(feature_path):
    """Return the roadmap ID of a specs/<ID>-<slug> directory (CI enforces the prefix)."""
    match = re.match(r"^(\d{3})-", Path(feature_path).name)
    return match.group(1) if match else None


def parse_roadmap(text):
    """Return {roadmap ID: entry} from the validator's `## Planned Specs` section and field rules.

    Exits with the validator's message when the roadmap is not valid enough to read safely.
    """
    try:
        entries = check_roadmap_dependencies.parse_entry_fields(text)
    except check_roadmap_dependencies.InvalidData as error:
        sys.exit(f"Cannot parse ROADMAP.md: {error}")
    for entry in entries.values():
        spec_dir = entry["fields"].get("Spec dir", "").strip("` ").rstrip("/")
        entry["spec_dir"] = spec_dir or None
    return entries


def find_roadmap_entry(root, roadmap_id):
    path = Path(root) / "ROADMAP.md"
    try:
        entries = parse_roadmap(path.read_text(encoding="utf-8"))
    except OSError as error:
        sys.exit(f"Cannot read {path}: {error}")
    return entries.get(roadmap_id)


def load_roadmap_entry(root, roadmap_id):
    entry = find_roadmap_entry(root, roadmap_id)
    if entry is None:
        sys.exit(f"Roadmap entry {roadmap_id} not found in ROADMAP.md")
    return entry


def existing_artifacts(root, feature_path):
    if not feature_path:
        return []
    return [name for _, name in ARTIFACTS if (Path(root) / feature_path / name).is_file()]


def roadmap_epic_body(entry, planning_numbers, feature_path=None, artifacts=()):
    """Render the roadmap epic body; identical inputs always give identical output."""
    markers = [roadmap_marker(entry["id"])]
    fields = [
        f"- **{name}:** {entry['fields'][name]}" for name in EPIC_FIELDS if name in entry["fields"]
    ]
    planning = []
    for key, purpose in PLANNING:
        number = planning_numbers.get(key)
        planning.append(f"- {purpose}: {f'#{number}' if number else '(not created yet)'}")
    links = [
        f"[{label}]({feature_path}/{name})" for label, name in ARTIFACTS if name in artifacts
    ]
    return (
        "\n".join(markers)
        + "\n\n"
        + f"Tracks roadmap entry **{entry['id']} — {entry['title']}** "
        + "from [ROADMAP.md](ROADMAP.md).\n\n"
        + "\n".join(fields)
        + "\n\n## Planning\n\n"
        + "\n".join(planning)
        + "\n\n## Artifacts\n\n"
        + (" | ".join(links) if links else "No specification artifacts yet.")
        + "\n\n## Completion\n\n"
        + "The task sync closes this epic only when the spec-and-plan issue is closed as completed "
        + "and every Phase currently defined in `tasks.md` is complete (all its tasks checked). "
        + "GitHub holds current progress; `ROADMAP.md` holds only the plan. "
        + "Read progress and dependency completion with `scripts/roadmap_status.py` or on the board.\n"
    )


def duplicate_hint(marker):
    return (
        f"Remove the marker {marker} from the duplicate issue, then re-run; closing it is not "
        "enough, because closed issues still match."
    )


TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
MARKER_PREFIXES = ("<!-- speckit-",)
# Setting a label needs triage rights, so an issue carrying one was vetted by a maintainer (or created
# by the sync itself), whoever opened it: a departed author, or a private member who GitHub reports
# as CONTRIBUTOR, must not make the sync create a duplicate Epic.
# Consequence: never add an issue template that applies one of these labels automatically, or any
# outsider could open a trusted marker issue.
MAINTAINER_LABELS = {"Epic", "Story", "Planning"}
# Issue numbers already warned about in this run; the lookups run many times over the same issues.
WARNED_ISSUES = set()


def is_trusted_author(issue):
    """True when a marker in this issue may be believed.

    That is a bot or app (every installed App, any `[bot]` author), a repository insider, or an issue
    carrying a maintainer-only label.

    Anyone can open an issue in a public repository and paste a marker into it. Issues loaded by
    `load_issues` always carry `author` and `author_association`; an issue without either field
    (hand-built data) is not judged.
    """
    if "author" not in issue and "author_association" not in issue:
        return True
    return (
        (issue.get("author") or "").endswith("[bot]")
        or issue.get("author_association") in TRUSTED_ASSOCIATIONS
        or bool(MAINTAINER_LABELS.intersection(issue.get("labels") or []))
    )


def trusted_issues(issues):
    """Drop issues from untrusted authors, with a warning naming each (marker lookups only)."""
    kept = []
    for issue in issues:
        if is_trusted_author(issue):
            kept.append(issue)
        elif issue.get("number") not in WARNED_ISSUES and any(
            marker in (issue.get("body") or "") for marker in MARKER_PREFIXES
        ):
            WARNED_ISSUES.add(issue.get("number"))
            print(
                f"Warning: ignoring #{issue.get('number')} by {issue.get('author') or 'unknown'} "
                f"({issue.get('author_association') or 'no association'}): it carries a sync "
                "marker but its author is not a bot, owner, member or collaborator and it has no "
                "Epic, Story or Planning label",
                file=sys.stderr,
            )
    return kept


def only_match(matches, description, hint=None):
    if len(matches) > 1:
        numbers = ", ".join(f"#{issue['number']}" for issue in matches)
        sys.exit(f"Multiple issues match {description}: {numbers}" + (f". {hint}" if hint else ""))
    return matches[0] if matches else None


def index_roadmap_issues(issues, roadmap_id):
    """Return (epic, {planning key: issue}) found by marker; ambiguous matches exit."""
    # The Epic label is deliberately not required: the sync scripts create issues, so any
    # unexpected copy of a marker from a trusted author (or one carrying a maintainer-only label)
    # must stop them before they add a duplicate Epic. Markers pasted by outsiders are dropped by
    # `trusted_issues`. The read-only status reader and gate ignore unlabelled copies instead; see
    # roadmap_status.resolve_epics.

    issues = trusted_issues(issues)

    def find(marker, description):
        matches = [issue for issue in issues if marker in (issue.get("body") or "")]
        return only_match(matches, description, duplicate_hint(marker))

    epic = find(roadmap_marker(roadmap_id), f"roadmap epic {roadmap_id}")
    planning = {
        key: find(planning_marker(roadmap_id, key), f"planning issue {roadmap_id}#{key}")
        for key, _ in PLANNING
    }
    return epic, planning


def epic_close_blockers(phases_complete, planning_issues):
    """Return the reasons a roadmap epic must stay open; empty when it may close.

    Closed phase issues are not evidence: only tasks.md checkboxes and the planning
    issue's own completed state count.
    """
    reasons = []
    for key, purpose in PLANNING:
        issue = planning_issues.get(key)
        if not issue:
            reasons.append(f"planning issue {key} ({purpose}) is missing")
        elif issue.get("state") != "closed":
            reasons.append(f"planning issue #{issue.get('number')} ({key}) is open")
        elif issue.get("state_reason") != "completed":
            reasons.append(
                f"planning issue #{issue.get('number')} ({key}) was closed as "
                f"{issue.get('state_reason') or 'unknown'}, not completed"
            )
    if not phases_complete:
        reasons.append("not every Phase defined in tasks.md is complete")
    return reasons
