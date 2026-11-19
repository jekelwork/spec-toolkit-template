# Spec-driven development toolkit

A team workflow for building software with coding agents: [Spec Kit](https://github.com/github/spec-kit) skills and hooks, a plan-only roadmap, GitHub issue and Project automation, CI gates, and shared sub-agents for GitHub Copilot, Codex and Claude Code. The project's name, description and GitHub Project board are set in [project-context.toml](project-context.toml), which `python3 scripts/bootstrap.py init` writes ([project-context.example.toml](project-context.example.toml) is the reference). Governance rules are in [.specify/memory/constitution.md](.specify/memory/constitution.md); the roadmap (spec of specs) is in [ROADMAP.md](ROADMAP.md); feature specs, plans and tasks are in [specs/](specs/).

This repository is built on the spec-driven development toolkit from the [jekelwork/spec-toolkit-template](https://github.com/jekelwork/spec-toolkit-template) template. To start another project with it, use **Use this template** on that page or `gh repo create <owner>/<name> --template jekelwork/spec-toolkit-template --public --clone`.

**New here?** To set up a repository created from the template, follow [docs/bootstrap.md](docs/bootstrap.md) and run `python3 scripts/bootstrap.py` at any time: it reports which setup steps are done and what to do next.

## Getting started

Run these steps once per clone, in the repository root, before your first commit.

0. Create your repository from the template, unless you already have one. Do not clone the template repository directly. Follow [docs/bootstrap.md](docs/bootstrap.md), or run `gh repo create <owner>/<name> --template jekelwork/spec-toolkit-template --public --clone`. Bootstrap then sets up your project's governance and GitHub settings.

1. Install the Git hooks (see [Git hooks](#git-hooks)) and generate the sub-agent files:

   ```bash
   npm ci
   ```

2. Choose the coding agent you use, so only its sub-agent files are generated (see [Coding agents](#coding-agents)):

   ```bash
   npm run setup-agents
   ```

   It asks which agents you use, saves the answer in your local Git config and regenerates only those tools' files. Run it again whenever you change coding agents, to generate the files for the new ones. `python3 scripts/bootstrap.py start` asks the same question once when no choice is saved (`start --agents <tool>,…` answers it without asking).

Git does not regenerate the files when you switch branches, because checking out a branch must not run its code. Run `python3 scripts/agent_setup.py sync` after switching branches or pulling changes to `agent-definitions/` (`npm run setup-agents` is the interactive way to change which tools you use; `python3 scripts/bootstrap.py` reports out-of-date agent files). Restart your coding agent session to load new or changed sub-agents.

When you review an untrusted pull request (for example from a fork), do not run its code or install its dependencies. Review it on GitHub, or check it out locally with hooks disabled: `git -c core.hooksPath=/dev/null checkout <branch>`. Lefthook reads `lefthook.yml` and its scripts from the working tree whenever any hook runs, so a branch can change what your hooks execute: use `git -c core.hooksPath=/dev/null` for checkouts **and commits** on untrusted branches.

### Troubleshooting: hooks don't run

If commits succeed without running the hooks, check that no `core.hooksPath` setting overrides Lefthook:

```bash
git config --local --get core.hooksPath   # prints a path if a setting overrides .git/hooks
git config --local --unset core.hooksPath
npx lefthook install
ls .git/hooks/pre-commit                  # should exist
python3 scripts/agent_setup.py sync      # generate the sub-agent files (or run npm ci again)
```

### Windows

The repository contains a symlink (`.claude/skills`; see [Coding agents](#coding-agents)). A clone without symlink support turns it into a small text file: Claude Code then finds no skills, and the `agent-setup` pre-commit hook fails with `.claude/skills: must be a symlink`. Work in WSL, where symlinks work without setup:

1. Clone inside the WSL file system (for example `~/repos`), not under `/mnt/c`.
2. Open the folder with the VS Code WSL extension (`code .` from the WSL shell).

To work on native Windows instead, enable symlinks before cloning:

1. Turn on Developer Mode (Settings → System → For developers), so Git can create symlinks without administrator rights.
2. Run `git config --global core.symlinks true`, then clone.

To fix an existing clone made without symlinks:

```bash
git config core.symlinks true
rm .claude/skills
git checkout -- .claude
```

Check the result with `python3 scripts/agent_setup.py check`.

## Git hooks

Git hooks are managed with [Lefthook](https://lefthook.dev) and configured in [lefthook.yml](lefthook.yml). They are installed by `npm ci` (see [Getting started](#getting-started)), whose `prepare` script runs `lefthook install` and `python3 scripts/agent_setup.py sync`. The `pre-commit` hook runs in parallel:

- **prettier** formats staged `*.yml`/`*.yaml` files, including Spec Kit-generated ones, using the root [.prettierrc.json](.prettierrc.json), and re-stages them.
- **secrets** runs [scripts/check-secrets.mjs](scripts/check-secrets.mjs) on the staged files, including `.env.example` files, and refuses the commit if one looks like a credential. Use obvious placeholders in example files; add `secret-scan:ignore` to a line to mark a deliberate fixture.
- **agent-setup** runs `python3 scripts/agent_setup.py check` (the same check as CI) when agent or Spec Kit files are staged (see [Coding agents](#coding-agents)). It never runs `sync`, because the generated files are git-ignored and the commit does not need them.

There are no `post-checkout`, `post-merge` or `post-rewrite` hooks: checking out a branch must not run its code. Run `python3 scripts/agent_setup.py sync` after switching branches (see [Coding agents](#coding-agents)).

**Existing clones:** older versions of this toolkit installed those three hooks, and `lefthook install` does not delete hooks that were removed from the config. They still run the checked-out branch's `lefthook.yml`. Remove them once: `rm -f .git/hooks/post-checkout .git/hooks/post-merge .git/hooks/post-rewrite` (in the hooks directory of `core.hooksPath` when you set one). `python3 scripts/agent_setup.py check`, which runs in the `agent-setup` pre-commit hook and in CI, fails locally and prints this command while any of them is left; it never fails in CI or a fresh clone, where they do not exist.

The `pre-push` hook runs **branch-name**, [.lefthook/pre-push/branch-name.sh](.lefthook/pre-push/branch-name.sh), which calls `python3 scripts/pr_conventions.py check-push`. It rejects a push whose remote branch name does not follow the [branch conventions](#branch-and-pull-request-conventions), so you can rename the branch before opening a pull request. It does not check the linked issue or matching Spec Kit directory; those checks run in the PR workflow. It skips tags, branch deletions and `main`. Bypass it with `git push --no-verify` or `LEFTHOOK=0` only when you have to; CI still validates the pull request.

For issue branches with unsupported work IDs and numeric prefixes of three or more digits, the
local syntax check may mistake the name for a Spec Kit branch; the PR workflow rejects it unless
the matching spec directory changed.

## Coding agents

Team members use different coding agents. GitHub Copilot, Codex and Claude Code all get the same rules, skills and sub-agents, and each file exists once:

| What                                                     | The one copy                     | Copilot (VS Code, CLI)             | Codex                                         | Claude Code                                  |
| -------------------------------------------------------- | -------------------------------- | ----------------------------------- | --------------------------------------------- | -------------------------------------------- |
| Rules                                                    | [AGENTS.md](AGENTS.md)           | reads it                            | reads it                                      | [CLAUDE.md](CLAUDE.md) imports it            |
| Skills (Spec Kit, roadmap, project, agent-skills)        | `.agents/skills/`                | reads it                            | reads it                                      | `.claude/skills` symlink to it               |
| Sub-agents                                               | `agent-definitions/<name>.md`    | `.github/agents/<name>.agent.md`    | `.codex/agents/<name>.toml` (points to it)    | `.claude/agents/<name>.md`                   |

- **Never copy a skill or sub-agent into a tool's folder.** Add or edit it in the one copy. Sub-agent files in the tool folders are generated locally from `agent-definitions/<name>.md` and `agent-definitions/agents.toml` and are git-ignored, so only the sources are versioned. `npm ci` generates them; run `python3 scripts/agent_setup.py sync` yourself after switching branches and after changing anything in `agent-definitions/`, because no Git hook regenerates them on checkout. Never edit the generated files by hand. `npm run setup-agents` asks which tools you use and saves the answer; `sync` and `npm ci` then generate only those tools and remove the generated files of the others, and their agents folders once empty (`.codex/` too), creating folders only for the selected tools (without a choice, all three are generated). `sync --tool <tool>` (repeatable) overrides the setting for one run. Without a terminal (`setup` needs one), set the choice yourself and then run `python3 scripts/agent_setup.py sync`:

  | You use                       | Run                                    | Sub-agents are generated in |
  | ----------------------------- | -------------------------------------- | --------------------------- |
  | GitHub Copilot (VS Code, CLI) | `git config agent-setup.tools copilot` | `.github/agents/`           |
  | Claude Code                   | `git config agent-setup.tools claude`  | `.claude/agents/`           |
  | Codex                         | `git config agent-setup.tools codex`   | `.codex/agents/`            |

  For several agents, separate them with commas, for example `copilot,claude`.
  The three folders are git-ignored as a whole and hold only generated files. Files you put there yourself are not versioned and Git may delete them on checkout, so keep personal agents in your user-level folder (for example `~/.claude/agents/`). No file in these folders may be versioned (CI fails if Git tracks one; put shared agents in `agent-definitions/`).
- **Model and reasoning effort are set per tool in `agent-definitions/agents.toml`,** not in the agent sources; see [Sub-agent models and reasoning effort](#sub-agent-models-and-reasoning-effort). Every `.md` in `agent-definitions/` is treated as an agent source, so the folder must hold only agent sources (no README).
- **Sub-agent `tools` use Claude Code names** (`Read`, `Edit`, `Write`, `Grep`, `Glob`, `Bash`). Copilot accepts them as aliases; Codex ignores the field.
- **Windows:** clone and work inside WSL (for example with the VS Code WSL extension). A Windows-side checkout turns the `.claude/skills` symlink into a plain file unless symlinks are enabled; see [Windows](#windows).
- Keep tool-specific settings (model, permissions, MCP servers) personal unless the whole team needs them. Process rules are enforced by Git hooks and CI, not by any one agent's instructions.

The **Repository tools** CI job runs `python3 scripts/agent_setup.py check` and fails when a symlink is broken, `.github/skills/` comes back, a sub-agent source or `agent-definitions/agents.toml` is invalid (it does not look at the git-ignored generated files), Git tracks a file in the sub-agent tool folders (`git rm --cached <path>`), or the Spec Kit files below drift. See [scripts/README.md](scripts/README.md#agent_setuppy).

If the check reports `.github/skills: must not exist`, remove that obsolete directory and rerun the check. It may contain only local metadata such as `.DS_Store`; skills belong in `.agents/skills/`. `python3 scripts/agent_setup.py sync` only generates the sub-agent files (Copilot, Claude Code and Codex) and does not remove this directory. Use the Spec Kit restore or upgrade command only when the check reports missing or modified generated Spec Kit files.

### Sub-agent models and reasoning effort

[agent-definitions/agents.toml](agent-definitions/agents.toml) is the source of truth for which model and reasoning effort each sub-agent uses in each tool. The agent sources (`agent-definitions/<name>.md`) must not set `model`, `effort` or `reasoning-effort`; `check` rejects them. The file has two parts:

- `[agents]` gives every agent a profile. Every source in `agent-definitions/` needs exactly one entry, and every entry needs a source.
- `[profiles.<profile>.<tool>]` sets `model` and `effort` for one tool: `claude`, `copilot` or `codex`. Model ids differ per tool, so each tool gets its own table. A missing key (or tool table) inherits the setting of the session that starts the sub-agent.

```toml
[agents]
code-reviewer = "critical-review"
tdd-implementer = "implementation"

[profiles.critical-review.claude]
model = "opus"              # Claude Code alias or full model id
effort = "medium"           # low, medium, high, xhigh or max
[profiles.critical-review.copilot]
model = "claude-opus-5.5"   # Copilot model id, not a Claude Code alias
effort = "medium"
[profiles.critical-review.codex]
model = "gpt-5.6-sol"
effort = "medium"
```

To change a model or effort:

1. Edit the profile's table for the tool. To change only some agents, add a new profile and point those agents to it in `[agents]`; every profile must be used by at least one agent.
2. Run `python3 scripts/agent_setup.py sync` to regenerate your tool's files, then `python3 scripts/agent_setup.py check`.
3. Restart your coding agent session so it loads the new settings.
4. Commit `agents.toml`. Teammates get the change by running `python3 scripts/agent_setup.py sync` (or `npm ci`); no Git hook does it for them.

In the generated files, the values appear as `model` and `effort` (Claude Code), `model` and `reasoning-effort` (Copilot), and `model` and `model_reasoning_effort` (Codex). Never edit them there: the next `sync` overwrites them.

## Spec Kit CLI and version

Day-to-day Spec Kit work (`speckit-specify` through `speckit-implement`) runs as skills in your agent and needs no CLI. The `specify` CLI is only for maintaining the installation: changing integrations or extensions, and upgrading Spec Kit.

The repository is pinned to the Spec Kit version in [.specify/integration.json](.specify/integration.json). Always run the CLI through the wrapper, which uses that version (it needs [uv](https://docs.astral.sh/uv/)):

```bash
scripts/specify.sh integration status
```

The Spec Kit skills are generated by the **Codex** integration into `.agents/skills/`, the folder every supported agent reads, so they mention commands as `$speckit-plan`; in Copilot and Claude Code type `/speckit-plan`. Do not install another integration: it would generate a second copy, and CI fails unless Codex is the only installed and default integration in `.specify/integration.json`.

Spec Kit 1.3.0 removed the `speckit-taskstoissues` skill from core in favour of the `github` extension; the pinned 1.4.0 does not generate it either, nor does it install the extension. Never run it (should it reappear) or the extension's `speckit-github-taskstoissues`: it creates one GitHub issue per `T###` task, while this toolkit creates issues only for Phases.

Generated Spec Kit files are never edited by hand. CI fails when the versions recorded in `.specify/` disagree, a file listed in `.specify/integrations/*.manifest.json` differs from its recorded hash, or a script under `.specify/` is not executable. To upgrade Spec Kit, do it in its own pull request:

```bash
SPECKIT_VERSION=<new> scripts/specify.sh integration upgrade codex
SPECKIT_VERSION=<new> scripts/specify.sh extension update
python3 scripts/agent_setup.py check
```

`extension update` installs each extension's latest release regardless of `SPECKIT_VERSION`; review the extension version change in `.specify/extensions/.registry` in the pull request.

## Agent personas and skills from agent-skills

Review and engineering personas come from [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills). `npx skills` installs skills only, so the two parts are managed differently:

| What                                                                                       | Where                                                                                                                                   | How to update                                                                                                                                 |
| ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Skills (`code-review-and-quality`, `performance-optimization`)                             | `.agents/skills/`; `npx skills` pins them in a `skills-lock.json` (added when you install or update a skill)     | `npx skills add addyosmani/agent-skills --skill <name>`                                                                                       |
| Personas (`code-reviewer`, `security-auditor`, `test-engineer`, `web-performance-auditor`) | `agent-definitions/<name>.md`                                                                                                           | Copy from upstream `agents/<name>.md`, reapply the local changes listed in its `Source:` note, then run `python3 scripts/agent_setup.py sync` |
| Shared checklist used by `performance-optimization` and the personas                       | `.agents/references/performance-checklist.md`; `performance-optimization` and the personas link it as `../../references/...`            | Copy from upstream `references/`                                                                                                              |

Both skills come from `addyosmani/agent-skills` (`skills/<name>/SKILL.md`). `performance-optimization` is unmodified. `code-review-and-quality` has one local change: its two "See Also" links point to the skill's own `references/` copies (`security-checklist.md`, `performance-checklist.md`) instead of `../../references/`. No upstream commit is recorded, only the content hashes `npx skills` compares when it adds or updates a skill: `code-review-and-quality` `0667c7af98996c0bec4fb0eb444680bdbf1da1d6a3cfdf5dbe8cbd350d9d2752` (recorded before the local change), `performance-optimization` `a2cdba94ff8f898183b3d5af736272dc439826083723b17a88782e75d2351530`. `npx skills add` or an update overwrites the local change, so reapply it afterwards.

`browser-testing-with-devtools` (Chrome DevTools MCP) is not included. A project with a web front end can install it with `npx skills add addyosmani/agent-skills --skill browser-testing-with-devtools`.

When `npx skills add` asks where to install, choose only the `.agents/skills` (universal) location, never a per-agent copy or link such as `.claude/skills/<name>`: `.claude/skills` is already a symlink to `.agents/skills`, so a per-agent link there would replace the skill with a link to itself.

Each persona has a `Source:` comment with the upstream commit and the local changes, mainly removed Claude Code slash-command and `docs/agents.md` references. `code-reviewer` also contains the PR mode (inline review comments via `gh`, `Code-review-agent:` prefix) that used to be the separate PR Review agent. Personas do not call each other; the user decides which one to run.

## Spec Kit workflow best practices

The step order is specify → clarify → plan → tasks → analyze → implement. It is enforced by the hooks in [`.specify/extensions.yml`](.specify/extensions.yml) and the rules in [`AGENTS.md`](AGENTS.md); `.specify/workflows/` is not used. Start a roadmap item with one persistent epic, one spec-plan planning issue and one provisional Phase 1 story. Use an issue-backed `spec` branch for the **spec-and-plan pull request**; `tasks.md` is generated afterwards on the Phase 1 implementation branch:

| PR               | Contents                                                                            | Gate                                                                                 |
| ---------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| 1. Spec and plan | `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md` | Team reviews scope and architecture. Merge to the default branch.                    |

Tasks are generated only after the spec-and-plan PR is merged, on the Phase 1 implementation branch. There is no separate tasks PR or tasks-planning issue: `tasks.md` is reviewed with the Phase 1 implementation PR and can be maintained in later reviewed implementation PRs. Do not add the `combined-spec-pr-approved` label yourself; the repository's hard rule is that spec and plan merge before task generation.

Steps:

1. **Use the synchronized planning issues.** The **Sync Roadmap to GitHub Issues** workflow creates or reconciles a planned entry's Epic, spec-plan planning issue and provisional `Phase 1: Kickoff — <Epic name>` story (added to the Project as Planned) after roadmap changes merge to `main`. During rollout, review the workflow's dry-run output before approving the first apply run. Start the spec-and-plan branch from `SPEC: <ID> Create and approve spec and plan`; provisional Phase 1 is blocked by it. The blocker clears after the spec-and-plan PR merges only when the roadmap workflow is apply-enabled (during dry-run rollout, run the approved apply sync). See the [roadmap item walkthrough](scripts/README.md#roadmap-item-walkthrough).
2. **Specify** the feature and its user-facing requirements. The mandatory `before_specify` hook `roadmap-entry.check` requires a planned [ROADMAP.md](ROADMAP.md) entry and an explicit `SPECIFY_FEATURE_DIRECTORY=specs/<roadmap-id>-<slug>` with a matching prefix. It warns about unfinished prerequisites; the blocking dependency check runs before implementation. Never guess the next ID.
3. **Clarify** the generated spec before planning. The mandatory `after_specify` hook runs it (unless the user explicitly skips it for an exploratory spike). Answer questions that could change scope, behavior, acceptance criteria, or design. If important ambiguity is discovered later, clarify the spec and revisit the affected plan decisions.
4. **Review the spec** before planning. Resolve concerns and obtain user approval of the spec before continuing.
5. **Plan** the approved requirements. Keep the roadmap plan-only; GitHub records planning and implementation progress.
6. **Review and merge the spec and plan PR** (PR 1). Run `code-review-and-quality` on the exact final diff before opening the PR and obtain the independent non-author approvals the constitution requires (`[review] required_approvals`) before merging. The roadmap sync refreshes the epic's artifact links after the merge; no per-entry initializer is run.
7. **Generate tasks** from the merged plan on the Phase 1 implementation branch, linked to the provisional Phase 1 issue. The mandatory `before_tasks` hook (`plan-merged.check`) refuses to run unless `spec.md` and `plan.md` are on `origin/main`.
8. **Analyze** after `tasks.md` is complete. Review the read-only report for consistency, coverage, and constitution issues across the spec, plan, and tasks; resolve CRITICAL findings before proceeding. The mandatory `after_tasks` hook runs this step after the initial generation. Rerun it after structural `tasks.md` edits (Phases, tasks, dependencies), not after checkbox-only or wording changes. Once CRITICAL findings are resolved, commit `tasks.md` and push the Phase 1 branch with the user's approval, so others can see the task list.
9. **Review `tasks.md` with the Phase 1 implementation PR**, with the same final-diff review and independent approval gates (keep the constitution's approval count and `[review] required_approvals` equal; if they differ, fix the setting to match the constitution and rerun `bootstrap.py github --apply`). When it merges, the [phase sync workflow](#creating-phase-issues-from-tasks) updates Phase 1 from `tasks.md` and creates the other Phase issues under the epic. Direct `tasks.md` maintenance is allowed in reviewed implementation PRs.
10. **Obtain user approval to implement.** Verify that the spec-and-plan PR is merged. Do not wait for a Phase sync or a separate tasks PR: Phase 1's `tasks.md` is generated on its implementation branch, and GitHub Phase issues synchronize after that PR merges.
11. **Implement from `tasks.md`** on branches linked to the corresponding Phase issues. Update task checkboxes as work changes; each Phase issue tracks a whole Phase, not individual `T###` tasks. Follow the task and phase guidance in [`AGENTS.md`](AGENTS.md). Before implementation, run the mandatory dependency guard; then run `code-review-and-quality` after implementation. Do not write progress statuses back to the roadmap.

See the [end-to-end walkthrough](scripts/README.md#roadmap-item-walkthrough) for commands and failed-sync recovery.

### What to read

A feature directory (`specs/<id>-<slug>/`) holds several kinds of file, and each role needs only some of them. The example paths are from `specs/001-guided-bootstrap/`. The reading times are rough estimates.

| Role                   | Read                                                                                                                                                                                                                                                                                                                                 | Skip unless needed                                                                                                                                                                       | Rough time             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| Stakeholder            | `spec.md`: User Scenarios & Testing (the user stories and their priorities) and Success Criteria                                                                                                                                                                                                                                     | Everything else                                                                                                                                                                          | About 15 minutes       |
| Spec-and-plan reviewer | `spec.md`: Clarifications, User Scenarios & Testing, Requirements; `plan.md`: Summary, Technical Context, Phase split (when present), revision notes (when present), Complexity Tracking and Constitution Check                                                                                                                      | `research.md`, `data-model.md`, `contracts/`, `quickstart.md`, and `plan.md`'s Project Structure (apart from Phase split and revision notes), unless a reviewed decision depends on them | About 30 to 45 minutes |
| Phase implementer      | The Phase's section in `tasks.md`; `plan.md`: Phase split (when present), Technical Context and Project Structure; `spec.md`: the user stories and requirements the Phase's tasks reference; `contracts/` and `data-model.md` for the tasks that touch them; the `research.md` decisions (D1, D2, ...) that the plan or a task cites | The rest of the spec and plan                                                                                                                                                            | Depends on the Phase   |

`tasks.md` exists only after the spec-and-plan PR merges, when the Phase 1 implementation branch generates it. The spec-and-plan reviewer reads the spec and plan in that PR; the implementer reads them again only for the parts their tasks touch.

### Creating Phase issues from tasks

The **Sync Spec Tasks to GitHub Issues** workflow ([sync-spec-tasks.yml](.github/workflows/sync-spec-tasks.yml)) runs automatically when a merged PR changes `specs/<feature>/tasks.md`; no manual sync is needed for the normal path.

- It runs `scripts/sync-task-issues.py --roadmap-only --apply` for each changed feature. Each Phase in `tasks.md` becomes a `Story` issue under the epic, titled and described from `tasks.md`; the provisional Phase 1 created by roadmap sync is updated when `tasks.md` first appears. All Phase issues are added to the configured GitHub Project as Planned, including cards currently in Backlog. **No issues are created for `T###` tasks**: task IDs stay stable in `tasks.md` and inline "(depends on T###)" text only orders implementation.
- Phase numbers are immutable identities and retired numbers cannot be reused. A removed Phase that is still Planned is closed as not planned and does not block the epic. If a removed Phase is In progress, In review or Done, sync stops with an actionable message instead of closing or reusing it.
- A retired Phase's card leaves the Project board (the issue stays closed as not planned).
- A Phase closes when all its tasks are checked, and later unchecked or new work reopens it. If a roadmap entry is deferred or needs information, its Phase issues are paused as not planned and reopened if the entry returns to planned; abandoned or retired Phases are not reopened. The epic closes only when the spec-plan issue is closed as completed and every currently defined Phase is complete; it reopens automatically if incomplete Phase work returns. Completion is recorded on GitHub, not as a roadmap progress status.
- Phase status follows its work: a phase branch means In progress, an open PR from the phase branch (or one that closes the Phase) means In review, a completed issue means Done, a paused Phase (deferred or needs-info) means Backlog, and a retired Phase (any other Phase closed as not planned, including by hand) leaves the board. Reopened issues leave Done or Backlog and return to the status indicated by their branch/PR state, or Planned.
- The workflow uses the workflow-scoped `GITHUB_TOKEN` with `contents: read`, `issues: write` and `pull-requests: read`. Optional Project updates use a board credential (see the next bullets) and are skipped if none is configured. The `Epic`, `Story` and `Planning` labels are used.
- Roadmap and Phase sync ignore marker issues opened by untrusted authors. An issue is trusted when its author is the owner, a member or a collaborator, when the author is a bot (any login ending in `[bot]`, so every App installed on the repository is trusted), or when it carries an `Epic`, `Story` or `Planning` label (setting a label needs triage rights).
- Roadmap sync uses the workflow's `GITHUB_TOKEN` for issues and pull requests. Without a board credential, the workflow reports that board sync was skipped. The `Epic`, `Story`, `Planning`, `needs-info` and `abandoned` labels must exist.
- Board sync uses a credential; none is required: without one, board sync is skipped with a notice and issues still sync. Use the GitHub App secrets (`PROJECT_STATUS_APP_ID`, `PROJECT_BOARD_APP_PRIVATE_KEY`) for an organization board (recommended), or your own classic PAT with the `project` scope and `repo` (`public_repo` for a public repository; `read:org` is not needed), stored with `gh secret set PROJECT_BOARD_TOKEN`: required for a board owned by a personal account and the simple path for any board. The board stays owned by the repository owner. Anyone with push access can read repository secrets, so they could act as you with a personal token; teams that rely on required reviews should use the App or a dedicated machine account instead. The board App wins over `PROJECT_BOARD_TOKEN` if both are set. See [Board credentials](docs/bootstrap.md#board-credentials).
- Repository reads use the workflow's `GITHUB_TOKEN`, so the App needs only Issues: read plus organization Projects: write. See [sync-project-status.py](scripts/README.md#sync-project-statuspy) for view setup and behavior.
- Configure `github_project.owner` and `github_project.number` in [project-context.toml](project-context.toml) for the target board. The roadmap, Phase sync and status workflows and agents share these settings; repositories created from a template must configure their own values. The board credentials require the Project owner to match the repository owner (case-insensitive); cross-owner Projects are not supported. Local `--project-owner` and `--project-number` options override the corresponding TOML settings.
- Phase status `In review` means an open PR exists for the Phase (from its phase branch, or closing it), including draft PRs; Phase PRs use `Part of #<phase issue>` and the Phase sync, not the merge, closes the Phase once every task is checked. Keep the built-in "Pull request linked to issue" workflow enabled with Status = `In review` so Planning issues retain automatic review-status updates; see the [board setup guidance](scripts/README.md#sync-project-statuspy).
- A daily scheduled reconciliation previews missed Phase syncs for roadmap-managed features with `tasks.md`. It applies only after the repository variable `PHASE_SYNC_AUTO_APPLY` is explicitly set to `true`; manual runs also default to preview and require checking **Apply changes**. For recovery or backfills, run the workflow manually from the Actions tab on `main`, optionally giving a roadmap-managed `feature` such as `specs/008-example-feature`; with no feature it processes every roadmap-managed feature. Then confirm synchronization with `--check`.
- Issue changes made with `GITHUB_TOKEN` do not trigger the separate Project-status workflow. After a Phase closes or reopens, its board status may remain stale until the next daily status reconciliation (05:17 UTC).

For initialization, manual sync commands, setup and recovery details, see [scripts/README.md](scripts/README.md).

### Roadmap

[ROADMAP.md](ROADMAP.md) holds the plan: scope, order, dependencies and plan statuses (`undecided`, `needs-info`, `planned`, `deferred`, `abandoned`). Its format is documented in [docs/roadmap-format.md](docs/roadmap-format.md) and validated by `python3 scripts/check_roadmap.py`. A repository fresh from the template has no `ROADMAP.md` yet; until bootstrap creates it (see `docs/bootstrap.md`) the roadmap checks print "Not initialized yet" and pass, and after that a missing roadmap fails. GitHub holds progress. Run `python3 scripts/roadmap_status.py` for a live table or add `--json` for machine-readable rows. `--id NNN --json` evaluates prerequisites (exit 0 ready, 1 blocked, 2 error). Historical progress notes are snapshots, not dependency evidence.

| Skill                      | When                                                                                                              | Behavior                                                                                             |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `/roadmap-write`           | After a constitution discussion or when the user requests a roadmap edit                                         | The only skill that edits `ROADMAP.md`; preserves history and uses plan statuses only                |
| `/roadmap-entry-check`     | Before specifying a feature                                                                                       | Requires a planned entry and matching explicit `SPECIFY_FEATURE_DIRECTORY`; blockers are warnings   |
| `/roadmap-dependency-check`| Before implementation and direct roadmap work                                                                    | Reads live GitHub Epic completion; blocks unfinished prerequisites unless the user acknowledges them |

Progress changes belong on GitHub, not in roadmap status-update PRs. Spec directory prefixes match their roadmap entry IDs; the spec's `Input` line names its entry. For example, entry 008 uses `SPECIFY_FEATURE_DIRECTORY=specs/008-example-feature`.

### Hooks and enforcement

The hooks are defined in [`.specify/extensions.yml`](.specify/extensions.yml). Agents executing the `speckit-*` skills read this file and run the hooks for their step. This is agent-executed skill guidance, not CI or automatic interception of direct CLI commands, and hooks do not fire when `specs/` files are edited by hand.

| Event                               | Hook (in order)                                                                                   | Mandatory                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------- |
| `after_constitution`                | `roadmap.write`                                                                                  | No                                |
| `before_specify`                    | `roadmap-entry.check`                                                                            | Yes                               |
| `after_specify`                     | `speckit.clarify`                                                                                 | Yes (skippable for a spike)       |
| `before_tasks`                      | `plan-merged.check`                                                                              | Yes                               |
| `after_tasks`                       | `speckit.analyze`                                                                                | Yes                               |
| `before_implement`                  | `roadmap-dependency.check`                                                                        | Yes                               |
| `after_implement`                   | `code-review-and-quality`                                                                         | Yes                               |

The main session owns the three roadmap hooks; `tdd-implementer` skips them and lists them as deferred. The entry check warns about blockers before specifying; the dependency guard blocks before implementation unless the user explicitly acknowledges the exact blocker set. When tasks are implemented through `tdd-implementer` without `/speckit-implement`, the main session runs the `before_implement` and `after_implement` hooks itself. Agents end every Spec Kit step with a `Hooks run: <hook>: <ran|skipped|failed>, ...` line so a skipped or failed hook is visible, and stop when a hook fails.

How this is enforced:

- **Agents:** the hooks above, and the hard rules in [`AGENTS.md`](AGENTS.md).
- **CI:** the [Spec PR phases workflow](.github/workflows/spec-pr-phases.yml) fails a pull request that adds a new `tasks.md` while also changing that feature's `spec.md` or `plan.md`, or whose feature does not yet have `spec.md` and `plan.md` on the base branch, unless it carries the `combined-spec-pr-approved` label. Create the label once (`gh label create combined-spec-pr-approved`) and require the **Spec and plan merge before tasks** check in the `main` branch ruleset (see [Repository rulesets](#repository-rulesets)) so it cannot be bypassed.
- **Roadmap CI gate:** [`.github/workflows/roadmap-gate.yml`](.github/workflows/roadmap-gate.yml) checks Phase implementation pull requests using trusted base-branch code and live prerequisite status. The `prerequisites-acknowledged` label is human-only and waives only reported known blockers; it cannot bypass invalid data or ambiguous identity. The gate rejects the label when it cannot verify that a human user applied it (a bot account, a GitHub App, or an undeterminable actor). The pull request's label events are authoritative; the triggering `labeled` payload from a user is used only while those events do not yet show the labeling. If the gate says the actor cannot be verified, re-run the job. Agents acting through a user's token must still never apply it. After this workflow is merged, ask a repository admin to create the label and add the **Roadmap prerequisites** job to the `main` ruleset's required checks. Neither setting is changed by repository automation.

Clarification is a standard pre-planning check, but it should focus on material unknowns rather than implementation details or preferences that do not affect the outcome. For an explicitly exploratory spike, the user may decide to skip it, with the understanding that unresolved assumptions increase downstream rework risk.

Run `/speckit-converge` after implementing the current task set and before merging the final implementation PR. It checks the code against the spec, plan, and tasks; if it appends remaining work, review the new tasks and include them in a reviewed implementation PR (rerun `speckit-analyze` if Phases or dependencies changed). When that PR merges, the phase sync updates or creates the affected Phase issues. Implement the new tasks and run converge again; if no gaps remain, it leaves `tasks.md` unchanged.

### Roadmap dependency guard

Before starting or resuming a roadmap feature, spec, phase, or task, the
[`roadmap-dependency-check`](.agents/skills/roadmap-dependency-check/SKILL.md)
skill checks every prerequisite before branch creation, issue status changes,
or implementation agents. The mandatory Spec Kit hooks are additional checks;
direct issue and task requests use the same guard. Ordinary dependencies are
satisfied only by a unique roadmap Epic closed as completed on GitHub;
missing or open Epics and those closed as not planned remain unsatisfied. The
guard reads `scripts/roadmap_status.py --id NNN --json`, not plan statuses or board Status.
A narrower milestone requires
verified evidence. Invalid, stale, or ambiguous dependency data stops work and
cannot be overridden.
If the skill is unavailable after updating the repository, reload or restart
the agent session and confirm it is available before starting work; do not
skip the guard.

## Branch and pull request conventions

Phase implementation and repository-maintenance pull requests must be backed by an open issue.
Create the branch from the issue's **Create branch** action or with
`gh issue develop` so GitHub records the development link.

- Spec Kit phase: `<github-issue>-phase-<phase-number>-<kebab-case-title>`, for
  example `81-phase-2-foundational-blocking-prerequisites`. GitHub's suggested
  full-title branch name is also accepted; it may place `phase-<phase-number>`
  within the slug and use repeated hyphens.
- Other repository work:
  `<github-issue>-<type>-<kebab-case-title>`, where `type` is `chore`, `ci`,
  `docs`, `fix`, `refactor`, `spec`, or `test`. The issue title must start with
  the matching type and a colon, regardless of case (for example `CHORE:`,
  `chore:`, or `Chore:`).
- Spec Kit design pull requests: the spec-plan planning issue created by the
  roadmap sync workflow (title `SPEC: <roadmap-id> Create and approve spec and plan`)
  backs the spec-and-plan branch, for example
  `412-spec-008-create-and-approve-spec-and-plan`. The provisional Phase 1 issue
  backs task generation and the Phase 1 implementation branch; there is no
  tasks-planning issue.

Branches generated by Spec Kit itself keep the `<feature-number>-<kebab-case-title>`
format, such as `008-example-feature`. They have no backing GitHub issue, so the
workflow validates their branch format but does not require an issue or add a
closing reference. The validator accepts
feature numbers with three or more digits, including zero-padded IDs such as
`005` and unpadded IDs such as `100`. A pull request using this format must
change a file under the matching `specs/<feature-number>-<name>/` directory so
the workflow can distinguish a Spec Kit branch from a similarly shaped issue
branch. Roadmap IDs and GitHub issue numbers are separate namespaces; if they
collide and a PR changes `specs/<same-ID>-*/`, the workflow treats it as a Spec
Kit branch. This is branch-routing validation, not an authorization boundary.

The [PR conventions workflow](.github/workflows/pr-conventions.yml) validates
the branch, confirms that its issue is open and matches the phase or
repository-work type, and adds `Closes #<github-issue>` to the PR body when missing (`Part of #<phase issue>` for a Phase branch, which must not close the Phase on a partial merge). It also
requires Spec Kit branches to change a file under the matching spec directory.
The `branch-name` [pre-push hook](#git-hooks) checks branch syntax locally; the
issue and spec-directory checks run in the PR workflow.

To make this check mandatory, configure the `main` branch ruleset in GitHub to
require the **Validate branch and link issue** status check after this workflow
has run on its first pull request. Repository instructions guide supported AI
agents, while this required check enforces the convention for every contributor.
See [Repository rulesets](#repository-rulesets) for the full configuration.

## GitHub workflows

All workflows are in [.github/workflows/](.github/workflows/); each workflow runs on the events listed below. Every `uses:` reference is pinned to a full commit SHA with the release as a trailing comment (`uses: owner/repo@<40-hex-sha> # vX.Y.Z`); `scripts/test_workflow_action_pins.py` fails otherwise. To update an action, resolve the new tag with `gh api repos/<owner>/<repo>/git/ref/tags/<tag>` (for an annotated tag, dereference it with `gh api repos/<owner>/<repo>/git/tags/<sha>`), then replace the SHA and the comment together.

| Workflow (job name)                                                                          | Runs on                                                  | What it checks or does                                                                                                                                                                                                       |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Toolkit](.github/workflows/toolkit.yml): **Repository tools**                               | Every pull request                                       | Runs the Node.js secret-scanner tests in `scripts/`, the Python unit tests, a check that the template repository itself stays uninitialized (only in a repository marked as a GitHub template; skipped in repositories created from it), `check_roadmap.py` (skipped with a notice while the repository is not initialized yet), then `scripts/agent_setup.py check` (see [Coding agents](#coding-agents)).                                                 |
| [PR conventions](.github/workflows/pr-conventions.yml): **Validate branch and link issue**   | Pull request opened, edited, reopened or pushed to       | Validates the branch name. For non-`spec` branches it confirms the issue is open and matches the phase or repository-work type, and adds `Closes #<issue>` to the PR body when missing (`Part of #<issue>` for a Phase branch).                   |
| [Spec PR phases](.github/workflows/spec-pr-phases.yml): **Spec and plan merge before tasks** | Every pull request                                       | Fails when a PR adds a new `tasks.md` together with `spec.md` or `plan.md`, or when the feature's `spec.md` and `plan.md` are not yet on the base branch. The `combined-spec-pr-approved` label skips it.                    |
| [Roadmap dependency gate](.github/workflows/roadmap-gate.yml): **Roadmap prerequisites**    | Phase pull request opened, reopened, synchronized, labeled or unlabeled | Checks the parent Epic and live roadmap prerequisites using trusted base code. The human-only `prerequisites-acknowledged` label waives known blockers, never data or identity errors; labels whose application by a human user cannot be verified (bot, app-performed or undeterminable actor) are rejected. Label events are authoritative; the triggering user payload counts only while they do not yet show the labeling (re-run the job if the actor cannot be verified). |
| [Sync Roadmap to GitHub Issues](.github/workflows/sync-roadmap-issues.yml)                   | Roadmap/spec artifact changes on `main`; daily; manual   | Reconciles roadmap epics, planning issues, dependency links and optional project ordering. New workflows start in dry-run mode; manual apply requires approval during rollout.                                                   |
| [Sync Spec Tasks to GitHub Issues](.github/workflows/sync-spec-tasks.yml)                    | Default-branch PR merge; daily/manual preview; manual or opt-in scheduled apply | Creates, updates, reopens and closes the Phase (`Story`) issues from `tasks.md` under the roadmap Epic and adds them to the Project as Planned; no `T###` task issues. Uses the least-privilege workflow token; optional Project updates need a board credential (Board App secrets or `PROJECT_BOARD_TOKEN`) and are skipped with a notice without one. See [Creating Phase issues from tasks](#creating-phase-issues-from-tasks). |
| [Sync project status](.github/workflows/sync-project-status.yml)                             | Phase branch/PR and issue events; daily; manual           | Reconciles Phase statuses on the configured Project. Requires the shared project context and a board credential (App secrets or `PROJECT_BOARD_TOKEN`); skips with a notice without one. Ignores fork PRs from untrusted authors. Documented in [Creating Phase issues from tasks](#creating-phase-issues-from-tasks).                                                     |

Which of these block merging is configured in the ruleset; see [Repository rulesets](#repository-rulesets). Application build, test and deployment workflows are project-specific: add them in their own workflow files, separate from `toolkit.yml`.

## Repository rulesets

The default branch (`main`) is protected by one GitHub ruleset, **Default branch protection** (Settings → Rules → Rulesets). It must be **Active** and target the default branch.

| Rule                                  | Status                                | Purpose                                                                    |
| ------------------------------------- | ------------------------------------- | -------------------------------------------------------------------------- |
| Restrict deletions                    | On                                    | `main` cannot be deleted.                                                  |
| Block force pushes                    | On                                    | History on `main` cannot be rewritten.                                     |
| Require a pull request before merging | On, with `[review] required_approvals` approvals | No direct pushes to `main`; non-authors approve (none are required when it is 0). With 2 or more approvals, stale approvals are dismissed on push. |
| Automatically request Copilot review | On here; not created by `bootstrap.py github` | Copilot reviews every pull request.                                        |
| Require status checks to pass         | On, with the checks listed below      | A pull request cannot merge while a listed check fails or is missing.      |

Required status checks (job names as shown on a pull request):

- **Validate branch and link issue** ([pr-conventions.yml](.github/workflows/pr-conventions.yml))
- **Spec and plan merge before tasks** ([spec-pr-phases.yml](.github/workflows/spec-pr-phases.yml))
- **Repository tools** ([toolkit.yml](.github/workflows/toolkit.yml))
- **Roadmap prerequisites** ([roadmap-gate.yml](.github/workflows/roadmap-gate.yml))

When the project adds application CI jobs, add the ones the constitution's quality gates require to this list.

Notes:

- A check only appears in the ruleset's **Add checks** search after it has run on a pull request, and its workflow must exist on the default branch to trigger for every pull request.
- A required check that never reports, for example because the workflow was renamed or skipped, blocks the pull request as "waiting for status". Update the ruleset when you rename a job.
- `bootstrap.py github --apply` never lowers the approval count or last-push approval of an existing ruleset unless you pass `--allow-weaken`; it prints a `Warning:` instead, and `--check` exits 1.
- Adding a rule or check is a settings change, not a code change. Record it in the pull request that introduces the workflow, and keep this section in step with the ruleset.
- `combined-spec-pr-approved` is a label, not a ruleset setting. It lets a pull request pass **Spec and plan merge before tasks** when the team has agreed to combine spec, plan and tasks.
