# Coding agents and Spec Kit

How Copilot, Codex and Claude Code share one set of skills and sub-agents, and how Spec Kit is pinned.

## Coding agents

Team members use different coding agents. GitHub Copilot, Codex and Claude Code all get the same rules, skills and sub-agents, and each file exists once:

| What                                                     | The one copy                     | Copilot (VS Code, CLI)             | Codex                                         | Claude Code                                  |
| -------------------------------------------------------- | -------------------------------- | ----------------------------------- | --------------------------------------------- | -------------------------------------------- |
| Rules                                                    | [AGENTS.md](../AGENTS.md)           | reads it                            | reads it                                      | [CLAUDE.md](../CLAUDE.md) imports it            |
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
- **Windows:** clone and work inside WSL (for example with the VS Code WSL extension). A Windows-side checkout turns the `.claude/skills` symlink into a plain file unless symlinks are enabled; see [Windows](local-setup.md#windows).
- Keep tool-specific settings (model, permissions, MCP servers) personal unless the whole team needs them. Process rules are enforced by Git hooks and CI, not by any one agent's instructions.

The **Repository tools** CI job runs `python3 scripts/agent_setup.py check` and fails when a symlink is broken, `.github/skills/` comes back, a sub-agent source or `agent-definitions/agents.toml` is invalid (it does not look at the git-ignored generated files), Git tracks a file in the sub-agent tool folders (`git rm --cached <path>`), or the Spec Kit files below drift. See [scripts/README.md](../scripts/README.md#agent_setuppy).

If the check reports `.github/skills: must not exist`, remove that obsolete directory and rerun the check. It may contain only local metadata such as `.DS_Store`; skills belong in `.agents/skills/`. `python3 scripts/agent_setup.py sync` only generates the sub-agent files (Copilot, Claude Code and Codex) and does not remove this directory. Use the Spec Kit restore or upgrade command only when the check reports missing or modified generated Spec Kit files.

### Sub-agent models and reasoning effort

[agent-definitions/agents.toml](../agent-definitions/agents.toml) is the source of truth for which model and reasoning effort each sub-agent uses in each tool. The agent sources (`agent-definitions/<name>.md`) must not set `model`, `effort` or `reasoning-effort`; `check` rejects them. The file has two parts:

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

The repository is pinned to the Spec Kit version in [.specify/integration.json](../.specify/integration.json). Always run the CLI through the wrapper, which uses that version (it needs [uv](https://docs.astral.sh/uv/)):

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
