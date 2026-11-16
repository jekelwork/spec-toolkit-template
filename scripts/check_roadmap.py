#!/usr/bin/env python3
"""Validate the owned, plan-only ROADMAP.md ledger and its spec directories."""

import argparse
from pathlib import Path
import re
import sys

import check_roadmap_dependencies as dependencies
import check_roadmap_spec_ids
import project_context
import roadmap_issues


ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FIELDS = ("Description", "Outcome", "Scope (in)", "Depends on")
OPTIONAL_FIELDS = (
    "Scope (out)",
    "Governed by",
    "Spec dir",
    "Notes",
)
ALLOWED_FIELDS = frozenset((*REQUIRED_FIELDS, *OPTIONAL_FIELDS))
FIELD_MARKER = re.compile(r"^[ \t]*[-+*][ \t]+\*\*")


def validate_roadmap_text(text):
    """Validate headings, fields and dependency graph using the shared parser."""
    entries = {}
    dependencies.parse_roadmap(
        text, entries, statuses=roadmap_issues.PLAN_STATUSES
    )
    fields_by_id = {}

    for item in dependencies.iter_entry_lines(text):
        if item.is_heading:
            if item.heading:
                fields_by_id[item.heading.group(1)] = {}
            continue

        field, line_number = item.field, item.number
        if FIELD_MARKER.match(item.text) and field is None:
            raise dependencies.InvalidData(
                f"line {line_number}: malformed roadmap field; use "
                "'- **Field name:** value'"
            )
        if not field:
            continue
        current_id = item.entry_id
        if current_id is None:
            raise dependencies.InvalidData(
                f"line {line_number}: roadmap field outside an entry"
            )

        name, value = field.groups()
        if name not in ALLOWED_FIELDS:
            raise dependencies.InvalidData(
                f"line {line_number}: unknown roadmap field {name!r}"
            )
        if name in fields_by_id[current_id]:
            raise dependencies.InvalidData(
                f"line {line_number}: duplicate {name!r} field in {current_id}"
            )
        if not (value or "").strip():
            raise dependencies.InvalidData(
                f"line {line_number}: empty {name!r} field in {current_id}"
            )
        fields_by_id[current_id][name] = value.strip()

    for number, entry_fields in fields_by_id.items():
        for name in REQUIRED_FIELDS:
            if name not in entry_fields:
                raise dependencies.InvalidData(
                    f"{number}: missing required field {name!r}"
                )
        spec_dir = entry_fields.get("Spec dir")
        if spec_dir:
            normalized = spec_dir.strip("`").rstrip("/")
            if not re.match(rf"^specs/{re.escape(number)}-[^/]+$", normalized):
                raise dependencies.InvalidData(
                    f"{number}: Spec dir prefix must be specs/{number}-<slug>/"
                )

    return entries


def validate_roadmap(root=ROOT):
    """Validate the root roadmap and ensure every numbered spec points to it."""
    roadmap_path = Path(root) / "ROADMAP.md"
    try:
        text = roadmap_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise dependencies.InvalidData(
            f"{project_context.ROADMAP_MISSING_HINT}: {roadmap_path}"
        ) from error
    except (OSError, UnicodeError) as error:
        raise dependencies.InvalidData(
            f"cannot read {roadmap_path}: {error}"
        ) from error

    entries = validate_roadmap_text(text)
    spec_errors = check_roadmap_spec_ids.check(Path(root))
    if spec_errors:
        raise dependencies.InvalidData("\n".join(spec_errors))
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root containing ROADMAP.md and specs/",
    )
    args = parser.parse_args(argv)
    try:
        state = project_context.bootstrap_state(args.root)
    except ValueError as error:
        print(f"Roadmap validation failed: {error}", file=sys.stderr)
        return 1
    if state == "uninitialized":
        print(project_context.NOT_INITIALIZED_MESSAGE)
        return 0
    try:
        entries = validate_roadmap(args.root)
    except (dependencies.InvalidData, OSError, UnicodeError) as error:
        print(f"Roadmap validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Roadmap format is valid ({len(entries)} entries).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
