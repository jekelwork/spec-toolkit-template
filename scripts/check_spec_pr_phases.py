#!/usr/bin/env python3
"""Enforce that a feature's spec and plan are merged before its tasks.md is created.

Subcommands:
  plan-merged --feature specs/<feature> [--ref origin/main]
      Local check used by the before_tasks hook: spec.md and plan.md must exist on the ref.
  pr-files [--allow-label LABEL] [--base-ref REF]
      CI check: reads "<status>\\t<path>" lines (GitHub PR files API) on stdin and fails when a
      PR adds a new specs/<feature>/tasks.md and either also changes that feature's spec.md or
      plan.md, or the feature's spec.md and plan.md are not yet on the base ref.
      The LABELS environment variable (comma separated PR labels) may carry the allow label.
"""

import argparse
import os
import re
import subprocess
import sys

ALLOW_LABEL = "combined-spec-pr-approved"
DESIGN_FILES = ("spec.md", "plan.md")


def classify_changes(changes):
    """Return (features adding a new tasks.md, features with spec.md or plan.md changes)."""
    added_tasks = set()
    design_changes = set()
    for status, path in changes:
        match = re.fullmatch(r"(specs/[^/]+)/(tasks\.md|spec\.md|plan\.md)", path)
        if not match:
            continue
        feature, name = match.groups()
        if name == "tasks.md" and status == "added":
            added_tasks.add(feature)
        elif name in DESIGN_FILES:
            design_changes.add(feature)
    return added_tasks, design_changes


def find_combined_features(changes):
    """Return sorted feature paths whose new tasks.md shares a PR with spec.md or plan.md changes."""
    added_tasks, design_changes = classify_changes(changes)
    return sorted(added_tasks & design_changes)


def find_unmerged_plan_features(changes, base_ref):
    """Return {feature: missing files} for new tasks.md whose spec or plan is not on base_ref."""
    added_tasks, _ = classify_changes(changes)
    unmerged = {}
    for feature in sorted(added_tasks):
        missing = missing_on_ref(feature, base_ref)
        if missing:
            unmerged[feature] = missing
    return unmerged


def missing_on_ref(feature, ref):
    missing = []
    for name in DESIGN_FILES:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{ref}:{feature}/{name}"],
            capture_output=True,
        )
        if result.returncode != 0:
            missing.append(name)
    return missing


def run_plan_merged(args):
    subprocess.run(["git", "fetch", "--quiet", "origin", "main"], check=False)
    feature = args.feature.rstrip("/")
    missing = missing_on_ref(feature, args.ref)
    if not missing:
        print(f"{feature}: spec.md and plan.md are on {args.ref}. Tasks may be generated.")
        return 0
    print(
        f"{feature}: {', '.join(missing)} not found on {args.ref}.\n"
        "Merge the spec and plan pull request first\n"
        "(review per the constitution's Git workflow principle), pull main,\n"
        "then run /speckit-tasks on the Phase 1 implementation branch.\n"
        "Tasks do not go in the spec-and-plan pull request.",
        file=sys.stderr,
    )
    return 1


def run_pr_files(args):
    changes = []
    for line in sys.stdin.read().splitlines():
        status, _, path = line.partition("\t")
        if path:
            changes.append((status, path))
    combined = find_combined_features(changes)
    unmerged = find_unmerged_plan_features(changes, args.base_ref)
    if not combined and not unmerged:
        print("No new tasks.md lacks a merged spec and plan, or shares a pull request with them.")
        return 0
    labels = {label.strip() for label in os.environ.get("LABELS", "").split(",")}
    if args.allow_label in labels:
        features = sorted(set(combined) | set(unmerged))
        print(f"Allowed by label '{args.allow_label}': {', '.join(features)}")
        return 0
    if combined:
        print(
            f"New tasks.md added together with spec.md or plan.md for: {', '.join(combined)}.\n"
            "Merge the spec and plan in their own pull request first, then generate tasks on the\n"
            "Phase 1 implementation branch.",
            file=sys.stderr,
        )
    for feature, missing in unmerged.items():
        if feature in combined:
            continue
        print(
            f"New tasks.md for {feature}, but {', '.join(missing)} not found on {args.base_ref}.\n"
            "Merge the spec and plan pull request first.",
            file=sys.stderr,
        )
    print(f"For an approved exception, add the '{args.allow_label}' label.", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    merged = commands.add_parser("plan-merged")
    merged.add_argument("--feature", required=True)
    merged.add_argument("--ref", default="origin/main")
    merged.set_defaults(run=run_plan_merged)

    files = commands.add_parser("pr-files")
    files.add_argument("--allow-label", default=ALLOW_LABEL)
    files.add_argument("--base-ref", default="origin/main")
    files.set_defaults(run=run_pr_files)

    args = parser.parse_args()
    sys.exit(args.run(args))


if __name__ == "__main__":
    main()
