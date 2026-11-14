#!/usr/bin/env python3
"""Validate project-context.toml and print its non-secret settings as JSON."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "project-context.toml"
BOOTSTRAP_GUIDE = "docs/bootstrap.md"
NOT_INITIALIZED_MESSAGE = f"Not initialized yet: no ROADMAP.md. See {BOOTSTRAP_GUIDE}."
ROADMAP_MISSING_HINT = (
    f"ROADMAP.md is missing; the `roadmap-write` skill creates it (see {BOOTSTRAP_GUIDE})"
)
SKIP_NOTICE_PREFIX = "::notice::Board sync skipped"
NO_CREDENTIAL_NOTICE = (
    f"{SKIP_NOTICE_PREFIX}: configure the GitHub App secrets PROJECT_STATUS_APP_ID and "
    "PROJECT_BOARD_APP_PRIVATE_KEY (organization boards) or the PROJECT_BOARD_TOKEN secret "
    "(personal-account boards) to enable it."
)
SCHEMA = {
    "project": {"name", "description"},
    "github_project": {"owner", "number"},
}
OPTIONAL_SCHEMA = {
    "toolkit": {"export_source"},
    "review": {"required_approvals"},
}
DEFAULT_REQUIRED_APPROVALS = 1
OWNER_PATTERN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


BOOTSTRAP_MARKERS = (
    "project-context.toml",
    "ROADMAP.md",
    ".specify/memory/constitution.md",
)


def bootstrap_state(root=ROOT):
    """Return "uninitialized" or "initialized"; the one definition CI and the scripts share.

    A repository is uninitialized only while none of these exist: project-context.toml,
    ROADMAP.md, .specify/memory/constitution.md, and a feature directory under specs/. Any one
    of them makes it initialized, so deleting a file later can never switch the checks off.
    Before answering "uninitialized" it requires root to be a repository root (it holds
    scripts/project_context.py) and raises ValueError otherwise, so a mistyped --root is an
    error instead of an empty directory that reads as "uninitialized".
    """
    root = Path(root)
    specs = root / "specs"
    has_features = specs.is_dir() and any(path.is_dir() for path in specs.iterdir())
    if has_features or any((root / marker).exists() for marker in BOOTSTRAP_MARKERS):
        return "initialized"
    if not (root / "scripts" / "project_context.py").is_file():
        raise ValueError(
            f"{root} is not a repository root (scripts/project_context.py not found)"
        )
    return "uninitialized"


def missing_context_message(path):
    """The one wording for a missing project-context.toml, shared by every script that reads it."""
    return (
        f"Missing project context {path}: run `python3 scripts/bootstrap.py init` "
        f"(see {BOOTSTRAP_GUIDE})."
    )


def board_owner_or_skip(config, root, repository_owner, environ):
    """Return (owner, stderr lines) for a workflow that syncs the Project board.

    The owner is empty when the board is skipped: no credential is configured, or the repository
    is not initialized yet and has no project-context.toml. Raises ValueError (or OSError and
    tomllib.TOMLDecodeError) when the board is wanted but the file is missing or invalid.
    """
    app = environ.get("HAS_BOARD_APP") == "true"
    token = environ.get("HAS_BOARD_PAT") == "true"
    if not app and not token:
        return "", [NO_CREDENTIAL_NOTICE]
    if not config.exists():
        if bootstrap_state(root) == "uninitialized":
            return "", [
                f"{SKIP_NOTICE_PREFIX}: project-context.toml does not exist yet "
                f"(see {BOOTSTRAP_GUIDE})."
            ]
        raise ValueError("::error::" + missing_context_message(config))
    context = load_context(config)
    try:
        validate_repository_owner(context, repository_owner)
    except ValueError as error:
        raise ValueError("::error::" + str(error)) from error
    credential = (
        "the GitHub App (PROJECT_STATUS_APP_ID and PROJECT_BOARD_APP_PRIVATE_KEY)"
        if app  # the GitHub App wins when both credentials are set
        else "PROJECT_BOARD_TOKEN"
    )
    return context["github_project"]["owner"], [f"::notice::Board sync uses {credential}."]


def load_context(path):
    with path.open("rb") as file:
        context = tomllib.load(file)
    unknown_sections = set(context) - set(SCHEMA) - set(OPTIONAL_SCHEMA)
    missing_sections = set(SCHEMA) - set(context)
    if unknown_sections or missing_sections:
        problems = []
        if unknown_sections:
            problems.append(f"unknown sections: {', '.join(sorted(unknown_sections))}")
        if missing_sections:
            problems.append(f"missing sections: {', '.join(sorted(missing_sections))}")
        raise ValueError(
            f"{'; '.join(problems)} (required sections: {', '.join(SCHEMA)}; "
            f"optional sections: {', '.join(OPTIONAL_SCHEMA)})"
        )
    for section, keys in SCHEMA.items():
        values = context[section]
        if not isinstance(values, dict):
            raise ValueError(f"{section} must be a table")
        missing = keys - values.keys()
        unknown = values.keys() - keys
        if missing:
            raise ValueError(f"missing {section}.{', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"unknown {section} keys: {', '.join(sorted(unknown))}")
    for key in ("name", "description"):
        value = context["project"][key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"project.{key} must be a non-empty string")
    owner = context["github_project"]["owner"]
    if not isinstance(owner, str) or len(owner) > 39 or not OWNER_PATTERN.fullmatch(owner):
        raise ValueError("github_project.owner must be a GitHub owner login")
    number = context["github_project"]["number"]
    if type(number) is not int or number < 1:
        raise ValueError("github_project.number must be a positive integer")
    if "toolkit" in context:
        toolkit = context["toolkit"]
        if not isinstance(toolkit, dict):
            raise ValueError("toolkit must be a table")
        unknown = toolkit.keys() - OPTIONAL_SCHEMA["toolkit"]
        if unknown:
            raise ValueError(f"unknown toolkit keys: {', '.join(sorted(unknown))}")
        if "export_source" in toolkit and type(toolkit["export_source"]) is not bool:
            raise ValueError("toolkit.export_source must be a boolean")
    if "review" in context:
        review = context["review"]
        if not isinstance(review, dict):
            raise ValueError("review must be a table")
        unknown = review.keys() - OPTIONAL_SCHEMA["review"]
        if unknown:
            raise ValueError(f"unknown review keys: {', '.join(sorted(unknown))}")
        approvals = review.get("required_approvals")
        if "required_approvals" in review and (type(approvals) is not int or approvals < 0):
            raise ValueError("review.required_approvals must be a non-negative integer")
    return context


def required_approvals(context):
    """Independent approvals a pull request needs; 0 is a solo project, absent means 1."""
    return context.get("review", {}).get("required_approvals", DEFAULT_REQUIRED_APPROVALS)


def is_toolkit_export_source(context):
    """True only in the toolkit's source repository; absent means false."""
    return context.get("toolkit", {}).get("export_source", False)


def validate_repository_owner(context, repository_owner):
    project_owner = context["github_project"]["owner"]
    if project_owner.casefold() != repository_owner.casefold():
        raise ValueError(
            "github_project.owner must match the repository owner; "
            "cross-owner Projects are not supported (a board owned by another account "
            "cannot be used)"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--repository-owner",
        help="require github_project.owner to match this repository owner",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--bootstrap-state",
        action="store_true",
        help="print uninitialized or initialized (see bootstrap_state)",
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root for --bootstrap-state"
    )
    modes.add_argument(
        "--toolkit-export-source",
        action="store_true",
        help="print true or false for toolkit.export_source (false when the file is missing)",
    )
    modes.add_argument(
        "--required-approvals",
        action="store_true",
        help="print review.required_approvals (1 when the setting is absent)",
    )
    modes.add_argument(
        "--board-owner-or-skip",
        action="store_true",
        help="for the board workflows: print github_project.owner, or a skip notice on stderr "
        "(no credential, or not initialized yet); fails once initialized without the file "
        "(needs --repository-owner; reads HAS_BOARD_APP and HAS_BOARD_PAT)",
    )
    args = parser.parse_args()
    if args.board_owner_or_skip:
        if not args.repository_owner:
            parser.error("--board-owner-or-skip requires --repository-owner")
        try:
            owner, lines = board_owner_or_skip(
                args.config, args.root, args.repository_owner, os.environ
            )
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            message = str(error)
            if not message.startswith("::error::"):
                message = f"::error::Invalid project context TOML {args.config}: {message}"
            print(message, file=sys.stderr)
            return 1
        for line in lines:
            print(line, file=sys.stderr)
        if owner:
            print(owner)
        return 0
    if args.bootstrap_state:
        try:
            print(bootstrap_state(args.root))
        except (OSError, ValueError) as error:
            print(f"Cannot determine the bootstrap state: {error}", file=sys.stderr)
            return 1
        return 0
    if args.toolkit_export_source and not args.config.exists():
        print("false")
        return 0
    if not args.config.exists():
        print(missing_context_message(args.config), file=sys.stderr)
        return 1
    try:
        context = load_context(args.config)
        if args.repository_owner:
            validate_repository_owner(context, args.repository_owner)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"Invalid project context TOML {args.config}: {error}", file=sys.stderr)
        return 1
    if args.toolkit_export_source:
        print("true" if is_toolkit_export_source(context) else "false")
        return 0
    if args.required_approvals:
        print(required_approvals(context))
        return 0
    print(json.dumps(context))
    return 0


if __name__ == "__main__":
    sys.exit(main())
