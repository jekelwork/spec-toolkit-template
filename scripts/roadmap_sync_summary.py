#!/usr/bin/env python3
"""Turn the dry-run output of sync-roadmap-issues.py into a short plain-text summary.

Used by the pull request preview workflow. The workflow runs this script from the base branch, never
from the pull request, but the dry-run lines still contain text taken from the pull request's
ROADMAP.md and specs (issue titles). The workflow therefore shows the result only inside a code
fence it writes itself, and the comment job adds the hidden marker, neutralises mentions and caps the
length again. Replacing `<` and triple backticks here is defense in depth, not the only barrier.
"""

import argparse
from pathlib import Path
import re
import sys

MAX_LENGTH = 60000  # GitHub rejects comments of 65536 characters or more
DRY_RUN = "[dry-run] "
EPIC_STATE = re.compile(r"^Roadmap Epic exists: (yes|no)$", re.MULTILINE)
KINDS = ("created", "updated", "closed", "reopened", "labels", "board", "links and other")
BOARD_LINE = re.compile(r"^(?:(?:add|remove) .* (?:to|from) project |set .* Roadmap order to )")
CODE_CHANGED = (
    "This pull request changes files other than ROADMAP.md and specs/ (the sync scripts, "
    "workflows or project settings may be among them). This preview used the base branch's code, "
    "so the code that runs after the merge may behave differently."
)


def kind_of(line):
    text = line[len(DRY_RUN) :]
    if BOARD_LINE.match(text):
        return "board"
    for kind, prefixes in (
        ("created", ("create ",)),
        ("updated", ("update ",)),
        ("closed", ("close ",)),
        ("reopened", ("reopen ",)),
        ("labels", ("add label ", "remove label ")),
    ):
        if text.startswith(prefixes):
            return kind
    return "links and other"


def safe(text):
    return text.replace("<", "‹").replace("```", "'''")


def apply_sentence(output, auto_apply):
    # Not stripped: the workflow's expressions cannot trim, so a padded value is invalid there too.
    value = (auto_apply or "").lower()
    if value == "false":
        return (
            "Automatic apply is off (ROADMAP_SYNC_AUTO_APPLY=false): merging does not apply these "
            "changes; run the sync manually with Apply changes selected."
        )
    if value not in ("", "true"):
        return (
            "ROADMAP_SYNC_AUTO_APPLY has an invalid value; the roadmap sync fails on pushes until "
            "it is `true` or `false`."
        )
    state = EPIC_STATE.search(output)
    if state is None:
        return "Whether merging applies these changes could not be determined."
    if state.group(1) == "no":
        return (
            "No roadmap Epic exists yet: the first roadmap sync is a manual, reviewed step, so "
            "merging does not apply these changes."
        )
    return "Merging to the default branch applies these changes automatically."


def summarize(output, exit_code, board_skipped, code_changed=False, auto_apply=""):
    lines = [line.rstrip() for line in output.splitlines() if line.startswith(DRY_RUN)]
    parts = []
    if code_changed:
        parts += [CODE_CHANGED, ""]
    parts += ["Roadmap sync preview: a dry run of the roadmap sync on this pull request's files.", ""]
    if exit_code:
        parts += [f"The dry run failed (exit code {exit_code}); see the job log.", ""]
    if lines:
        counts = {}
        for line in lines:
            counts[kind_of(line)] = counts.get(kind_of(line), 0) + 1
        parts.append(
            f"Planned changes: {len(lines)} ("
            + ", ".join(f"{kind}: {counts[kind]}" for kind in KINDS if kind in counts)
            + ")"
        )
    elif not exit_code:
        parts.append("No issue changes are planned.")
    parts.append(apply_sentence(output, auto_apply))
    if board_skipped:
        parts.append(
            "Board changes were not previewed (pull request runs do not use the board credential)."
        )
    if lines:
        shown = []
        used = 0
        truncated = False
        for line in lines:
            line = safe(line)
            if used + len(line) + 1 > MAX_LENGTH - 2000:
                truncated = True
                break
            shown.append(line)
            used += len(line) + 1
        parts += [""] + shown
        if truncated:
            parts.append("(truncated; see the job log for the full output)")
    return "\n".join(parts) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output_file", help="file with the dry-run output")
    parser.add_argument("--exit-code", type=int, default=0, help="exit code of the dry run")
    parser.add_argument("--board-skipped", action="store_true", help="board changes were not previewed")
    parser.add_argument("--code-changed", action="store_true", help="the pull request changes files other than ROADMAP.md and specs/")
    parser.add_argument("--auto-apply", default="", help="value of the ROADMAP_SYNC_AUTO_APPLY variable")
    args = parser.parse_args(argv)
    try:
        output = Path(args.output_file).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        sys.exit(f"Cannot read {args.output_file}: {error}")
    sys.stdout.write(
        summarize(output, args.exit_code, args.board_skipped, args.code_changed, args.auto_apply)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
