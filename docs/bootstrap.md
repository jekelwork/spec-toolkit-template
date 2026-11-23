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

- `gh` 2.40 or newer, installed and logged in: `gh auth login`, then `gh auth refresh -s project`
  (the board needs the `project` scope).
- Python 3.11 or newer.
- Node as pinned in `.nvmrc`, then `npm ci` in the repository root.
- Repository admin rights on GitHub, for the ruleset.
- A public repository, or a GitHub plan that supports rulesets on private repositories (Pro, Team or
  Enterprise). On GitHub Free, rulesets work only on public repositories.
- Optional: your choice of coding agents. `start` asks which coding agents you use (Claude Code,
  GitHub Copilot, Codex) once, when no choice is saved, and generates only their sub-agent files.
  Enter saves all three; with `--no-input` nothing is asked. Run `npm run setup-agents` again
  whenever you change coding agents, to generate the files for the new ones.

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

Each step is explained below. To let the toolkit work through the steps with you instead, see
[Guided path with start](#guided-path-with-start), or to be guided in a conversation, see
[Guided path in your coding agent](#guided-path-in-your-coding-agent); the manual steps stay valid
and the guided path does the same things.

## Create the repository from the template

`gh repo create --template` (or the **Use this template** button on GitHub, followed by `git clone`)
creates your repository with the template's files as a single initial commit on `main`, with a fresh
history. GitHub copies files only: labels, the ruleset, secrets and the board are set up by the
bootstrap steps below. If the clone comes out empty because GitHub was still generating the
repository, run `git fetch origin && git checkout main` in it. `npm ci` runs the `prepare` script,
which installs the Git hooks and runs `python3 scripts/agent_setup.py sync` to generate the
sub-agent files for Copilot, Claude Code and Codex (`npm run setup-agents` is the interactive way
to limit them to the tools you use). Run it in the clone, because the hooks need a Git repository.
Restart your coding agent session afterwards so it loads the agents. Git does not regenerate the
agent files on checkout, pull or rebase: run `python3 scripts/agent_setup.py sync` after switching
branches (`bootstrap.py check` reports out-of-date agent files). When you review an untrusted pull
request, do not run its code or install its dependencies; review it on GitHub, or locally with
`git -c core.hooksPath=/dev/null`. Lefthook reads `lefthook.yml` from the working tree whenever
any hook runs, so use `git -c core.hooksPath=/dev/null` for checkouts
and commits on untrusted branches. Older clones may still have the removed `post-checkout`,
`post-merge` and `post-rewrite` hooks, which run the checked-out branch's `lefthook.yml`: remove them
with `rm -f .git/hooks/post-checkout .git/hooks/post-merge .git/hooks/post-rewrite`
(`python3 scripts/agent_setup.py check` fails locally and prints this command while they exist).

## Bootstrap issue

The bootstrap needs an issue and a branch named `<issue>-chore-bootstrap`, because the **Validate
branch and link issue** check rejects other names (see
[One bootstrap pull request](#one-bootstrap-pull-request)). The manual way is the two `gh`
commands in the [Quick start](#quick-start). `check --json` and `start` find the issue by itself:
an open issue counts as the bootstrap issue when it carries the hidden
`<!-- spec-toolkit:bootstrap -->` marker and was opened by the logged-in user, or when a branch
named `<issue>-chore-bootstrap` exists for it (local, on GitHub, or checked out). With one
candidate `start` uses it; with none it creates one titled `CHORE: bootstrap the project` with the
marker; with several it asks which one (see [`--issue`](#--confirm)). The step is done when its
branch is checked out, or once the bootstrap is merged.

## `project-context.toml`

```bash
python3 scripts/bootstrap.py init --description "What the project does" --create-board
```

`init` takes the owner and project name from the `origin` remote and prompts for the rest in a
terminal; without one, or with `--yes`, it names the flag for any missing value. The board is
`--project-number N` for an existing one or `--create-board` for a new empty one. Other flags:
`--name`, `--owner`, `--required-approvals` (default 0), `--force` (overwrite). The result is
validated before it is written:

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

The skill also writes a Sync Impact Report, an HTML comment at the top of the file with notes on
what changed. It is meant for review only and is not committed. You do not need to remove it
yourself: when `bootstrap.py start` commits the constitution in the `pull_request` step, it removes
that comment first, and its preview says so. With the manual path, and for later amendments of the
constitution, delete the comment before you commit.

## Roadmap

Run the `roadmap-write` skill to create `ROADMAP.md` from the constitution and your project brief. It
is the only skill that edits the roadmap, which is plan-only: GitHub holds progress. The format is in
[roadmap-format.md](roadmap-format.md); validate it with `python3 scripts/check_roadmap.py`. An
entry's ID is the feature ID: its spec lives in `specs/<id>-<slug>/`.

A first version is enough (one entry is fine), and `roadmap-write` can amend it at any time: add
entries, change scope or dependencies, pause an entry with `needs-info` or `deferred`, or drop it
with `abandoned` (an abandoned entry's issues are not reopened automatically). If you are unsure
what to write, ask the skill for a first draft: it proposes a complete first version from the
constitution and the brief (if any), marks what it inferred, and writes only after your yes.
Otherwise it asks for anything missing. Each entry needs:

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
request linked to issue" and set Status to `In review`. The command prints this as a manual step
(`Manual step (the API cannot read or set it): in <board> open Workflows and check that "Pull request
linked to issue" is enabled with Status = In review; set it if not.`). `github --apply` prints it
after `Applied N change(s).`; `--check` and a preview with nothing to change also print it; a
preview with changes leaves it out, because a new board has no `In review` option before the
apply. `start` leaves it out of its preview and prints it after `Done: github_settings: …`. A new
board turns this workflow on with Status `In progress`, and `check` can only see whether it is on,
not which Status it sets, so check the Status yourself even when `check` reports the workflow as
on.
That built-in workflow reacts to pull requests linked to an issue (a closing keyword or a manual
link); Phase pull requests carry only `Part of #<phase issue>`, so the toolkit's own
`sync-project-status` workflow moves a Phase to `In review` from the open pull request on its Phase
branch, and the setting matters for other issues.

## Board credentials

No token is required. The workflows that update the GitHub Project board (`sync-roadmap-issues`,
`sync-phase-issues`, `sync-project-status`) work without a board credential: board sync is skipped with
a notice and issues are still synced. To enable board sync, configure one of the two credentials
below. Issue writes always use the workflow's own `GITHUB_TOKEN`; the board credential is used only
for the board. The board owner (`github_project.owner`) must match the repository owner for both
credentials (case-insensitive). On GitHub Free, organization secrets may not be available to
private repositories; repository secrets always work.

The `*_APP_ID` secrets may hold the App's Client ID or its numeric App ID; the workflows pass the
value to `actions/create-github-app-token` as `client-id` (through the shared `.github/actions/board-token` action).

Set every secret in a separate terminal window, or on the repository's Settings → Secrets and
variables → Actions page. Never run `gh secret set` through a coding agent's shell or a prefix that
runs it there, such as Claude Code's `!`. The prompting form (`gh secret set PROJECT_BOARD_TOKEN`
with no body option or input redirection) reads empty input without a terminal and sets an empty
secret, which `check` then reports as configured.

For the classic token, `check` supplies the command in the `board_credential` step's action:
`gh secret set PROJECT_BOARD_TOKEN -R <owner>/<repo>`, with the repository named so a terminal in
another repository cannot set the secret there. The coding agent shows that command and never
builds it itself.

All credentials the toolkit can use:

| Secret                                                              | Used by                        | Credential and scopes                                                       |
| ------------------------------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------- |
| `PROJECT_STATUS_APP_ID`, `PROJECT_BOARD_APP_PRIVATE_KEY` (optional) | The three board sync workflows | GitHub App: Issues read, organization Projects write                        |
| `PROJECT_BOARD_TOKEN` (optional)                                    | The three board sync workflows | Classic PAT: `project`, plus `repo` (`public_repo` for a public repository) |

GitHub Apps and fine-grained PATs cannot access Projects owned by a personal account. A
personal-account board therefore needs a classic PAT with the `project` scope and read access to the
repository's issues and pull requests: `repo` for a private repository, `public_repo` for a public
one. Classic scopes cannot be limited to reading: `repo` grants read and write, and `public_repo`
grants write to public repositories. The classic PAT needs no other scope: `read:org` is not
required for a personal-account board.
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

- **`PROJECT_BOARD_TOKEN`:** read and write on every Project its account can reach, and read and
  write on every repository that account can access, with the account's own rights. A classic `repo`
  scope covers all of them, private ones included: pushing code, changing issues and pull requests,
  and changing settings where the account is an admin. A `public_repo` scope covers the public ones.
  The workflows only read the repository, but a classic scope cannot be limited to that. A personal
  token can also act as you elsewhere, including submitting reviews; a machine
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
repository: `repo` gives read and write on every repository you can access, private ones included,
with your own rights. Rotate the token before it lapses. The PAT is never printed and is passed to
scripts only through the environment. Teams relying on required reviews should follow the note
above.

### Revoke and replace a leaked token

Treat a token as leaked when it was pasted into a chat, a pull request, an issue, a log or any place
other people or tools can read, even briefly. Do these steps yourself, outside the chat. For a
personal access token (`ghp_` or `github_pat_`):

1. Revoke it: GitHub, Settings, Developer settings, Personal access tokens, Tokens (classic) (or
   Fine-grained tokens for a `github_pat_` token), then delete the token (for a machine account,
   sign in as that account).
2. If the leaked token was the board token, create a new one of the same kind with the same access
   (a classic token: `project`, plus `repo` or `public_repo`; a fine-grained token: the
   organization's Projects permission) and an expiry. Otherwise replace it wherever it was used; it
   does not go into a board secret.
3. For the board token, set it again in a separate terminal window (not the agent's shell or `!`;
   see [Board credentials](#board-credentials)), where the command prompts for the value, or on
   the Actions secrets page (repository Settings, Secrets and variables, Actions; an
   organization-level secret is set in the organization's settings instead):

   ```bash
   gh secret set PROJECT_BOARD_TOKEN
   ```

4. Check that the name is listed with `gh secret list`, or run `python3 scripts/bootstrap.py check`.
   Both show names only, never values.

Other cases:

- **The App's private key** (`PROJECT_BOARD_APP_PRIVATE_KEY`): GitHub requires an App to keep at
  least one private key, so generate a new key in the App's settings, delete the leaked one right
  away, then set the secret again in a separate terminal window (not the agent's shell or `!`) or on
  the Actions secrets page. The
  `PROJECT_STATUS_APP_ID` secret holds an identifier, not a credential, and needs no change.
  Installation tokens already made with the old key stay valid until they expire, within an hour.
- **Tokens that start with `ghs_`, `ghu_` or `ghr_`** come from a GitHub App. A `ghs_` installation
  token expires within an hour. Rotating the App's private key (as above) stops new ones from being
  made but does not revoke one already issued, so wait for it to expire or revoke it early with the
  REST API (`DELETE /installation/token`, authenticated with that token). A `ghu_` user token or
  `ghr_` refresh token is not tied to the private key: revoke it under Settings, Applications,
  Authorized GitHub Apps (Revoke). As the App owner you can also revoke a `ghu_` token with the
  "Delete an app token" API.
- **A token that starts with `gho_`** is an OAuth App token. Revoke the app that issued it under
  Settings, Applications, Authorized OAuth Apps. For the GitHub CLI (the token `gh auth login`
  stores when you log in through the browser), revoke "GitHub CLI", which revokes every token it
  issued for your account, then run `gh auth login` again. If `gh` was logged in with a personal
  access token, revoke that token as in step 1.

Check your account's security log (Settings, Archives, Security log) and, for an organization, its
audit log for activity you do not recognize. Update any other place that used the old token. If the
value was pasted into a chat, delete that message from the chat history if your tool allows it.

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
- **Review before it opens:** like every pull request, the bootstrap pull request gets the
  `code-review-and-quality` review on its exact final diff before it is opened (see `AGENTS.md`),
  and the review runs again if a file changes afterwards. It checks, for example, that the
  constitution has the sections listed in [Constitution](#constitution). The review looks at the
  constitution without its Sync Impact Report comment, so the `pull_request` step removing that
  comment does not count as a change after the review.
- **Bootstrap review exception:** to keep the first run short, the review findings of this pull
  request are not handed to you to decide, for the bootstrap pull request only. The agent fixes the
  review's Critical and Required findings itself through the skills (`speckit-constitution` and
  `roadmap-write`), and in the first round also its Optional and Nit findings where feasible,
  without asking, and reports them in one line; a second round fixes only Critical and Required
  findings and drops the rest. A finding whose fix would change what the constitution or roadmap
  decides is still asked of you, together in one multi-select question; dropped and FYI findings
  are counted in the chat and listed in the pull request body. The agent writes them, one line each
  under `## Review notes`, to `bootstrap-review-notes.md` at the repository root (Git ignores it),
  the only file the agent writes outside the skills, and passes `--review-notes` to the
  `pull_request` preview and its confirming command. With none of them there is no notes file and
  no `--review-notes`. A constitution amended before this pull request keeps version 1.0.0,
  because no earlier version was ever committed. After the bootstrap, every finding is relayed to
  you again. The rule is in
  [review-and-approvals.md](review-and-approvals.md#bootstrap-review-exception).
- **Code owners:** the toolkit ships no CODEOWNERS file. If you want code-owner reviews, add one and
  turn on "Require review from Code Owners" in the ruleset yourself; `bootstrap.py github` does not
  manage it.

## How the syncs behave

Three workflows keep GitHub in step with `ROADMAP.md` and `tasks.md`, and a fourth, `sync-roadmap-preview`, previews the roadmap sync on pull requests. Without a board credential they
skip board updates and still sync issues (`sync-project-status` skips entirely).

| Workflow                                             | Runs on                                                                 | Preview or apply                                                                                                                      |
| ---------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `sync-roadmap-issues` (Epics, Planning, Phase 1)     | Push to `main` changing the roadmap or specs; daily; manual             | A push applies once the first sync has run; before that, and on daily runs, it only previews. Manual runs preview unless **Apply changes** is checked. |
| `sync-phase-issues` (Phase issues from `tasks.md`)     | A merged PR to `main`; daily; manual (optional `feature` input)         | A merged PR that changes `tasks.md` applies. Daily runs preview unless `PHASE_SYNC_AUTO_APPLY` is `true`. Manual runs need **Apply**. |
| `sync-project-status` (board Status of Phase issues) | Phase branch and PR events; issue close, reopen or label; daily; manual | Always applies; changes the Status of existing Phase issues and takes retired Phases off the board; it creates and closes no issues.  |

- **Retired Phases leave the board.** A Phase removed from `tasks.md` is closed as not planned and
  its card is taken off the Project (removed again by any later run if it is still there); the
  issue stays closed as not planned. Closing a Phase as not planned by hand retires it too: the
  `issues: closed` event runs `sync-project-status`, which removes the card right away.
- **The first roadmap sync is a manual, reviewed step.** After the bootstrap pull request merges, run
  `gh workflow run sync-roadmap-issues.yml -f apply=false`, read the log, and only then run it with
  `-f apply=true` (or use the Actions tab). Until then Phase syncs find no Epic, preview with a
  warning and skip the feature. Until a roadmap Epic exists, push runs only preview and say so.
- **Later roadmap merges apply automatically.** Once at least one roadmap Epic exists, a push to
  `main` that changes `ROADMAP.md` or a spec artifact applies the sync (closures included), with no
  further approval; a failed run is completed by rerunning it. Set the repository variable
  `ROADMAP_SYNC_AUTO_APPLY` to `false` (any case) to turn this off: push runs then only preview and
  applying is manual. Unset or `true` leaves it on; any other value, including one with surrounding spaces, fails the push run with an error.
  `bootstrap.py github` only reports whether the variable is set, never its value. Roadmap applies
  run in their own `issue-sync-roadmap` queue, apart from the Phase sync's `issue-sync`, because one
  merge of a `tasks.md` change starts both and a shared queue would drop one; both are idempotent
  and marker-based, so running side by side is safe. Neither is ever cancelled.
- **Pull requests get a preview.** The `sync-roadmap-preview` workflow dry-runs the sync on a pull
  request to `main` that changes those files. It runs the scripts of the merge commit's first
  parent (the base branch tip) on the merge commit's `ROADMAP.md` and `specs/` only (symbolic links there are rejected; a pull request without
  either path is fine). Pull requests from forks cannot change what runs; for a same-repository pull
  request GitHub runs its own copy of the workflow file, so review changes to `.github/` and
  `scripts/` before trusting the preview. If the pull request changes any file other than those two
  paths, the summary says so and that the merged code may behave differently. It
  writes the planned changes, and whether merging will apply them, to the job summary. For a pull
  request from the same repository it also creates or updates one comment (found by a hidden marker
  that the comment job writes), so reruns edit it in place; fork pull requests get the summary only.
  It uses no secret and no board credential, so board changes are not previewed. On the pull request
  that first adds this workflow the preview fails, because the base branch has no such scripts yet.
- **Idempotent and recoverable.** The syncs find their issues by markers and change only what
  differs, so rerunning a failed run is safe and rerunning a finished one does nothing. After a
  partial failure, fix the cause and run the same sync again. `sync-task-issues.py --check` exits 1
  while changes are pending.
- **No issue templates that apply labels.** Do not add issue templates that apply the `Epic`, `Story`
  or `Planning` label automatically: the roadmap and Phase sync trust an issue that carries one of
  those labels, so such a template would let outsiders create trusted marker issues.
- **Never point an unreviewed apply at a board that matters.** Review a preview first, and test a
  new setup against a scratch repository and board.
- Issue changes made with `GITHUB_TOKEN` do not trigger `sync-project-status`; a board Status can be
  stale until its daily run (05:17 UTC).

## Your first feature

If the roadmap has no planned entry yet, add one (`roadmap-write`) and merge it. Run the first sync,
then start from the entry's `SPEC: <ID> Create and approve spec and plan` issue. The step order is
specify → clarify → plan → tasks → analyze → implement, enforced by the hooks in
`.specify/extensions.yml`. In your coding agent, run `/speckit-specify` for the entry on its SPEC
issue branch (`$speckit-specify` in Codex): your agent derives
`SPECIFY_FEATURE_DIRECTORY=specs/<id>-<slug>` from the roadmap entry, so the directory prefix
matches it, and you do not set it or run the entry check yourself. The spec and plan merge in their
own pull request; `tasks.md` is generated afterwards on the Phase 1 implementation branch. See
[AGENTS.md](../AGENTS.md) for the rules agents follow and the
[Spec-driven workflow](spec-workflow.md#spec-kit-workflow-best-practices) for the whole flow.

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

## Guided path with start

```bash
python3 scripts/bootstrap.py start
```

`start` is the guided way through the steps above. It reads the state like `check --json`, shows
what the next step will do, and does it only after you confirm. It never does what only you can
decide or do: the constitution and the roadmap (it stops there and names the skill to run), the
review and merge of the bootstrap pull request, and the board credential and board workflow
settings (it prints them and continues, since they do not block). Everything it does is what the
manual commands above do, so you can leave the guided path and return to the manual one at any
point, and the other way round: `start` works out where you are from the repository and GitHub each
time, and stores no progress. It never overwrites an existing `project-context.toml`, never passes
`--allow-weaken`, and never stashes.

It acts on five steps, each after a preview:

- `bootstrap_issue`: checks out the issue's branch (after `git fetch origin <branch>` when it is only
  on GitHub), or runs `gh issue develop`, or first `gh issue create` (see
  [Bootstrap issue](#bootstrap-issue)). Uncommitted bootstrap files carry over to the branch and the
  preview lists them. Untracked files do not stop the step: they are listed in the preview,
  carried over to the branch and not committed. A changed tracked file outside the bootstrap files
  stops it, with the action `Commit, stash or revert the changes to <paths>, then run start again.`
  An `origin` that is not a GitHub repository or names another one than the one `gh` uses stops
  it too (see [Which repository `start` uses](#which-repository-start-uses)).
- `project_context`: runs `init` for a missing `project-context.toml`; with `--create-board` it also
  creates the board.
- `github_settings`: runs `github --apply`.
- `pull_request`: commits `project-context.toml`, the constitution and `ROADMAP.md` by name (nothing
  else; other changed files are only listed) with the message `CHORE: bootstrap the project`, runs
  `git push -u origin HEAD` and `gh pr create` with `Closes #<issue>`; only the parts that are still
  missing. It stops (exit 1, nothing written) and prints the fix when:
  - no bootstrap branch is checked out, a merge conflict, or a bootstrap file was deleted;
  - the branch is behind the default branch (`git pull origin <default>`, preceded by
    `git fetch origin` when the default tip is not in the clone yet);
  - the checked-out branch is not the open pull request's head branch (`git switch <branch>`);
  - local `HEAD` does not contain the pull request's head commit, or, with no pull request, the
    branch's tip on GitHub (`git pull --rebase origin <branch>`);
  - there is no pull request and nothing is committed beyond the default branch (commit the
    bootstrap files first);
  - `origin` or any of its push URLs is not a GitHub repository, or names another one than the one
    `gh` uses (see [Which repository `start` uses](#which-repository-start-uses)).
- `first_sync`: dispatches the first roadmap sync with `apply=true` and follows the run by polling it. It
  stops unless `ROADMAP.md`, `specs/`, `scripts/`, `.github/workflows/`, `.github/actions/`, `project-context.toml` and
  `.specify/memory/constitution.md` are committed.

`pull_request` with `--review-notes` adds the text of `bootstrap-review-notes.md` (at the repository
root) to the pull request body, after a blank line below `Closes #<issue>`. The preview then says
`Will add these review notes from <path> to the pull request body:` followed by every notes line
as `  | <line>`, so the confirmation digest covers the text you see. The template's `.gitignore`
ignores the file with the entry `/bootstrap-review-notes.md`, so it is never committed. `start`
exits 2, before anything is changed, when the file is missing, is not a regular file (a directory,
for example), is empty, is longer than 60 000 characters or 100 000 bytes (UTF-8; the read is
bounded, so a larger file is never read whole), contains an output-unsafe character, is a symbolic
link, or is not ignored by Git. It also refuses, naming the reason and never the match, notes that
contain a credential pattern (a `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` or `github_pat_` token, a
`-----BEGIN … PRIVATE KEY-----` header or an AWS access key ID), a line with a GitHub closing
keyword (`close`, `fix` or `resolve` in any form, with or without a colon) followed by an issue
reference or issue URL (also `www.github.com`, or in angle brackets), which would close an issue
on merge, or an `@` mention anywhere in a line (an `@` that follows a letter, a digit or a `.`, as
in an email address, is not a mention), which would notify someone. A tab or a carriage return (a
Windows line ending) is also refused, by name. With the pull request already open the
preview says `The pull request already exists; the review notes are not added.` Without the flag
the body is `Closes #<issue>` only.

`bootstrap_issue`, `pull_request` and `first_sync` are `performable: false` in `check --json`
whenever `start --confirm` would stop on `origin` (or, for `pull_request`, a push URL), with the
same stop in its title and the same command. Known limit: `performable` does not yet reflect three
stops `start` makes when acting, the `github_settings` stop "local project-context.toml differs from
the default branch" and the `pull_request` merge-conflict and deleted-bootstrap-file stops, so
`start --confirm` can still stop with exit 1 there.

Every preview of these steps (except `project_context`) includes a `Repository: <owner>/<name>`
line. Branch and default-branch names in the printed commands are shell-quoted, and only
`<issue>-chore-bootstrap`, optionally followed by `-`-separated lowercase letters and digits (the
names the branch check accepts), counts as a bootstrap branch.

### Terminal, `--no-input` and exit codes

`start` prints progress lines on stdout while it works
(`Reading the repository and GitHub state…`, one line per action, and for the first sync the run's
number and link and `Still running (<m> min <s> s)…`, at least every 30 seconds because each
`gh run view` is cut off in time), so a slow step never looks stuck. Everything that is not an
error goes to stdout, so a successful run never shows red in a tool that colours stderr; stderr
carries only errors (a failed step, a usage error, a failed sync). After it performs a step it
prints `Done: <step>: <result>.`. After `Done: github_settings: …` and the manual board line it
prints the optional `board_credential` step once, with its link to
[Board credentials](#board-credentials), when the credential is not done. It does not print it
again on later runs, and not when `github_settings` was done earlier (for example by hand with
`github --apply`); `check` still reports it, and the `github_settings` preview leaves its line out
(`github` itself still prints it). When bootstrap is complete, the last line, after a blank line,
is `BOOTSTRAP IS COMPLETE` or `BOOTSTRAP IS COMPLETE; MANUAL SETTINGS REMAIN (SEE ABOVE)`; it is
bold only on a terminal with `NO_COLOR` unset or empty.

In a terminal, `start` goes through the steps from the first unfinished one. At each of the five
steps it prints the preview, a line naming the step (a fixed label such as
`Confirm "Run the first sync":`), a last line `confirm: <step-id>@<digest>`, and asks
`Proceed? [y/N]`. Only `y` continues; anything else prints `Nothing was changed.` and exits 1.
Input typed before the prompt appears is discarded, so an answer is always one given after the
preview was shown. After your yes it reads the state and the preview again. It stops with exit 4
when either changed while the prompt waited, ends with exit 0 when bootstrap became complete
meanwhile, and with exit 1 when the step can no longer be done or a stop applies. A step that is
not done and does not block (an optional board credential, a manual setting) is printed with its
action, and `start` continues. At any other step it does not perform (`prerequisites`, the
constitution, the roadmap, the merge, an invalid `project-context.toml`) it stops with exit 1 and
prints the action, its command and the documentation anchor. A stop that a step finds for itself
(for example a checkout that has to be updated first) prints a title, what to do and the command,
but no anchor.

Without a terminal, or with `--no-input`, it never prompts. At the first step that needs a
confirmation it prints a blank line, `Next step (not done yet): <step>` and the preview. At exit 3
it then prints `To do it, run:` followed by the full command
`python3 scripts/bootstrap.py start --no-input --confirm <step-id>@<digest>` (with `--issue N` where
it applies, and, for `project_context`, the init flags, shell-quoted), the step-name line (for
example `Confirm "Run the first sync":`) and, last, the `confirm:` line. For each listed bootstrap
issue the label reads `Confirm "Use bootstrap issue #12":` and the command is printed per
candidate. Pass `--no-input` from a coding agent or script even if it runs in a pseudo-terminal,
where the prompt could otherwise wait or be answered by the tool itself.

At `project_context` without a terminal, missing `init` flags are decisions only you can make:
`start` prints `Next step project_context needs your decisions: <flags>`, then `To give them, run:`
and a command with placeholders, and exits 1 (nothing is wrong). The decisions line lists missing or
invalid flags: when at least one required flag is missing, a given flag whose value fails
validation is listed too, with one `<flag>: <validation message>` line each (never the value).
With no flag missing, an invalid value exits 2 naming the flag and the validation message on
stderr, never the value. With `--confirm`, the same missing or invalid flags exit 2 with a `needs:`
line on stderr.

With `--guided` (it requires `--confirm`, and exits 2 before reading anything without it) `start`
performs only the confirmed step, prints `Done: <step>: <result>.` and
`Done. Go back to your coding agent.`, and exits 0 without a next preview. If bootstrap is already
complete it prints the completion line and the same `Done.` line; on exit 1 to 4 it prints its
usual message, then `Go back to your coding agent and tell it what happened.` The coding agent
adds `--guided` to the commands it shows you in "guide me" mode, and asks you to reply "done" only
for a command you run in a separate terminal.

With `--agents <tool>,…` (`claude`, `copilot`, `codex`, comma-separated) `start` takes your choice of
coding agents without asking. It saves it as `agent-setup.tools` in your local Git config, generates
the sub-agent files for those tools (this also removes the generated agent files of the tools not
chosen; they come back if you choose them later with `npm run setup-agents`) and prints
`Coding agents: <tools> (saved in git config agent-setup.tools).` and a line saying to run
`npm run setup-agents` again whenever you change coding agents. Without `--confirm` (without a
terminal or with `--no-input`) it then prints
`Go back to your coding agent and tell it the choice is saved.`, ends with exit 0 and does not
preview the next step; with `--confirm` it continues with the steps as usual. Without the flag, a
terminal run that is not `--guided` and finds no saved choice asks
`Which coding agents do you use?` after its first state read (Enter saves all three); with
`--no-input` and no `--agents`, nothing is asked or saved. There is no preview or confirmation,
because it writes local Git config and git-ignored files only, and the previews and digests of the
steps do not change. An unknown or empty `--agents` value exits 2 before any state is read. While
saving the choice, `start` shows the progress line `Generating the agent files…`. If generating the
files fails (exit 2), the choice stays saved and a rerun syncs again.

Exit codes:

- 0: bootstrap is complete (or complete with manual settings still open).
- 1: stopped at a step `start` does not perform, a step that cannot be done now (the message says
  why), `project_context` without the `init` flags you still have to decide (without a terminal),
  a prompt answered with anything but `y`, or end of input at the coding-agent question (nothing
  is saved; Ctrl-C there exits 130 like every other prompt).
- 2: an error: a malformed `--confirm` value, a missing or invalid `init` flag after `--confirm`,
  `--guided` without `--confirm`, an unknown or empty `--agents` value, or a failed command. The
  message is on stderr.
- 3: a confirmation or a choice is needed. Run the command printed after `To do it, run:` (it holds
  `--confirm <step-id>@<digest>`, plus `--issue N` when bootstrap issues are listed).
- 4: the step named in `--confirm` is not the next step, the next step needs no confirmation, or the
  preview changed since the digest was made. Nothing changed.
- 130: interrupted.

A coding agent that runs these commands itself appends the fixed text `; echo "exit $?"` to each
`python3 scripts/bootstrap.py` command, so the exit code appears as an `exit <n>` line at the end
of the output. The agent tools show a non-zero exit as a failure, and exit codes 1, 3 and 4 are
normal outcomes (a stop at a step `start` does not perform, a preview waiting for your yes, a
changed preview). The engine prints no such line and its exit codes do not change; the agent never
puts the suffix on a command it shows you. The suffix assumes a POSIX shell (bash or zsh). If no
`exit <n>` line with a number appears, the agent does not retry the command: it stops and tells you
the shell is not bash or zsh and that the guide needs one of them.

### `--confirm`

```bash
python3 scripts/bootstrap.py start --no-input --confirm <step-id>@<digest> [--issue N] [init flags] [--review-notes] [--guided]
```

`--review-notes` (for the `pull_request` step; see [One bootstrap pull request](#one-bootstrap-pull-request))
adds the review notes, read from `bootstrap-review-notes.md` at the repository root, to the pull
request body after the `Closes #<issue>` line. It takes no value, and the confirming command
carries it when the preview had it. `--guided` makes the command do only the confirmed step and end by telling you to go back to your
coding agent; it requires `--confirm`. When the preview was made with `--full-dry-run`, the
confirming command that `start` prints carries it too.

`--confirm` takes the value of the `confirm:` line of a preview. The digest is the first 12 hex
characters of a SHA-256 over the step (for `bootstrap_issue` also the chosen issue number, or
`new`), the preview text, the content of the files the step will commit, the local `HEAD`, the
default branch's tip on GitHub, and the head of the open pull request or the bootstrap branch's
tip; for `first_sync` it covers the full dry run, with or without `--full-dry-run`. A confirmation
therefore only acts when the state still matches the preview that produced the digest; it does
not show that anyone saw it. A file, a flag value, a commit, a branch tip or the
target repository that changed since then gives exit 4. After the confirmed step `start`
continues as without `--confirm`, so without a terminal or with `--no-input` the next confirmation
exits 3 again.

The checks run in this order; the first one that applies decides, and nothing changes unless check 8
is reached:

1. The value is not `<step-id>@<12 hex characters>`: exit 2.
2. Bootstrap is complete (no next step): exit 0.
3. The step is not the next blocking step, or that step needs no confirmation: exit 4.
4. The step cannot be done on this run: exit 1, with the action printed. This includes the stops a
   step checks first: for `github_settings` after the merge, a local `project-context.toml` that
   differs from the default branch's (parsed values; comments and spacing do not count) or is
   missing, with the hint `git switch <default> && git pull --ff-only origin <default>` (with
   `git fetch origin <branch> && ` before a `git switch` to a branch that is only on GitHub); for
   `pull_request`, the stops listed [above](#guided-path-with-start) (no bootstrap branch, a merge
   conflict or deleted bootstrap file, a branch that is behind, not the pull request's head or
   missing its commits, and an `origin` that does not match); for `first_sync`, see below.
5. For `project_context`, an `init` flag is missing: exit 2, with a `needs:` line. This is checked
   in a terminal too, because `--confirm` never prompts. Check 5b follows it: a given flag value
   that fails the validation `init` uses (for example an empty `--description`, or one over 350
   characters) also exits 2.
6. For `bootstrap_issue`, the candidate: with several bootstrap issues and no `--issue`, or an
   `--issue` that is not one of them, `start` lists the candidates, each with its own `confirm:`
   line, and exits 3, also in a terminal. With no candidate, an `--issue N` is never ignored (that
   would create a duplicate): it exits 3 and asks you to rerun without it, also in a terminal.
7. The recomputed digest differs from the given one: exit 4.
8. Otherwise it acts.

### The `project_context` step and the `init` flags

`--description`, `--required-approvals` and `--project-number N` or `--create-board` are decisions,
so `start` never fills them in with a default. Without a terminal a plain run, or the run after a
confirmed step, stops with exit 1 and `Next step project_context needs your decisions`. `--owner`
is also required when the repository has no GitHub `origin` remote to take it from. `--name`
defaults to the repository's name, or to the checkout directory's name when there is no GitHub
`origin` remote. Without a terminal, or with `--no-input` or `--confirm`, a missing flag is named
before any preview (exit 1 with `To give them, run:` in a plain run, exit 2 with `--confirm`).
`--name` and `--description` must be a single line: control characters, line and paragraph
separators and every other format character (Unicode category Cf, bidi controls included) are
rejected; the zero-width joiner, the zero-width non-joiner and the soft hyphen stay valid.
`--description` is also limited to 350 characters (GitHub's repository description limit). In a
terminal `start` asks for the missing values before the preview (for the approval count it offers
0, which you accept or change), so the preview shows everything that will be written. The same
flags go to the preview run and to the confirming run; changing one in between gives exit 4.

### Which repository `start` uses

The repository is the one `gh repo view` reports in the checkout, else the `origin` remote. `start`
passes it as `--repo` to the `gh` commands it runs (`gh issue create`, `gh issue develop`,
`gh pr create`, `gh workflow run`, `gh run list`, `gh run view`), and `github --apply` uses it in
its API paths. Every preview of `bootstrap_issue`, `pull_request`, `first_sync` and
`github_settings` includes a `Repository: <owner>/<name>` line, so the digest binds the target
repository: if `gh` resolves to another repository between the preview and the confirmation, the
confirmation exits 4.
Git commands (`git fetch`, `git push`) use the `origin` remote, so three steps stop (exit 1, before
any issue, push, pull request or dispatch) when `origin` and the repository `gh` uses differ:

- `bootstrap_issue` and `pull_request` read `origin` the way the rest of the toolkit does
  (`git remote get-url origin`), so HTTPS URLs, `git@` and `ssh://` URLs and SSH host aliases such
  as `git@github-work:` all count. They stop when `origin` is not a GitHub repository (also when
  `origin` is missing or cannot be read) or names another one than `gh`; the fix is printed
  (`gh repo set-default <owner/name>`, or `git remote -v` to inspect `origin`). Nothing rewrites an
  alias. `pull_request` also reads every push URL (`git remote get-url --push --all origin`, the
  fetch URL when none is set) and stops unless each is a GitHub repository naming the same one as
  `gh`, since `git push` goes to all of them. `check --json` applies the same checks to both steps
  and reports `performable: false` with the same stop in its title and the same command.
- `first_sync` also needs the form the sync itself reads from `remote.origin.url`: only
  `https://github.com/<owner>/<repo>` or `git@github.com:<owner>/<repo>`. Any other form, an SSH
  alias or an `ssh://` URL included, stops with the title "not in a supported form". The fix is
  printed: `gh repo set-default <owner/name>` for a mismatch, or `git remote set-url origin <url>`
  (an SSH URL for an `ssh://` or `git@` origin, an HTTPS one otherwise).

### First roadmap sync: two previews

The guided and the manual path preview the first sync differently.

- **Guided (`start`):** the preview is a count summary of a local dry run of
  `python3 scripts/sync-roadmap-issues.py`, printed before the prompt or before exit 3: how many
  issues it will create (by kind, such as `Feature`, `SPEC` and `Phase 1`), sub-issue links, board
  items and other changes. Lines that explain a block (for example a skipped item) are kept in
  full. `start` with `--full-dry-run` adds the full dry run under the summary, and the flag is
  carried in the confirming command that `start` prints. The digest is computed from the full dry
  run, with or without the flag, so the same confirmation works for both and any change to the dry
  run exits 4.
  The dry run adds `--skip-project` when the board credential is not done (not configured,
  incomplete, or could not be checked); when it could not be checked at all, the preview also says
  that board changes are not previewed. A dry run that fails offers no apply: `start` prints its
  output in full and exits 1. Before it, `start` stops (exit 1) unless the checkout is exactly the
  default branch's tip with no uncommitted or untracked changes in `ROADMAP.md`, `specs/`,
  `scripts/`, `.github/workflows/`, `.github/actions/`, `project-context.toml` or `.specify/memory/constitution.md`
  (the dispatched workflow runs what is on GitHub, so the local dry run must read the same files),
  and unless the `origin` remote is a GitHub URL for the same repository `gh` uses (see
  [Which repository `start` uses](#which-repository-start-uses)). The fix is printed:
  `git switch <default> && git pull --ff-only origin <default>` for the checkout, and the commands
  in that section for `origin`. The local dry run uses your shell environment (for example
  `BOARD_GH_TOKEN`), so it can differ from the workflow run, which uses the repository's secrets.
- **Manual:** run the workflow's preview, read the log, then apply, as in
  [How the syncs behave](#how-the-syncs-behave).

The confirmed apply runs `gh workflow run sync-roadmap-issues.yml -f apply=true`. To find its run,
`start` lists the IDs of the sync's earlier manually dispatched runs of the logged-in user before
the dispatch, then polls for up to 60 seconds for a run with a higher ID than any of those, so it
never takes the push-triggered preview run and uses no clock comparison. It prints
`Run <id>: <url>`, then polls `gh run view` (each call is cut off so that a `gh` call that hangs
counts as still running) and prints `Still running (<m> min <s> s)…` at least every 30 seconds
until the run ends. If the run does not appear, or you interrupt `start` after the dispatch, it
says so and names the `gh run list` or `gh run watch` command; do not rerun `start` until the run
has finished.

### How far the confirmation can be trusted

`--confirm` guards against mistakes, not against a person or agent that holds the GitHub rights. It
cannot prove that a person said yes, and an agent in a pseudo-terminal can answer the prompt
itself. What protects the repository is who holds the rights:

- Commits reach the default branch only through the pull request, which the branch ruleset and the
  required reviews protect.
- The steps that change something without a pull request review (creating the issue or the board,
  `github --apply` and the sync apply) are protected only by who has those GitHub rights; the
  status report marks them `bypasses_review`.

Two more limits follow from how the writes run:

- `start` reads some state again while it writes: `pull_request` stages and commits the files it
  names, `github --apply` plans the settings again, and a dispatched workflow may start later and
  read the repository then. A change at exactly that moment was not in the preview and the digest
  cannot cover it.
- When the same user dispatches the sync workflow at the same time (another terminal, the Actions
  tab), `start` may watch the wrong run: it takes the newest run of that user whose ID is higher
  than any it listed before the dispatch.

### Step anchors

The `doc` of each step's `action` in `check --json` points to one of these headings:

- `prerequisites`: [Prerequisites](#prerequisites)
- `bootstrap_issue`: [Bootstrap issue](#bootstrap-issue)
- `project_context`: [`project-context.toml`](#project-contexttoml)
- `constitution`: [Constitution](#constitution)
- `roadmap`: [Roadmap](#roadmap)
- `github_settings` and `board_workflows`: [GitHub settings](#github-settings)
- `board_credential`: [Board credentials](#board-credentials)
- `pull_request` and `merge`: [One bootstrap pull request](#one-bootstrap-pull-request)
- `first_sync`: [How the syncs behave](#how-the-syncs-behave)

There are two exceptions. When `gh` is missing, too old or not logged in, every GitHub step that
could not be checked reuses the `prerequisites` action, so its `doc` is `#prerequisites`. And
`github_settings` points to `#project-contexttoml` when `project-context.toml` cannot be loaded.

### The toolkit repository itself

The toolkit's own repository has a `project-context.toml`, constitution and `ROADMAP.md` on its
default branch, so `check`, `check --json` and `start` read it as already bootstrapped. This is
expected: the guided path is for a repository created from the template.

## Guided path in your coding agent

You can also be guided in a conversation. In your coding agent, ask it to bootstrap the project
(for example "bootstrap this project" or "what is the next bootstrap step?"). The agent then uses
the [`bootstrap-guide`](../.agents/skills/bootstrap-guide/SKILL.md) skill. It works with every
coding agent the toolkit sets up (see [Coding agents](coding-agents.md#coding-agents)). If the agent
does not pick the skill from a free-text request, call it by name: `$bootstrap-guide` in Codex,
`/bootstrap-guide` in Copilot and Claude Code.

The skill keeps no list of steps. It reads where you are from `check --json` (see
[Machine-readable status](#machine-readable-status)), and it reads the state again after every
step. It explains each step from the section of this page that the step links to. At the start it
asks which mode you want. When no coding-agent choice is saved, it also asks which coding agents
you use and passes your answer with `start --no-input --agents`.

- **guide me**: you run each command that changes something. The agent shows it, explains it and
  reads the result. The agent still writes the review notes file, `bootstrap-review-notes.md`,
  itself, and its text is shown in the preview. For a step `start` performs, the agent shows the
  `start` preview unchanged after the bold line **Nothing has changed yet.**, and in the same
  message the confirming command, the one `start` prints after `To do it, run:`, with `--guided`
  added so that it does only that step and ends by telling you to go back to the agent. It puts
  the command after the bold line **If you approve, run this:**; running that command is your approval, with no separate
  yes. The agent shows the preview only up to the `To do it, run:` line and gives you exactly one
  command, so you never have to choose between commands; it mentions later steps only when the
  current one is done. No reply line follows a command run with `!`, because the agent continues
  by itself; for a command in a separate terminal, and for actions on GitHub's web pages, it asks
  you to reply "done".
- **do it for me**: the agent runs the commands that change something. For a step `start`
  performs, it first shows the `start` preview unchanged, says when the change skips pull request
  review, and asks for your yes. That yes also covers the one confirming command, which the agent
  shows with the preview, so there is no second yes for it. Every other command that changes
  something gets its own yes. A command that `start` only prints when it stops is yours to run in
  both modes, because it can discard or rewrite work, or make a commit (the confirming `start`
  command is not one of them: your yes to its preview covers it). So is any such command that
  `check` reports as a step's action, such as `git pull --rebase`, `git restore` or another
  `git pull`. The one exception is
  `git switch <default branch> && git pull --ff-only origin <default branch>`, when
  `check --json` reports exactly that command as the step's action, the default branch name uses
  only plain characters (ASCII letters, digits, `.`, `_`, `/` and `-`, not starting with `-`) and
  your working tree is clean: in this mode the agent offers to run it and runs it after your yes
  for that command. A fast-forward-only pull on a clean tree does not discard committed or tracked
  changes, but ignored local files (such as `.env`) at paths the default branch tracks can be
  overwritten.

The agent shows the preview in a code block in its own message. In "do it for me" mode it ends
that message with the approval question in bold; if your agent has a dialog for choices, it asks
the same question after that message. A preview that only appeared in tool output has not been
shown, and a yes to it does not count. The agent describes steps in plain words, never with
`check --json` field names or step states, and every question that waits for you, every reply line
and every safety warning is in bold. In Claude Code it shows a `python3 scripts/bootstrap.py` command with the `!`
prefix, which runs it in this session and puts its output in the chat; other agents show the plain
command for your terminal. Interactive commands such as `gh auth login`, `gh auth refresh` and
`gh secret set` are never shown with `!`: they need a terminal of their own (for `gh secret set`,
see [Board credentials](#board-credentials)). The `gh secret set` command comes from `check`, as
the `board_credential` step's action, and the command to run after merging comes from the `merge`
step's action; the agent shows them and never builds them itself. The commands the agent runs
itself end with
`; echo "exit $?"` (see [Terminal, `--no-input` and exit codes](#terminal---no-input-and-exit-codes)),
and it never puts that on a command it shows you. After each command that can change state, it
reads `check --json` again before it names the next step.

When bootstrap is complete, the agent starts its reply with `BOOTSTRAP IS COMPLETE`, as `start`
does.

The agent always runs the read-only commands itself (`check --json`, the `start --no-input`
preview and `git status --porcelain`). You can switch mode at any point. The mode only decides
who types a command. These decisions stay yours in both modes: the project description, the
repository visibility, the approval count, the constitution and the roadmap. The agent never runs
the `github --apply` command directly or any `--allow-weaken` command. The agent changes GitHub
settings only through the `start` step, after you have seen its preview and approved it (a yes, or
in "guide me" mode running the command). When the
approval count is still to decide, the choice and the action are yours. The review and merge of
the bootstrap pull request are yours too.

The constitution and the roadmap do not need to be final. A first version is enough (one roadmap
entry is fine): the constitution is versioned and can be amended, and `roadmap-write` changes the
roadmap at any time. If you are unsure, send the call as it is: the skill drafts a first version and
you accept or edit it.
The skill calls the agent gives you work when you send them unchanged, with no placeholder to fill
in; you can add your own decisions at the end of the call first. The constitution step ends with a
yes/no question about the version the skill wrote, with an offer to show the file; before it, the
agent summarizes what the draft decides (scope, approval count, sections by name, anything the
skill inferred). Your yes is what makes it yours. A constitution amended before the bootstrap pull
request keeps version 1.0.0, because no earlier version was ever committed.

## Checking progress

```bash
python3 scripts/bootstrap.py              # same as: bootstrap.py check
python3 scripts/bootstrap.py check --json # the machine-readable report
python3 scripts/bootstrap.py start        # work through the steps, with a preview and a confirmation each
```

The human `check` prints each step with `✓` done, `✗` to do or `–` optional or skipped, and the
next action; GitHub settings are skipped without a logged-in `gh`. It exits 0 when no step is `✗`,
else 1. For a coding agent or a script, use `check --json`, see
[Machine-readable status](#machine-readable-status). To be guided in a conversation, see
[Guided path in your coding agent](#guided-path-in-your-coding-agent). To let the toolkit do the
next step with you, use `start`, see [Guided path with start](#guided-path-with-start).

## Machine-readable status

```bash
python3 scripts/bootstrap.py check --json
```

This prints exactly one JSON document on stdout, for a coding agent or a script; diagnostics go to
stderr. The command is read-only: it changes nothing locally or on GitHub and does not fetch. Its
only local sync run is the first-sync dry run (`first_sync` `details.pending_changes`). A failing `gh` call never ends the
report early: the steps that need GitHub become `not_checked`.

The current `format_version` is `1`. It is an integer, raised when, and only when, a field is
removed or its meaning changes, or when a value is added to `state`, `actor` or `result`, or a step
is added. Adding fields, `details` keys or values inside `details` does not raise it. Callers must
ignore fields they do not know, and must stop when `format_version` is higher than the one they
support.

The report escapes the text that GitHub or the repository controls, so it can never be mistaken
for something else when it is shown: in each step's `title`, in the action's `text`, in every string
inside `details` and in `default_branch`, every control character, line or paragraph separator,
invisible or bidirectional formatting character and lone surrogate is written as a Python escape
(`\x1b`, `\u202e`), and every backslash is written as `\\`. Read `\x1b` as the characters
it names; never turn it back into the character, and never put such text into a command. The same
characters are escaped in the text that `check`, `start`, `github` and `init` print. A line feed inside a quoted value from GitHub, `gh` or git is shown as U+23CE (⏎), so the value stays on its line. A command is never
escaped, because an escaped command would not run as shown: when a value in it contains such a
character, `action.command` is `null` and `action.text` says that the command was left out; the
user works it out by hand after checking the names. This does not change the meaning of any
field, so `format_version` stays `1`.

### Example

A shortened report: a real one always lists all 11 steps.

```json
{
  "format_version": 1,
  "result": "incomplete",
  "next_step": "constitution",
  "bootstrap_merged": false,
  "default_branch": "main",
  "steps": [
    {
      "id": "project_context",
      "title": "project-context.toml is valid",
      "state": "done",
      "blocking": true,
      "actor": "engine",
      "needs_decision": true,
      "needs_confirmation": true,
      "performable": false,
      "bypasses_review": true,
      "action": null,
      "details": {}
    },
    {
      "id": "constitution",
      "title": "Constitution is missing (.specify/memory/constitution.md)",
      "state": "todo",
      "blocking": true,
      "actor": "user",
      "needs_decision": true,
      "needs_confirmation": false,
      "performable": false,
      "bypasses_review": false,
      "action": {
        "text": "Run the speckit-constitution skill, giving it the approval count (0).",
        "command": null,
        "doc": "docs/bootstrap.md#constitution"
      },
      "details": {"required_approvals": 0}
    },
    {
      "id": "board_workflows",
      "title": "Board workflow \"Pull request linked to issue\" is off",
      "state": "manual",
      "blocking": false,
      "actor": "user_web",
      "needs_decision": false,
      "needs_confirmation": false,
      "performable": false,
      "bypasses_review": false,
      "action": {
        "text": "Open the board's Workflows, enable \"Pull request linked to issue\" and set Status to In review.",
        "command": null,
        "doc": "docs/bootstrap.md#github-settings"
      },
      "details": {"workflows": {"Pull request linked to issue": false}}
    }
  ]
}
```

### Report fields

| Field              | Meaning                                                                                  |
| ------------------ | ---------------------------------------------------------------------------------------- |
| `format_version`   | The format version, see above.                                                           |
| `result`           | `complete`, `complete_with_manual_settings` or `incomplete`.                             |
| `next_step`        | The `id` of the first blocking step that is not `done`, or `null` when there is none.    |
| `bootstrap_merged` | Whether the bootstrap files are on the default branch, or `null` when it cannot be read. |
| `default_branch`   | The repository's default branch, or `null` when it cannot be read.                       |
| `steps`            | Every step listed below, always all 11, in that order. Step `id`s never change within a version. |

### Step fields

| Field                | Meaning                                                                                            |
| -------------------- | -------------------------------------------------------------------------------------------------- |
| `id`                 | The step's identifier, see the last table.                                                         |
| `title`              | A one-line description of the step's current situation.                                            |
| `state`              | `done`, `todo`, `manual`, `optional`, `waiting` or `not_checked`.                                  |
| `blocking`           | `true` when the step must be `done` before bootstrap is complete.                                  |
| `actor`              | Who acts: `engine`, `user` or `user_web`.                                                          |
| `needs_decision`     | `true` when the user decides, so a caller must let them.                                           |
| `needs_confirmation` | `true` when a caller must get the user's explicit yes before acting on the step.                   |
| `performable`        | `true` when `start --confirm` can do the step now. `start --confirm` acts only on `next_step`, and only when that step has `needs_confirmation: true` and `performable: true`; a caller asks for a yes only then. Other steps may show `performable: true` while an earlier blocking step is `next_step`. |
| `bypasses_review`    | `true` when the step changes something without a pull request review.                              |
| `action`             | `null`, or `{text, command, doc}`: what to do, a command to run as is from the repository root (or `null`, never with a secret), and the documentation anchor. |
| `details`            | Step-specific facts, see the last table. It never contains a secret value.                         |

### Steps and details keys

The steps are in report order. A `details` key is present only when it applies: for example
`ruleset_warning` appears only when there is a ruleset warning.

| Step id            | `details` keys                                                                                       |
| ------------------ | ---------------------------------------------------------------------------------------------------- |
| `prerequisites`    | `project_scope`: a string, one of `listed`, `missing` or `not_readable` (for example `not_readable` with `GH_TOKEN`); it does not block. `agent_tools`: the saved coding-agent choice, the known names `claude`, `copilot` and `codex` in that order, then any saved names the toolkit does not know, in saved order, without duplicates; or `null` when none is saved; it never changes the step's state. |
| `bootstrap_issue`  | `candidates`: a list of `{number, title, branch}`; `branch` is `null` when the issue has no branch.  |
| `project_context`  | `ruleset_warning`: a note when the ruleset could not be read as expected.                            |
| `constitution`     | `required_approvals`: the approval count to give the constitution skill, or `null` when `project-context.toml` cannot be read. |
| `roadmap`          | none                                                                                                 |
| `github_settings`  | `ruleset_warning`: a note when the ruleset could not be read as expected.                            |
| `board_credential` | `secrets`: the names of the board secrets that exist, never their values.                            |
| `board_workflows`  | `workflows`: board workflow name to `true`, `false` or `null` (not readable).                        |
| `pull_request`     | `pull_request`: `{number, branch}` of the open one; `earlier_pull_request`: `{number, state}` of an earlier one, `state` being `merged` or `closed`. |
| `merge`            | `pull_request`: `{number, branch}` of the open one; `failing_checks`: the names of failing checks.   |
| `first_sync`       | `pending_changes`: an integer, or `null` when the dry run did not run or failed; information only, it never changes a `state` or `result`. |

A `board_workflows` step that is `done` still carries a reminder in its title: `Board workflow "Pull
request linked to issue" is on; check that it sets Status to In review (the API cannot read it)`.
Its `action` is `null`, and the format version stays `1`.

### Exit codes

| Code | Meaning                                                                 |
| ---- | ----------------------------------------------------------------------- |
| 0    | `result` is `complete` or `complete_with_manual_settings`.              |
| 1    | `result` is `incomplete`.                                               |
| 2    | No report could be produced: a message on stderr and nothing on stdout. |

A `gh` that is missing or logged out does not give exit 2: the GitHub steps are `not_checked`, with
the reason in `title` and the fix in `action`, and `result` is `incomplete`.

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
| Git hooks do not run                                         | See [Troubleshooting: hooks don't run](local-setup.md#troubleshooting-hooks-dont-run).               |
| **Repository tools** fails in `AgentsIndexTest` (`scripts/test_agents_index.py`) | The "Where to find what" table in `AGENTS.md` is checked on every pull request: it needs the header `Question \| Where to look`, 10 to 16 rows, links that resolve to existing files, and anchors that match a heading; code-span paths must exist and must not be excluded from the export. A broken row, link or heading is named in the failure message; a wrong header or row count is reported with the rule it breaks. Fix the row, or update the target when you rename a heading or move a file. |
