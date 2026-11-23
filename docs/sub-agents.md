# Sub-agents

Reference for the sub-agents in [`agent-definitions/`](../agent-definitions/): who they are, which one takes which task, and the rules that keep them from colliding. The hard rules stay in [`AGENTS.md`](../AGENTS.md); this page does not override them or the Spec Kit skills.

Model and reasoning effort per tool are set in [`agent-definitions/agents.toml`](../agent-definitions/agents.toml), because model names differ per tool; see that file for each agent's profile and values.

## One copy of every skill and sub-agent

The team uses Copilot, Codex and Claude Code, and every skill and sub-agent exists once ([Coding agents](coding-agents.md#coding-agents)):

- **Where things live** (the rules are in `AGENTS.md`): `.claude/skills` is a symlink to the skills. `.github/agents/*.agent.md`, `.claude/agents/*.md` and `.codex/agents/*.toml` are generated locally from the sub-agents and `agents.toml` (by `npm ci`, or `python3 scripts/agent_setup.py sync`; no Git hook regenerates them) and are gitignored. `npm run setup-agents`, `sync --tool <name>` or `git config agent-setup.tools copilot` limits generation to some tools. The three folders are git-ignored as a whole; keep personal agents in your user-level folder (for example `~/.claude/agents/`).
## Roster

| Agent                                                                        | Role                                                                                                                                                                                                                                           |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`tdd-implementer`](../agent-definitions/tdd-implementer.md)                 | Builds the given task IDs red-green-refactor, updates the docs the change invalidates, runs the area's full checks and marks only its own finished tasks `[x]`. Never commits.                                                                 |
| [`code-reviewer`](../agent-definitions/code-reviewer.md)                     | Reviews a diff or PR against the spec and constitution across correctness, readability, architecture, security and performance. Given a PR number or URL, posts inline comments as one GitHub review. Runs lint/typecheck/tests only if asked. |
| [`test-engineer`](../agent-definitions/test-engineer.md)                     | Designs tests, audits coverage against the constitution's quality gate, writes Prove-It reproductions for bugs and checks that guard tests can actually fail.                                                                                  |
| [`security-auditor`](../agent-definitions/security-auditor.md)               | Report-only security pass: authentication and server-side authorization, infrastructure permissions, secrets, input handling, dependencies and supply chain; check the constitution's technology baseline.                                     |
| [`web-performance-auditor`](../agent-definitions/web-performance-auditor.md) | Report-only web performance audit of the project's web front end: Core Web Vitals, loading, rendering and network. Use when performance is the question, not on every change.                                                                  |
| [`Explore`](../agent-definitions/Explore.md)                                 | Read-only search across many files on the low-cost `search` profile; returns locations, and a short explanation when asked, in 400 words or less. Capitalized so it replaces Claude Code's built-in Explore, which inherits the session model. |

The mandatory review gate is in `AGENTS.md` (Agents, and When Creating a Pull Request); the approval count is explained in [Approvals](review-and-approvals.md#approvals). A missing `[review]` section or `required_approvals` key in `project-context.toml` is a validation error. `code-reviewer` also reviews each task's diff after `tdd-implementer` has run; `tdd-implementer` never reviews its own work and skips the `after_implement` `code-review-and-quality` hook, which remains as a safety net for anyone who runs `speckit-implement` directly; the other agents are invoked on demand. Run `security-auditor` for security-sensitive changes (the constitution's security principle) and before the first deployment. `code-reviewer`, `test-engineer`, `security-auditor` and `web-performance-auditor` are adapted from [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) `agents/`; each file names the upstream commit it came from. `tdd-implementer` and `Explore` are local to this repository.

## Which agent takes which task

| The task is...                                                                                               | Give it to                                                                    |
| ------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| An implementation, with or without its own test                                                              | `tdd-implementer`                                                             |
| Test-only: "write a failing test…", an end-to-end or round-trip test, a bug reproduction                     | `test-engineer` (stops at red), then `tdd-implementer` for the implementation |
| A coverage audit, or "can this guard test actually fail?" after a task                                       | `test-engineer`                                                               |
| A review of a diff, a task or a pull request                                                                 | `code-reviewer`                                                               |
| A security pass on auth, permissions, IAM, secrets or infrastructure-as-code changes                         | `security-auditor`                                                            |
| A performance question about the web front end                                                               | `web-performance-auditor`                                                     |
| A broad search: where something is defined or used across many files                                         | `Explore` (history or blame questions: the main session)                      |
| Specifying, clarifying, planning or breaking work into tasks                                                 | The `speckit-*` skills in the main session, no agent                          |
| Running something: the CI checks, the quickstart walkthrough, a deploy to the constitution's dev environment | The main session, no agent                                                    |

## Rules that keep agents from colliding

- **One agent per task ID.** `tdd-implementer` and `test-engineer` both start by writing the failing test, so giving both the same ID produces two sets of tests.
- **One writer per file at a time.** `[P]` in `tasks.md` is meant to say the tasks touch different files. Where several `[P]` tasks target the same file, run them in sequence with one agent; parallel agents would lose each other's edits, as they do on `tasks.md`.
- **`test-engineer` stops at red and reports; `tdd-implementer` takes it to green.** Run them one after the other, never together. Tell `test-engineer` in the prompt to write tests only, not to touch production code. When the tests came first, `tdd-implementer` confirms they fail before implementing and does not edit them.
- **The caller owns `tasks.md` when more than one agent works at once.** Say so in the prompt; the agent then reports finished task IDs instead of marking them. `test-engineer` never marks tasks.
- **Agents do not call each other.** Reviewers and auditors recommend follow-up passes in their report; the caller starts them.
- **Relaying findings and never committing** are hard rules in `AGENTS.md` (Agents): the caller relays every review and audit finding to the user, and sub-agents never commit, push or open pull requests.
