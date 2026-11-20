#!/usr/bin/env python3
"""Validate issue-backed branch names and maintain PR issue references."""

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Optional, Union


ISSUE_WORK_ID_PATTERN = r"(?:chore|ci|docs|fix|refactor|spec|test)"
ISSUE_BRANCH_PATTERN = re.compile(
    rf"^(?P<issue_number>[1-9]\d*)-(?P<work_id>{ISSUE_WORK_ID_PATTERN})-"
    r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
PHASE_BRANCH_PATTERN = re.compile(
    r"^(?P<issue_number>[1-9]\d*)-"
    r"(?:(?:[a-z0-9]+(?:-+[a-z0-9]+)*)-+)?"
    r"phase-(?P<phase_number>[1-9]\d*)-+"
    r"(?P<slug>[a-z0-9]+(?:-+[a-z0-9]+)*)$"
)
SPEC_BRANCH_PATTERN = re.compile(
    r"^(?P<feature_number>\d{3,})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
EXEMPT_PUSH_BRANCHES = frozenset({"main"})
BRANCH_REF_PREFIX = "refs/heads/"


class ConventionError(ValueError):
    """Raised when branch or issue metadata violates repository conventions."""


@dataclass(frozen=True)
class IssueBranchMetadata:
    branch_type: str
    issue_number: int
    work_id: str
    slug: str
    phase_number: Optional[int] = None


@dataclass(frozen=True)
class SpecBranchMetadata:
    branch_type: str
    feature_number: int
    feature_id: str
    slug: str


BranchMetadata = Union[IssueBranchMetadata, SpecBranchMetadata]


def parse_branch(branch: str) -> BranchMetadata:
    match = ISSUE_BRANCH_PATTERN.fullmatch(branch)
    if match:
        return IssueBranchMetadata(
            branch_type="issue",
            issue_number=int(match.group("issue_number")),
            work_id=match.group("work_id"),
            slug=match.group("slug"),
        )

    match = PHASE_BRANCH_PATTERN.fullmatch(branch)
    if match:
        phase_number = int(match.group("phase_number"))
        return IssueBranchMetadata(
            branch_type="phase",
            issue_number=int(match.group("issue_number")),
            work_id=f"phase-{phase_number}",
            phase_number=phase_number,
            slug=match.group("slug"),
        )

    match = SPEC_BRANCH_PATTERN.fullmatch(branch)
    if match:
        return SpecBranchMetadata(
            branch_type="spec",
            feature_number=int(match.group("feature_number")),
            feature_id=match.group("feature_number"),
            slug=match.group("slug"),
        )

    raise ConventionError(
        "Branch must match a repository-work format "
        "'<github-issue>-<work-id>-<kebab-case-title>', a phase format "
        "containing '-phase-<phase-number>-', or a Spec Kit format "
        "'<three-or-more-digit-feature-number>-<kebab-case-title>'."
    )


def validate_spec_branch_changes(
    metadata: BranchMetadata, changed_paths: Iterable[str]
) -> None:
    """Require a Spec Kit branch to change files under its numbered spec directory."""
    if not isinstance(metadata, SpecBranchMetadata):
        raise ConventionError("Only Spec Kit branches have a spec directory.")

    feature_prefix = f"{metadata.feature_id}-"
    for changed_path in changed_paths:
        path_parts = changed_path.split("/")
        if (
            len(path_parts) >= 3
            and all(part not in {".", ".."} for part in path_parts)
            and path_parts[0] == "specs"
            and path_parts[1].startswith(feature_prefix)
        ):
            return

    raise ConventionError(
        f"Spec Kit feature {metadata.feature_id} must change a file "
        f"under 'specs/{feature_prefix}<slug>/'. If this is an "
        "issue branch, use a supported work ID such as 'chore'."
    )


def parse_pull_request_branch(
    branch: str, changed_paths: Iterable[str]
) -> BranchMetadata:
    """Prefer a matching spec directory when an issue-like branch is ambiguous."""
    spec_match = SPEC_BRANCH_PATTERN.fullmatch(branch)
    if spec_match:
        spec_metadata = SpecBranchMetadata(
            branch_type="spec",
            feature_number=int(spec_match.group("feature_number")),
            feature_id=spec_match.group("feature_number"),
            slug=spec_match.group("slug"),
        )
        try:
            validate_spec_branch_changes(spec_metadata, changed_paths)
        except ConventionError:
            pass
        else:
            return spec_metadata

    return parse_branch(branch)


def check_pushed_branches(lines: Iterable[str]) -> list[str]:
    """Validate the remote branch names in Git pre-push stdin lines.

    Each line is '<local ref> <local sha> <remote ref> <remote sha>'. Deletions,
    tags and the default branch are skipped.
    """
    errors = []
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 4:
            raise ConventionError(f"Unexpected pre-push input line: {line!r}")

        _, local_sha, remote_ref, _ = fields
        if set(local_sha) == {"0"} or not remote_ref.startswith(BRANCH_REF_PREFIX):
            continue

        branch = remote_ref[len(BRANCH_REF_PREFIX) :]
        if branch in EXEMPT_PUSH_BRANCHES:
            continue
        try:
            parse_branch(branch)
        except ConventionError as error:
            errors.append(f"'{branch}': {error}")
    return errors


def validate_issue(metadata: BranchMetadata, title: str, state: str) -> None:
    if isinstance(metadata, SpecBranchMetadata):
        raise ConventionError(
            "Spec Kit branches do not have a linked issue to validate."
        )

    if state.upper() != "OPEN":
        raise ConventionError(
            f"Issue #{metadata.issue_number} must be open, but its state is {state}."
        )

    if metadata.phase_number is not None:
        if not phase_title_matches_number(title, metadata.phase_number):
            raise ConventionError(
                f"Issue #{metadata.issue_number} title must contain "
                f"'Phase {metadata.phase_number}:' to match the branch."
            )
        return

    expected_prefix = metadata.work_id.upper()
    if not re.match(rf"^{re.escape(expected_prefix)}:\s+\S", title, re.IGNORECASE):
        raise ConventionError(
            f"Issue #{metadata.issue_number} title must start with "
            f"'{expected_prefix}: ' to match the branch work ID."
        )


def phase_title_matches_number(title: str, phase_number: int) -> bool:
    """Checks whether a Phase N title matches the branch phase number."""
    return re.search(
        rf"\bPhase\s+{phase_number}\s*:", title, re.IGNORECASE
    ) is not None


def has_closing_reference(
    body: str, issue_number: int, repository: Optional[str] = None
) -> bool:
    keyword = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    repository_prefix = (
        rf"(?:{re.escape(repository)})?" if repository is not None else ""
    )
    return (
        re.search(
            rf"\b{keyword}:?\s+{repository_prefix}#{issue_number}(?![A-Za-z0-9])",
            body,
            re.IGNORECASE,
        )
        is not None
    )


def ensure_closing_reference(
    body: str, issue_number: int, repository: Optional[str] = None
) -> tuple[str, bool]:
    if has_closing_reference(body, issue_number, repository):
        return body, False

    content = body.rstrip()
    separator = "\n\n" if content else ""
    return f"{content}{separator}Closes #{issue_number}\n", True


def has_phase_reference(
    body: str, issue_number: int, repository: Optional[str] = None
) -> bool:
    """True when the body already links the Phase issue, closing or not."""
    if has_closing_reference(body, issue_number, repository):
        return True
    repository_prefix = (
        rf"(?:{re.escape(repository)})?" if repository is not None else ""
    )
    return (
        re.search(
            rf"\b(?:part\s+of|refs?|related\s+to|relates\s+to):?\s+"
            rf"{repository_prefix}#{issue_number}(?![A-Za-z0-9])",
            body,
            re.IGNORECASE,
        )
        is not None
    )


def ensure_phase_reference(
    body: str, issue_number: int, repository: Optional[str] = None
) -> tuple[str, bool]:
    """Link a Phase PR to its Phase issue without closing it.

    A Phase is closed by the Phase sync once all its tasks are checked; a closing
    keyword would close it on the first partial merge. A closing keyword the author
    wrote is left alone: the Phase sync reopens a Phase that still has unchecked tasks.
    """
    if has_phase_reference(body, issue_number, repository):
        return body, False

    content = body.rstrip()
    separator = "\n\n" if content else ""
    return f"{content}{separator}Part of #{issue_number}\n", True


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_parser = subparsers.add_parser("parse")
    parse_parser.add_argument("branch")

    subparsers.add_parser(
        "check-push",
        help="Validate the branches in Git pre-push input read from stdin.",
    )

    validate_parser = subparsers.add_parser("validate-issue")
    validate_parser.add_argument("branch")
    validate_parser.add_argument("--issue-title", required=True)
    validate_parser.add_argument("--issue-state", required=True)

    validate_spec_parser = subparsers.add_parser("validate-spec-branch")
    validate_spec_parser.add_argument("branch")
    validate_spec_parser.add_argument("--changed-paths-file", required=True, type=Path)

    parse_pr_parser = subparsers.add_parser("parse-pr-branch")
    parse_pr_parser.add_argument("branch")
    parse_pr_parser.add_argument("--changed-paths-file", required=True, type=Path)

    update_parser = subparsers.add_parser("update-body")
    update_parser.add_argument("issue_number", type=int)
    update_parser.add_argument("--body-file", required=True, type=Path)
    update_parser.add_argument("--output-file", required=True, type=Path)
    update_parser.add_argument("--repository")
    update_parser.add_argument(
        "--phase",
        action="store_true",
        help="link a Phase PR with 'Part of #N' instead of a closing keyword",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    try:
        if args.command == "parse":
            print(json.dumps(asdict(parse_branch(args.branch))))
            return

        if args.command == "check-push":
            errors = check_pushed_branches(sys.stdin)
            if errors:
                raise ConventionError(
                    "Push rejected, branch name does not follow the conventions "
                    "(see docs/ci-and-conventions.md 'Branch and pull request conventions'):\n"
                    + "\n".join(f"  {error}" for error in errors)
                    + "\nRename the branch ('git branch -m <new-name>') or push it under "
                    "a valid name ('git push origin <local-branch>:<new-name>')."
                )
            return

        if args.command == "validate-issue":
            metadata = parse_branch(args.branch)
            if isinstance(metadata, SpecBranchMetadata):
                raise ConventionError(
                    "Spec Kit branches do not have a linked issue to validate."
                )
            validate_issue(metadata, args.issue_title, args.issue_state)
            return

        if args.command == "validate-spec-branch":
            changed_paths = args.changed_paths_file.read_text(
                encoding="utf-8"
            ).splitlines()
            metadata = parse_pull_request_branch(args.branch, changed_paths)
            validate_spec_branch_changes(metadata, changed_paths)
            return

        if args.command == "parse-pr-branch":
            changed_paths = args.changed_paths_file.read_text(
                encoding="utf-8"
            ).splitlines()
            print(
                json.dumps(
                    asdict(parse_pull_request_branch(args.branch, changed_paths))
                )
            )
            return

        body = args.body_file.read_text(encoding="utf-8")
        ensure = ensure_phase_reference if args.phase else ensure_closing_reference
        updated_body, changed = ensure(body, args.issue_number, args.repository)
        args.output_file.write_text(updated_body, encoding="utf-8")
        print("updated" if changed else "unchanged")
    except (ConventionError, OSError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
