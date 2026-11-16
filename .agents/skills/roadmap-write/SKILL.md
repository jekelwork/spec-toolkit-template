---
name: roadmap-write
description: Create or amend the root plan-only roadmap from the constitution, project brief (if any), and confirmed decisions.
---

# Write the project roadmap

This skill is the **only skill that edits root `ROADMAP.md`**. Use it after a
constitution discussion to create the roadmap, or when the user asks to add or
amend plan scope, dependencies, decisions, or a plan status.

This skill adapts the `speckit.roadmap.write` command from the
[`speckit-roadmap` extension](https://github.com/srobroek/speckit-roadmap),
version `0.1.1`, by Sjors Robroek, Copyright 2026. The source is licensed under
Apache-2.0; see [LICENSE](LICENSE). This repository's version is modified for
the root plan-only roadmap and its owned format validator.

## Gather and confirm context

1. Read `.specify/memory/constitution.md`, `project-brief.md` when it exists,
   and any relevant discussion, ADRs, or existing notes. Use those sources to
   prefill proposals; never invent decisions, scope, outcomes, or dependencies.
   Without a project brief, work from the constitution and confirmed user
   decisions, and ask the user for any scope or goals neither provides.
2. If no `ROADMAP.md` exists, propose its first entries from the confirmed
   constitution discussion and project brief (if any). Ask focused questions about
   genuine gaps before writing; record unresolved questions only when the user
   agrees they belong in the plan.
3. If the roadmap exists, inspect it first and ask only about the requested
   delta. Preserve existing entries, historical notes, and context. Do not
   rewrite unrelated entries or delete superseded material.
4. Present proposed changes for user confirmation before editing. If the user
   has not supplied enough information to define a required field, stop and
   ask rather than fabricate it.

## Maintain the plan-only ledger

- Write only the format documented in [`docs/roadmap-format.md`](../../../docs/roadmap-format.md).
- Use unique three-digit roadmap IDs and only `undecided`, `needs-info`,
  `planned`, `deferred`, or `abandoned` statuses.
- Keep each entry's description, outcome, scope, dependencies, and notes
  consistent with the confirmed project context. Every dependency must name an
  existing entry and the graph must remain acyclic.
- Keep `Spec dir` prefixes equal to the entry ID, and add a `Spec dir` only after its
  `spec.md` exists. Do not change or renumber existing IDs to work around an
  inconsistency.
- Preserve historical content. Do not turn old progress notes into current
  status, and never write `specced`, `in-progress`, `implemented`, or `verified`
  as plan statuses.
- Do not edit feature specs, plans, tasks, GitHub issues, labels, or project
  settings as part of this skill.

After the user confirms the edit, update only the requested roadmap material
and run `python3 scripts/check_roadmap.py`. If validation fails, repair only the
confirmed edit; do not weaken validation or alter unrelated history. Report
what changed and any unresolved user-approved questions.
