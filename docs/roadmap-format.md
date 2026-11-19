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
the graph must be acyclic. A `Spec dir`, when present, is exactly
`specs/<entry-ID>-<slug>` (one directory, an optional trailing `/`, optionally in
backticks) and must name a directory that already contains `spec.md`, so add it only
after the spec exists.
Put extra context in `Notes`; fields not listed above are rejected.

A field may continue on the lines after it, indented or not. The roadmap sync appends those lines to
the field, joined with single spaces. Blank lines are skipped, and only a new `- **Field:**` line or a
heading ends the field. The Epic body shows Description, Outcome, Scope (in), Scope (out), Depends on,
Governed by and Notes (every field except Spec dir).
Fenced code blocks, including their content, are dropped from the Epic body. Nested bullets and line
breaks inside a field are flattened into one line there, so keep roadmap fields to plain prose.

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
file. It can draft a first version on request, and the roadmap can be amended at
any time. Preserve historical entries and notes; do not reclassify progress or
rewrite the ledger as a side effect of another workflow.

## Hooks and dependency checks

The main session owns exactly three roadmap hooks in `.specify/extensions.yml`:

- `roadmap-entry.check` (mandatory, before specifying): the entry must be `planned` and `SPECIFY_FEATURE_DIRECTORY=specs/<ID>-<slug>` must name its directory. It warns about dependency blockers but does not acknowledge them.
- `roadmap-dependency.check` (mandatory, before implementing): rechecks live GitHub completion and blocks unless the user explicitly acknowledges the exact blocker set.
- `roadmap.write` (optional, after a constitution change).

Run the applicable hooks once per feature or phase, never per `tdd-implementer` slice. The main session runs the dependency guard before delegating roadmap work and gives `tdd-implementer` the entry, the requested scope, any verified milestone evidence, the blockers, and any explicit acknowledgement (or confirms there are no blockers). The agent stops if this result is missing; it does not invent or broaden an override.

`ROADMAP.md` uses plan statuses only: `undecided`, `needs-info`, `planned`, `deferred`, `abandoned`. Read progress with `python3 scripts/roadmap_status.py` or on the board; use `python3 scripts/roadmap_status.py --id <ID> --json` for dependency checks. Only an Epic closed as completed satisfies ordinary completion; missing Epics, open Epics and closure as not planned do not. Historical notes and board Status are not completion evidence.

The `prerequisites-acknowledged` label (human-only, see `AGENTS.md`) applies only to the known blockers reported for that Phase PR and never overrides invalid data or ambiguous identity.
