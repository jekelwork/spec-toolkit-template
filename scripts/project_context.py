#!/usr/bin/env python3
"""Validate project-context.toml and print its non-secret settings as JSON."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tomllib

import display_text


def mask_url_credentials(url):
    """`url` with the userinfo of a `scheme://` URL (everything before the last `@` of the
    authority) replaced by `***`, so a token embedded in a remote never reaches a message, title
    or command. scp-style `git@host:path` has no scheme and stays as it is. Only for display:
    never pass the result to a real command. The one implementation, shared by the sync and the
    bootstrap steps."""
    scheme, separator, rest = url.partition("://")
    if not separator or not scheme or not scheme.replace("+", "").replace("-", "").isalnum():
        return url
    authority, slash, path = rest.partition("/")
    userinfo, at, host = authority.rpartition("@")
    return f"{scheme}://***@{host}{slash}{path}" if at else url


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
    "review": {"required_approvals"},
}
MISSING_REVIEW_MESSAGE = (
    "missing review.required_approvals: add `[review] required_approvals = N`, using the "
    "number of independent approvals your constitution's Git workflow principle sets "
    "(0 for a solo project)"
)
OWNER_PATTERN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
# The characters project.name and project.description may not hold (control characters,
# separators and format characters except ZWJ, ZWNJ and soft hyphen) are defined once, in
# display_text.
SINGLE_LINE_MESSAGE = "must be a non-empty single-line string without control characters"


class InvalidValue(ValueError):
    """A value for an `init` flag that failed validation. The message is
    `<flag>: <shown value> <requirement>`; the flag and the requirement are kept apart so a
    caller can print the requirement without the value."""

    def __init__(self, flag, shown, requirement):
        super().__init__(f"{flag}: {shown} {requirement}")
        self.flag = flag
        self.requirement = requirement


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
    return parse_context(path.read_bytes().decode("utf-8"))


def parse_context(text):
    """Validate project-context.toml text; the one validation shared with the bootstrap scripts."""
    context = tomllib.loads(text)
    unknown_sections = set(context) - set(SCHEMA)
    missing_sections = set(SCHEMA) - set(context)
    if missing_sections == {"review"} and not unknown_sections:
        raise ValueError(MISSING_REVIEW_MESSAGE)
    if unknown_sections or missing_sections:
        problems = []
        if unknown_sections:
            problems.append(f"unknown sections: {', '.join(sorted(unknown_sections))}")
        if missing_sections:
            problems.append(f"missing sections: {', '.join(sorted(missing_sections))}")
        raise ValueError(
            f"{'; '.join(problems)} (required sections: {', '.join(SCHEMA)})"
        )
    for section, keys in SCHEMA.items():
        values = context[section]
        if not isinstance(values, dict):
            raise ValueError(f"{section} must be a table")
        missing = keys - values.keys()
        unknown = values.keys() - keys
        if unknown:
            raise ValueError(f"unknown {section} keys: {', '.join(sorted(unknown))}")
        if missing:
            hint = f": {MISSING_REVIEW_MESSAGE.partition(': ')[2]}" if section == "review" else ""
            raise ValueError(f"missing {section}.{', '.join(sorted(missing))}{hint}")
    for key in ("name", "description"):
        value = context["project"][key]
        if (not isinstance(value, str) or not value.strip()
                or any(display_text.is_input_forbidden(char) for char in value)):
            raise ValueError(f"project.{key} {SINGLE_LINE_MESSAGE}")
    owner = context["github_project"]["owner"]
    if not isinstance(owner, str) or len(owner) > 39 or not OWNER_PATTERN.fullmatch(owner):
        raise ValueError("github_project.owner must be a GitHub owner login")
    number = context["github_project"]["number"]
    if type(number) is not int or number < 1:
        raise ValueError("github_project.number must be a positive integer")
    approvals = context["review"]["required_approvals"]
    if type(approvals) is not int or approvals < 0:
        raise ValueError("review.required_approvals must be a non-negative integer")
    return context


def required_approvals(context):
    """Independent approvals a pull request needs; 0 is a solo project. Never implied."""
    return context["review"]["required_approvals"]


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
        "--required-approvals",
        action="store_true",
        help="print review.required_approvals (required in the context)",
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
    if args.required_approvals:
        print(required_approvals(context))
        return 0
    print(json.dumps(context))
    return 0


if __name__ == "__main__":
    sys.exit(main())
