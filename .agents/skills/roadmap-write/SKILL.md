---
name: roadmap-write
description: Create or amend the root plan-only roadmap from the constitution, project brief (if any), and confirmed decisions; drafts a complete first version when the user asks for one.
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

## The roadmap is a living document

People stall on the roadmap because it feels final. When you create the
roadmap, or when the user seems unsure what to write, tell them in plain words
and before your first question that it is not. For a routine amendment of an
existing roadmap, skip this unless they hesitate.

- A first version is enough; one entry is fine.
- `roadmap-write` can amend the roadmap at any time: add entries, change scope
  or dependencies, pause an entry with `needs-info` or `deferred`, or drop it
  with `abandoned` (an abandoned entry's issues are not reopened
  automatically).
- Once an entry has GitHub issues (the roadmap sync creates them for `planned`
  and `needs-info` entries), change its status instead of deleting or
  renumbering it: deleting such an entry abandons it.
- If they are unsure what to write, they can ask for a first draft and then
  accept or edit it.

## Gather and confirm context

1. Read `.specify/memory/constitution.md`, `project-brief.md` when it exists,
   and any relevant discussion, ADRs, or existing notes. Use those sources to
   prefill proposals. Without a project brief, work from the constitution and
   confirmed user decisions.
2. If no `ROADMAP.md` exists, propose its first entries from the confirmed
   constitution discussion and project brief (if any). Ask focused questions about
   genuine gaps before writing; record unresolved questions only when the user
   agrees they belong in the plan. When the user asks for a draft or hands the
   content over, follow [Drafting on request](#drafting-on-request) instead of
   asking.
3. If the roadmap exists, inspect it first and ask only about the requested
   delta. Preserve existing entries, historical notes, and context. Do not
   rewrite unrelated entries or delete superseded material. When the user asks
   for a draft of the change or hands it over, follow
   [Drafting on request](#drafting-on-request) instead of asking.
4. Present proposed changes for user confirmation before editing. Unless the
   user has asked for a draft or handed the content over, never invent
   decisions, scope, outcomes, or dependencies: if neither the sources nor the
   user define a required field, stop and ask rather than fabricate it.

## Drafting on request

Use this when the user asks for a draft, or hands the content over (for example
"you decide" or "fill it in as you see best"), at any point, including after
you have asked questions. It applies to a new roadmap and to an amendment.

1. Do not ask the gap questions again. Instead, propose a complete first version
   (or the complete change) from the constitution, the project brief (if any)
   and what the user has already answered, with every required field of every
   entry filled in.
2. Mark what you inferred as proposals: for example the split into entries,
   outcomes, scope, dependencies and statuses. List them after the file and say
   for each whether it comes from the constitution, the brief, the user's
   answers, or your own assumption. Also say which entries will get GitHub
   issues once the change is on the default branch: the roadmap sync creates an
   Epic, a spec-and-plan planning issue and a Phase 1 issue for each `planned`
   entry, and an Epic for each `needs-info` entry.
3. Show the whole file: the full proposed `ROADMAP.md`, or, when amending, every
   entry you add or change in full.
4. Write only after an explicit yes on the text you showed. A change request is
   not a yes: revise and show the result again. The user's yes is what makes the
   content their decision.

A draft hand-over covers the content only; the ledger rules below still apply.

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
