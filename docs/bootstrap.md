# Bootstrap

## What bootstrap is

A repository created from the template starts "not initialized": it has no
`project-context.toml`, constitution, `ROADMAP.md` or specs, and CI passes or skips with a notice.
Bootstrap sets up your project's own governance and the GitHub settings the automation needs. The end
state is a repository where:

- `project-context.toml` names your project, its GitHub Project board and its required approvals;
- your constitution and `ROADMAP.md` exist, created through the `speckit-constitution` and
  `roadmap-write` skills;
- labels, board fields and the `main` ruleset match what the workflows expect;
- you can start your first feature with `speckit-specify`.

`python3 scripts/bootstrap.py` (the same as `bootstrap.py check`) reports where you are and the next
action. It changes nothing, so run it whenever you are unsure.

## Prerequisites

- `gh` installed and logged in: `gh auth login`, then `gh auth refresh -s project` (the board needs the
  `project` scope).
- Python 3.11 or newer.
- Node as pinned in `.nvmrc`, then `npm ci` in the repository root.
- Repository admin rights on GitHub, for the ruleset.
- A public repository, or a GitHub plan that supports rulesets on private repositories (Pro, Team or
  Enterprise). On GitHub Free, rulesets work only on public repositories.

## Quick start

Create your repository from the template and clone it:

```bash
# --public makes your code, specs and roadmap visible to everyone. Rulesets need a public
# repository on GitHub Free; use --private only on Pro, Team or Enterprise.
gh repo create <owner>/my-project --template jekelwork/spec-toolkit-template --public --clone
cd my-project
npm ci                                  # git hooks and agent files
```

Work on an issue-backed branch, then run the steps:

```bash
gh issue create --title "CHORE: bootstrap the project" --body "Set up governance and GitHub settings."
gh issue develop <issue> --base main --name <issue>-chore-bootstrap --checkout
python3 scripts/bootstrap.py init --description "What the project does" --create-board
# in your coding agent: run the speckit-constitution skill, then the roadmap-write skill
# (have each roadmap entry's details ready, see "Roadmap")
python3 scripts/bootstrap.py github --apply
gh secret set PROJECT_BOARD_TOKEN       # optional: enables board sync; or the App secrets, see "Board credentials"
python3 scripts/bootstrap.py            # everything should be ✓ (or – for optional parts)
git add -A && git commit -m "CHORE: bootstrap the project" && git push -u origin HEAD
gh pr create --fill
```

Each step is explained below.

## Create the repository from the template

`gh repo create --template` (or the **Use this template** button on GitHub, followed by `git clone`)
creates your repository with the template's files as a single initial commit on `main`, with a fresh
history. GitHub copies files only: labels, the ruleset, secrets and the board are set up by the
bootstrap steps below. If the clone comes out empty because GitHub was still generating the
repository, run `git fetch origin && git checkout main` in it. `npm ci` runs the `prepare` script, which installs the Git
hooks and runs `python3 scripts/agent_setup.py sync` to generate the sub-agent files for Copilot,
Claude Code and Codex (`npm run setup-agents` is the interactive way to limit them to the tools you use). Run it in
the clone, because the hooks need a Git repository. Restart your coding agent session afterwards so it
loads the agents. Git does not regenerate the agent files on checkout, pull or rebase: run
`python3 scripts/agent_setup.py sync` after switching branches (`bootstrap.py check` reports out-of-date agent
files). When you review an untrusted pull request, do not run its code or install its dependencies;
review it on GitHub, or locally with `git -c core.hooksPath=/dev/null`. Lefthook reads `lefthook.yml`
from the working tree whenever any hook runs, so use `git -c core.hooksPath=/dev/null` for checkouts
and commits on untrusted branches. Older clones may still have the removed `post-checkout`,
`post-merge` and `post-rewrite` hooks, which run the checked-out branch's `lefthook.yml`: remove them
with `rm -f .git/hooks/post-checkout .git/hooks/post-merge .git/hooks/post-rewrite`
(`python3 scripts/agent_setup.py check` fails locally and prints this command while they exist).

## `project-context.toml`

```bash
python3 scripts/bootstrap.py init --description "What the project does" --create-board
```

`init` takes the owner and project name from the `origin` remote and prompts for the rest in a
terminal; without one, or with `--yes`, it names the flag for any missing value. The board is
`--project-number N` for an existing one or `--create-board` for a new empty one. Other flags:
`--name`, `--owner`, `--required-approvals` (default 0), `--force` (overwrite). The result is validated before it
is written:

```toml
[project]
name = "widgets"
description = "What the project does"

[github_project]
owner = "acme"
number = 3

[review]
required_approvals = 0
```

`required_approvals` is the number of independent approvals a pull request needs. It is required and
has no implicit default: a missing `[review]` section or key makes validation fail. `init` always
writes it and proposes 0 for a new project (`--required-approvals N` sets another number); the
constitution decides the number. **A solo project should choose 0**, because the author cannot
approve their own pull request. A team project must set at least 1; 0 lets any member merge their
own pull request without review. The same number goes into the constitution and the ruleset (see
below). The ruleset enforces it; GitHub keeps each review (reviewer, state, reviewed commit and
time) on the pull request, so there is no separate approval record. With 0, branch protection
requires no approvals. To see who approved a change, find its pull request (the merge commit names
it) and run
`gh pr view <n> --json reviews --jq '.reviews[] | "\(.author.login) \(.state) \(.commit.oid[0:7]) \(.submittedAt)"'`.

**Upgrading an existing repository.** Add `[review] required_approvals = N` to
`project-context.toml`, with the number the constitution states. The old implicit value was 1, so
`required_approvals = 1` keeps the current ruleset. Lowering an existing ruleset to 0 needs
`bootstrap.py github --apply --allow-weaken`.

## Constitution

Run the `speckit-constitution` skill and give it the `required_approvals` number, so its approval
rule states the same number. Keep them equal; if they differ, fix `[review] required_approvals` to
match the constitution and rerun `bootstrap.py github --apply`. Write governance for your project; do
not copy another project's product scope, technology requirements or numeric coverage targets. The
toolkit's agents and docs refer to the constitution's principles or sections by name, so give your
constitution principles or sections that these names resolve to (for example a "Merge and Release
Gates" section):

- **Scope boundaries:** what is in and out of scope, which the agents and `tdd-implementer` check
  changes against.
- **Spec-driven development:** an approved spec and plan before implementation; the initial `tasks.md`
  is generated and analyzed before coding, with no separate tasks-only issue or PR; `tasks.md` may
  evolve in reviewed implementation PRs, with another `speckit-analyze` after structural edits.
- **Traceable work tracking:** a roadmap entry has an Epic and a spec-and-plan Planning issue; each
  Phase in `tasks.md` is one GitHub issue; `T###` tasks never become issues.
- **Git workflow:** independent, non-author approval before merge, with the approval count above.
- **Merge gates:** the reviews and checks required before merge, including the `code-reviewer` pass.
- **Team-decision rules:** which decisions (for example architecture choices) need a written
  proposal and approval by another team member.
- **Security principle:** when a change needs documented security review (authentication,
  permissions, data access, secrets, IAM); authorization is enforced server-side.
- **Quality gate:** the automated tests, coverage minimums and checks your project actually needs.
- **Technology baseline:** the languages, frameworks and tools the project uses, which the agents
  check changes against.
- **Dev environment:** where changes are deployed and tried out before release.

## Roadmap

Run the `roadmap-write` skill to create `ROADMAP.md` from the constitution and your project brief. It
is the only skill that edits the roadmap, which is plan-only: GitHub holds progress. The format is in
[roadmap-format.md](roadmap-format.md); validate it with `python3 scripts/check_roadmap.py`. An
entry's ID is the feature ID: its spec lives in `specs/<id>-<slug>/`.

Prepare these for each entry before you run the skill; it stops and asks for anything missing:

- a three-digit ID and a title;
- a plan status: `planned`, or `undecided`, `needs-info`, `deferred` or `abandoned`;
- a **Description** (what the feature covers) and an **Outcome** (the intended result);
- **Scope (in)**: the work the entry includes;
- **Depends on**: `none`, or the IDs of the entries it needs first.

Optional: Scope (out), Governed by, Spec dir and Notes. Only `planned` entries get an Epic with
an open planning issue and Phase 1; a `needs-info` entry gets only an Epic labelled `needs-info`.

## GitHub settings

```bash
python3 scripts/bootstrap.py github            # preview: one line per planned change
python3 scripts/bootstrap.py github --apply    # make the changes
python3 scripts/bootstrap.py github --check    # change nothing; exit 1 if anything differs
```

It reads `project-context.toml` (run `init` first). Exit codes: 0 done or nothing to do, 1 drift under
`--check`, 2 error. It manages:

- **Labels:** the toolkit's issue and override labels, created or updated.
- **Board:** the `Status` options (`Backlog`, `Planned`, `In progress`, `In review`, `Done`), the
  `Roadmap order` field and the repository link. A fresh board's `Todo` and `In Progress` are renamed.
- **Ruleset** `Default branch protection`: no deletion or force pushes, a pull request with
  `required_approvals` approvals, and the required checks. An existing ruleset keeps its other
  settings. With 2 or more approvals, stale approvals are dismissed when new commits are pushed.
- **Actions:** the repository's fork pull request workflow approval policy is raised to
  `all_external_contributors`, so workflows that run a pull request's code wait for approval from every
  outside contributor, not only first-time ones. A stricter policy is never lowered. Needs admin rights.
  Private repositories have no such policy (GitHub answers 422); they are skipped with a `Note:`.

`--apply` never weakens an existing ruleset: it does not lower the approval count, turn off approval
of the last push unless you pass `--allow-weaken`. Without it, it prints a `Warning:` and leaves the
setting, and `--check` exits 1. Stale-review dismissal is never turned off by the command: it is turned
on with 2 or more approvals and otherwise left as it is. When the ruleset has `bypass_actors`, the
command prints a `Note:`, because anyone or any App listed there can merge around the review
requirement.

Details are in [scripts/README.md](../scripts/README.md#bootstrappy). It only reports secrets and
variables (a missing board credential is a warning, not drift). Run `github --apply` **before**
opening the bootstrap pull request: the ruleset then applies to that pull request, so with 1 or more
approvals you need a second person to approve it.

One board setting cannot be changed through the API: open the board's **Workflows**, enable "Pull
request linked to issue" and set Status to `In review`. The command prints this as a manual step.
That built-in workflow reacts to pull requests linked to an issue (a closing keyword or a manual
link); Phase pull requests carry only `Part of #<phase issue>`, so the toolkit's own
`sync-project-status` workflow moves a Phase to `In review` from the open pull request on its Phase
branch, and the setting matters for other issues.

## Board credentials

No token is required. The workflows that update the GitHub Project board (`sync-roadmap-issues`,
`sync-spec-tasks`, `sync-project-status`) work without a board credential: board sync is skipped with
a notice and issues are still synced. To enable board sync, configure one of the two credentials
below. Issue writes always use the workflow's own `GITHUB_TOKEN`; the board credential is used only
for the board. The board owner (`github_project.owner`) must match the repository owner for both
credentials (case-insensitive). On GitHub Free, organization secrets may not be available to
private repositories; repository secrets always work.

The `*_APP_ID` secrets may hold the App's Client ID or its numeric App ID; the workflows pass the
value to `actions/create-github-app-token` as `client-id`.

All credentials the toolkit can use:

| Secret                                                              | Used by                        | Credential and scopes                                                       |
| ------------------------------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------- |
| `PROJECT_STATUS_APP_ID`, `PROJECT_BOARD_APP_PRIVATE_KEY` (optional) | The three board sync workflows | GitHub App: Issues read, organization Projects write                        |
| `PROJECT_BOARD_TOKEN` (optional)                                    | The three board sync workflows | Classic PAT: `project`, plus `repo` (`public_repo` for a public repository) |

GitHub Apps and fine-grained PATs cannot access Projects owned by a personal account. A
personal-account board therefore needs a classic PAT with the `project` scope and read access to the
repository's issues and pull requests: `repo` for a private repository, `public_repo` for a public
one. The classic PAT needs no other scope: `read:org` is not required for a personal-account board.
A fine-grained PAT with the organization's Projects permission also works for an organization
board, but the App is preferred. If both credentials are set, the GitHub App is used.

The trade-off of a personal token: anyone with push access can add a workflow that reads repository
secrets, so they could act as you with it. For a solo project (`required_approvals = 0`) or one
where you are the only person who can push, that is usually acceptable.

**Teams relying on required reviews.** If `[review] required_approvals` is 1 or more and several
people can push, you should not store a personal token: a leaked one could approve pull requests as
that person. You should use the GitHub App instead, or a dedicated machine account added to the
Project with Write access and with read access to the repository (none for a public one).
`bootstrap.py check` prints a hint when it sees this combination.

What each credential can do if it leaks:

- **`PROJECT_BOARD_TOKEN`:** read and write on every Project its account can reach, and read access
  to the repositories that account can read (a classic `repo` scope covers all of them, private ones
  included). A personal token can also act as you elsewhere, including submitting reviews; a machine
  account without write access to the repository cannot push, and its reviews do not count for the
  ruleset.
- **`GITHUB_TOKEN`:** the permissions each workflow declares, for the length of the run.

### Organization board (GitHub App)

Set it up once per organization; the two secrets can be organization-level secrets. Install the App
on every repository that uses it ("All repositories", or add each new repository to the
installation), because the token is requested for the repository by name.

```bash
gh secret set PROJECT_STATUS_APP_ID --body "<client id or app id>"
gh secret set PROJECT_BOARD_APP_PRIVATE_KEY < path/to/private-key.pem
```

The App needs only repository permission Issues: read and organization permission Projects: write,
because the workflows read the repository with `GITHUB_TOKEN`. Both secrets are needed: with only
one, the workflows do not use the App and silently fall back to `PROJECT_BOARD_TOKEN` or skip board
sync (`bootstrap.py github` warns). Tokens are scoped to this repository by name, taken from
`GITHUB_REPOSITORY` (a scheduled run has no event repository), so a scheduled run does not get a
token for every repository of the installation.

### Personal-account board, or the simple path (classic PAT)

```bash
gh secret set PROJECT_BOARD_TOKEN   # paste the token when prompted
```

Create your own classic PAT with `project`, plus `repo` (private repository) or `public_repo`
(public one), and set an expiry. The board stays owned by the repository owner, because the code
requires `github_project.owner` to be the repository owner. A classic PAT is not limited to one
repository: `repo` reaches every repository you can read. Rotate the token before it lapses. The
PAT is never printed and is passed to scripts only through the environment. Teams relying on
required reviews should follow the note above.

Do not add issue templates that apply the `Epic`, `Story` or `Planning` label automatically: the
roadmap and Phase sync trust an issue that carries one of those labels, so such a template would let
outsiders create trusted marker issues.

### Optional hardening: a GitHub Environment

Anyone with write access can read repository secrets from a workflow they push. To narrow this, store
the credentials as secrets of a GitHub Environment that is restricted to the default branch, and name
that environment in the jobs that use them. Note that `sync-project-status` also has a trigger for
pushes to phase branches, so its job would need to run from the default branch only (or skip board
updates on phase branches) before it can use such an environment.

### Local fallback

Without any secret, run the board scripts with your own `gh` login:

```bash
python3 scripts/sync-project-status.py --all --apply
python3 scripts/sync-roadmap-issues.py --apply
```

## One bootstrap pull request

Commit `project-context.toml`, the constitution and `ROADMAP.md` together in one pull request. Once
any of them exists the repository counts as initialized, so a partial setup fails CI: a missing or
invalid `ROADMAP.md` fails the roadmap check, and a missing `project-context.toml` fails board sync
when a board credential is configured.

- **The first branch needs an issue.** Create a `CHORE:` issue and a linked branch named
  `<issue>-chore-bootstrap` (`gh issue develop <issue> --base main --name <issue>-chore-bootstrap --checkout`);
  the **Validate branch and link issue** check rejects other names.
- **Required checks** (from the ruleset): `Validate branch and link issue`,
  `Spec and plan merge before tasks`, `Repository tools` and `Roadmap prerequisites`.
- **Approvals:** the number in `[review] required_approvals`; with 0 you merge it yourself.
- **Code owners:** the toolkit ships no CODEOWNERS file. If you want code-owner reviews, add one and turn on "Require review from Code Owners" in the ruleset yourself; `bootstrap.py github` does not manage it.

## How the syncs behave

Three workflows keep GitHub in step with `ROADMAP.md` and `tasks.md`. Without a board credential they
skip board updates and still sync issues (`sync-project-status` skips entirely).

| Workflow                                             | Runs on                                                                 | Preview or apply                                                                                                                      |
| ---------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `sync-roadmap-issues` (Epics, Planning, Phase 1)     | Push to `main` changing the roadmap or specs; daily; manual             | Push and daily runs preview only. Manual runs preview unless **Apply changes** is checked.                                            |
| `sync-spec-tasks` (Phase issues from `tasks.md`)     | A merged PR to `main`; daily; manual (optional `feature` input)         | A merged PR that changes `tasks.md` applies. Daily runs preview unless `PHASE_SYNC_AUTO_APPLY` is `true`. Manual runs need **Apply**. |
| `sync-project-status` (board Status of Phase issues) | Phase branch and PR events; issue close, reopen or label; daily; manual | Always applies; changes the Status of existing Phase issues and takes retired Phases off the board; it creates and closes no issues.  |

- **Retired Phases leave the board.** A Phase removed from `tasks.md` is closed as not planned and its card is taken off the Project (removed again by any later run if it is still there); the issue stays closed as not planned. Closing a Phase as not planned by hand retires it too: the `issues: closed` event runs `sync-project-status`, which removes the card right away.
- **The first roadmap sync is a manual, reviewed step.** After the bootstrap pull request merges, run
  `gh workflow run sync-roadmap-issues.yml -f apply=false`, read the log, and only then run it with
  `-f apply=true` (or use the Actions tab). Until then Phase syncs find no Epic, preview with a
  warning and skip the feature.
- **`PHASE_SYNC_AUTO_APPLY`** is a repository variable. Set it to `true` only after reviewing the
  proposed full reconciliation; `bootstrap.py github` only reports whether it is set. Leave it unset
  to keep daily runs as previews.
- **Idempotent and recoverable.** The syncs find their issues by markers and change only what
  differs, so rerunning a failed run is safe and rerunning a finished one does nothing. After a
  partial failure, fix the cause and run the same sync again. `sync-task-issues.py --check` exits 1
  while changes are pending.
- **Never point an unreviewed apply at a board that matters.** Review a preview first, and test a
  new setup against a scratch repository and board.
- Issue changes made with `GITHUB_TOKEN` do not trigger `sync-project-status`; a board Status can be
  stale until its daily run (05:17 UTC).

## Your first feature

If the roadmap has no planned entry yet, add one (`roadmap-write`) and merge it. Run the first sync,
then start from the entry's `SPEC: <ID> Create and approve spec and plan` issue. The step order is
specify → clarify → plan → tasks → analyze → implement, enforced by the hooks in
`.specify/extensions.yml`; before `speckit-specify`, set `SPECIFY_FEATURE_DIRECTORY=specs/<id>-<slug>`
so it matches the roadmap entry. The spec and plan merge in their own pull request; `tasks.md` is
generated afterwards on the Phase 1 implementation branch. See [AGENTS.md](../AGENTS.md) for the rules
agents follow and the [README](../README.md#spec-kit-workflow-best-practices) for the whole flow.

## Your application's CI

No application pipeline ships with the template. Add your own workflow file under
`.github/workflows/` (separate from `toolkit.yml`) with its own job names. To make a job block
merging, add its name to the ruleset's required checks (Settings → Rules → Rulesets; a check is
searchable there after it has run once on a pull request). `bootstrap.py github` adds its four
required checks and keeps any others you add.

## The Node version

`.nvmrc` is required: the **Repository tools** job reads it (`node-version-file: .nvmrc`), and if it is
missing `setup-node` fails that required check. Changing it for your application also changes the Node
version the toolkit CI runs on.

## Checking progress

```bash
python3 scripts/bootstrap.py    # same as: bootstrap.py check
```

It prints each step with `✓` done, `✗` to do or `–` optional or skipped, and the next action; GitHub
settings are skipped without a logged-in `gh`. It exits 0 when no step is `✗`, else 1.

## Troubleshooting

| Message or symptom                                           | Fix                                                                                                |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `... already exists; use --force`                            | `project-context.toml` exists; edit it, or rerun `init --force`.                                   |
| `A GitHub Project number is required`                        | Pass `--project-number N` or `--create-board`.                                                     |
| `gh` fails with a missing scope                              | `gh auth refresh -s project`.                                                                      |
| `github_project.owner must match the repository owner`       | Use a board owned by the repository's owner; cross-owner boards are not supported.                 |
| `no GitHub Project #N found`                                 | Create the board and set `github_project.number`.                                                  |
| `bootstrap.py github` exits 2 on the ruleset (403 or 404)    | Either you lack admin rights (ask an admin) or the plan has no rulesets for a private repository (make it public or upgrade). The message names the likely cause. |
| `Roadmap validation failed: ROADMAP.md is missing`           | A partial setup: add `ROADMAP.md` (and the other files) in the same pull request.                  |
| `Missing project context` in a workflow                      | Commit `project-context.toml` (`python3 scripts/bootstrap.py init`).                               |
| **Repository tools** fails at setup-node                     | `.nvmrc` is missing.                                                                               |
| A pull request waits for a required check that never runs    | The job name or workflow changed; update the ruleset's required checks.                            |
| Branch name rejected                                         | Use `<issue>-chore-<title>` (or `<issue>-phase-<n>-<title>`) from `gh issue develop`.              |
| "Board sync skipped" notice                                  | No board credential or no `project-context.toml` yet; see [Board credentials](#board-credentials). |
| Sub-agents not found or out of date                          | `python3 scripts/agent_setup.py sync`, then restart the agent session. |
| Git hooks do not run                                         | See [Troubleshooting: hooks don't run](../README.md#troubleshooting-hooks-dont-run).               |
