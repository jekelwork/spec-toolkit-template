# Roadmap format

`ROADMAP.md` at the repository root is the plan-only roadmap ledger. GitHub
issues and pull requests hold implementation progress; do not write progress
statuses into the roadmap.

The ledger contains exactly one `## Planned Specs` section. All real entry
headings must occur there; a subsequent level-two (`##`) or level-one (`#`)
heading ends that section. Real entries outside it are rejected, not archived
or silently ignored. Fenced code and HTML comments may contain examples.

Each entry in that section has a unique three-digit ID and this level-three
(`###`) heading shape:

```markdown
### 001 — Example feature  [status: planned]
```

Only these plan statuses are valid: `undecided`, `needs-info`, `planned`,
`deferred`, and `abandoned`. Every entry requires these fields:

```markdown
- **Description:** What the planned feature covers.
- **Outcome:** The intended outcome.
- **Scope (in):** Work included in the entry.
- **Depends on:** none
```

Optional fields are `Scope (out)`, `Governed by`, `Spec dir`, and `Notes`.
Dependencies are `none` or a comma-separated list of existing three-digit IDs;
the graph must be acyclic. A `Spec dir`, when present, starts with
`specs/<entry-ID>-`. Put extra context in `Notes`; fields not listed above are rejected.

The read-only validator checks both the ledger format and the IDs referenced by
spec directories:

```bash
python3 scripts/check_roadmap.py
```

A `needs-info` entry pauses its work without losing it: the sync keeps the Epic open and labels it
`needs-info`, closes the planning issue and every Phase issue as not planned (each Phase gets a pause
marker, so it is not treated as retired), and its Phase cards stay on the board and move to Backlog on
the next status sync (daily), because the roadmap sync closes Phases with `GITHUB_TOKEN`, which does
not trigger the status sync. When the entry returns to `planned`, the sync reopens those same issues
and removes the label. The pause is refused while a Phase is In progress, In review or Done.

Use the sole roadmap-writing skill, `roadmap-write`, to create or amend this
file. Preserve historical entries and notes; do not reclassify progress or
rewrite the ledger as a side effect of another workflow.
