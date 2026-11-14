---
name: plan-merged-check
description: Verify that a Spec Kit feature's spec.md and plan.md are merged to origin/main before tasks are generated. Use for the plan-merged.check before_tasks hook or when asked whether tasks may be generated yet.
---

# Check the plan is merged before generating tasks

Tasks are generated only after the feature's spec and plan pull request is reviewed and merged (README "Spec Kit workflow best practices"). There is no separate tasks pull request: `tasks.md` is generated on the Phase 1 implementation branch and included in the implementation pull request.

## Steps

1. Resolve the feature directory (`specs/<feature>`) from the invoking command or, if none, from the current branch and `.specify/feature.json`. Do not guess from directory ordering.
2. From the repository root run:

   ```bash
   python3 scripts/check_spec_pr_phases.py plan-merged --feature specs/<feature>
   ```

3. If it exits 0, report that tasks may be generated and continue. Before generating tasks, apply the constitution's quality gate: it overrides the `speckit-tasks` default that tests are optional; when the constitution or the spec requires automated tests, `tasks.md` needs a test task for every behavior change, written before its implementation task.
4. If it exits non-zero, **stop**. Do not run `/speckit-tasks`. Tell the user the spec and plan PR must be merged first, then they must pull `main` and generate tasks on the Phase 1 implementation branch. Never work around the check by editing the files or the script.
