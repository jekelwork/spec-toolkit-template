---
name: roadmap-entry-check
description: Validate the planned roadmap entry and explicit feature directory before specifying a feature.
---

# Check the roadmap entry before specifying

Run this mandatory check immediately before `speckit-specify`.

1. Derive an explicit `SPECIFY_FEATURE_DIRECTORY=specs/NNN-<slug>` from the
   roadmap entry the user or issue names, and pass it explicitly to
   `speckit-specify`; do not ask the user to set it. Use the path stated in the
   entry's `SPEC: NNN Create and approve spec and plan` issue when it has one;
   otherwise the entry's `Spec dir`, or the one existing `specs/NNN-*`
   directory. Failing those, `NNN` is the entry ID and `<slug>` is the entry
   title the way the sync builds it: lowercase, every run of characters other
   than `a-z` and `0-9` replaced by one hyphen, leading and trailing hyphens
   removed (so "Don't panic" becomes `don-t-panic`). Do not infer the ID from
   the next free number, issue number, branch name, or a stale feature. If no
   entry is named, or the named one is ambiguous, ask which entry is meant; if
   several `specs/NNN-*` directories exist and no `Spec dir` is set, ask which
   one to use.
2. Run `python3 scripts/check_roadmap.py`. If the ledger is invalid, stop and
   report the validator error.
3. Read the matching entry in root `ROADMAP.md`. The three-digit directory
   prefix must match exactly one entry, and its status must be `planned`.
   Missing, ambiguous, malformed, or non-planned entries are errors; do not
   create or edit the roadmap to make the check pass.
4. Run `python3 scripts/roadmap_status.py --id NNN --json` for that entry.
   Exit 0 means no ordinary dependency blockers. Exit 1 means dependencies
   remain unfinished: report every blocker as a warning and continue with
   specifying. Exit 2 means invalid or unavailable data; stop. This warning
   does not acknowledge blockers or authorize implementation.

When specifying, the new spec's `**Input**:` line must name `ROADMAP.md` entry
`<ID>` (the directory prefix); the Repository tools check rejects a spec whose
Input line does not.

Specification remains subject to the normal user review and approval gates.
