# Scripts

## bootstrap.py

One entry point for setting up a repository created from the template; see
[`docs/bootstrap.md`](../docs/bootstrap.md). `check` (also what a plain
`bootstrap.py` runs), `init`, `github` and `start`:

```bash
python3 scripts/bootstrap.py                   # same as: check
python3 scripts/bootstrap.py check --json      # the machine-readable report
python3 scripts/bootstrap.py start             # guided: preview and confirm each step
python3 scripts/bootstrap.py start --no-input  # never prompt; exit 3 with a preview to confirm
python3 scripts/bootstrap.py start --no-input --agents claude,copilot,codex  # save the coding agents you use
python3 scripts/bootstrap.py start --no-input --confirm <step-id>@<digest> [--review-notes] [--guided]
python3 scripts/bootstrap.py init --description "What it does" --create-board
python3 scripts/bootstrap.py github            # preview
python3 scripts/bootstrap.py github --apply    # make the changes
python3 scripts/bootstrap.py github --check    # exit 1 if anything differs
python3 -m unittest scripts.test_bootstrap scripts.test_bootstrap_github scripts.test_bootstrap_check scripts.test_bootstrap_steps scripts.test_bootstrap_start
```

`check` (code in `bootstrap_check.py`) is a read-only status report: it prints each step with
`✓`, `✗` or `–` (optional or skipped) and the next action, and exits 0 when no step is `✗`, else 1.
The steps are a valid `project-context.toml`, the constitution, a valid `ROADMAP.md`, the GitHub
settings (the `github --check` logic; skipped with a note without `gh`, when `gh` is not logged in
or before `project-context.toml` is valid), the board credential (optional; a non-failing hint appears when reviews are required and only `PROJECT_BOARD_TOKEN` is set), and the agent files
of the selected tools, compared by content like `agent_setup.py sync` does. It writes nothing
locally or on GitHub.

`check --json` (code in `bootstrap_steps.py`) prints exactly one JSON document on stdout, with all
11 steps (`id`, `state`, `blocking`, `actor`, `needs_confirmation`, `performable`, `action`,
`details` and more), `result`, `next_step`, `default_branch`, `bootstrap_merged` and an integer
`format_version`. It never writes and never runs `git fetch`; a failing `gh` call makes the steps
that need GitHub `not_checked` instead of ending the report. Exit codes: 0 when `result` is
`complete` or `complete_with_manual_settings`, 1 when it is `incomplete`, 2 when no report could be
made (a message on stderr, nothing on stdout). The fields and every step's `details` keys are in
[Machine-readable status](../docs/bootstrap.md#machine-readable-status).

`start` (code in `bootstrap_start.py`, with the step catalogue in `bootstrap_steps.py`) works
through the steps from the first unfinished one. At the five steps it can do (`bootstrap_issue`,
`project_context`, `github_settings`, `pull_request`, `first_sync`) it prints a preview ending in
a step-name line (`Confirm "<fixed step label>":`) and `confirm: <step-id>@<digest>` (copy the
latter) and, in a terminal, asks `Proceed? [y/N]`. Without a terminal or with
`--no-input` it never prompts: it exits 3 after the preview and prints `To do it, run:` with the
full command (`--confirm <step-id>@<digest>`, plus `--issue N` when several bootstrap issues are
listed, and the same `init` flags: `--name`, `--description`, `--owner`, `--project-number` or
`--create-board`, `--required-approvals`). Progress and every other non-error output go to stdout
and only errors to stderr, and a performed step prints `Done: <step>: <result>.`. The
`first_sync` preview is a count summary of the dry run; `--full-dry-run` adds the full dry run and
is carried in the confirming command, and the digest covers the full dry run either way. Untracked
files do not stop `bootstrap_issue`; a changed tracked file does (commit, stash or revert it).
The optional board credential note is printed once, after `Done: github_settings: …`, and not when
`github_settings` was done earlier (`check` still reports it); the `github_settings` preview leaves
that line out, while `github` itself still prints it. Without a terminal, missing `init` flags at
`project_context` exit 1 with `Next step project_context needs your decisions` (exit 2 with
`--confirm`). `--guided`, which needs `--confirm`, does only the confirmed step and ends with
`Done. Go back to your coding agent.`; the coding agent adds it to the commands it shows you.
`--review-notes` (no value) adds the text of `bootstrap-review-notes.md` at the repository root,
which `.gitignore` ignores, to the pull request body of the `pull_request` step after
`Closes #<issue>`; the preview shows every notes line as `  | <line>`, so the digest covers the
shown text. A missing, empty, too long, unsafe, symbolic-link, non-regular or not-ignored file
exits 2 before anything is changed (too long means over 60 000 characters or 100 000 UTF-8 bytes;
the read is bounded), and so do notes with a credential pattern, a closing keyword followed by an
issue reference or issue URL (with or without a colon, `www.github.com` or angle brackets), or an
`@` mention anywhere in a line (an email address is not one), or a tab or carriage return; the message names the reason, never
the match. In `check --json`, the `merge`
step's waiting action carries the command `git switch <default> && git pull --ff-only origin
<default>`, and the not-configured `board_credential` action carries
`gh secret set PROJECT_BOARD_TOKEN -R <owner>/<repo>`; the guide skill shows these and never
builds them.

`--agents <tool>,…` (`claude`, `copilot`, `codex`) gives your choice of coding agents without
asking: `start` saves it as `agent-setup.tools` in the local Git config, generates those agents'
files and prints `Coding agents: <tools> (saved in git config agent-setup.tools).` and a line to run
`npm run setup-agents` again whenever you change coding agents. Without `--confirm` (without a
terminal or with `--no-input`) it then prints
`Go back to your coding agent and tell it the choice is saved.` and exits 0 without previewing the
next step; with `--confirm` it continues with the steps as usual. Without it, an interactive run
that is not `--guided` asks `Which coding agents do you use?` once, when no choice is saved (Enter
saves all three); with `--no-input` nothing is asked. There is
no preview or confirmation: it writes only local Git config and git-ignored files, and the step
previews and digests do not change. If generating the files fails (exit 2), the choice stays saved
and a rerun syncs again.

The digest binds the confirmation to the preview and the repository state
it was made from; a change since gives exit 4 and nothing is done. It stops with exit 1 at the
steps only a person can do (prerequisites, constitution, roadmap, merge) and prints the action. The
`bootstrap_issue`, `pull_request` and `first_sync` steps also stop when `origin` names another
repository than the one `gh` uses or is not a GitHub repository; `pull_request` also checks every
push URL (`git remote get-url --push --all origin`), and `check --json` reports both steps as not
performable in those cases. `first_sync` additionally needs the `origin` URL forms the sync itself
reads (`https://github.com/` or `git@github.com:`) and requires `ROADMAP.md`, `specs/`, `scripts/`,
`.github/workflows/`, `.github/actions/`, `project-context.toml` and `.specify/memory/constitution.md` to be committed;
`bootstrap_issue` and `pull_request` accept SSH aliases and `ssh://`. Every preview of
`bootstrap_issue`, `pull_request`, `first_sync` and `github_settings` includes a
`Repository: <owner>/<name>` line, so the digest binds the target repository. Branch names in
printed commands are shell-quoted. Exit codes: 0 complete, 1 stopped or declined (including end of input at the coding-agent question, which saves nothing), 2 error
(including a malformed `--confirm`, a missing `init` flag after `--confirm`, or `--guided` without
`--confirm`, or an unknown or empty `--agents` value), 3 confirmation or choice needed, 4
the preview or next step changed, 130 interrupted. The order of the `--confirm` checks, the trust
model (it guards against mistakes, not against someone who holds the GitHub rights) and the
first-sync preview are in
[Guided path with start](../docs/bootstrap.md#guided-path-with-start). The git and `gh` calls go
through the same injectable runner as `check` and `github`, `start` runs no write command before a
confirmation, and the tests (`test_bootstrap_start.py`, `test_bootstrap_steps.py`) use a fake that
never reaches GitHub.

`init` writes `project-context.toml`: owner and name default from the `origin` remote, it prompts
in a terminal, and with `--yes` or without a terminal it fails naming the missing flag. A board
number comes from `--project-number` or `--create-board` (`gh project create`). The result is
validated by `project_context.py` before an atomic write; an existing file needs `--force`.

`display_text.py` (standard library only) is the one definition of the characters that are unsafe
on a single output line: control characters, invisible and bidirectional formatting characters,
line and paragraph separators, lone surrogates. `bootstrap.py`, `start`, `github` and the
`check --json` report escape them with `display_text.printable` (`\x1b`, `\u202e`; a backslash
becomes `\\`, so an escape cannot be faked), once, where text GitHub or git controls is printed.
A command is never escaped: one that would contain such a character is left out. `init` refuses
control characters, line and paragraph separators and every other format character (Unicode
category Cf: the bidi controls and marks, zero-width space, word joiner, byte order mark, tag
characters) in `--name` and `--description`; only the zero-width joiner U+200D (emoji sequences),
the zero-width non-joiner U+200C and the soft hyphen U+00AD stay valid. These are the user's own
values and are repeated unchanged, shell-quoted, in the confirming command. Messages are built from
raw values and escaped once, where they are printed. A line feed inside a quoted value from GitHub,
`gh` or git is shown as U+23CE (⏎), so the value stays on its line. `toml_string` writes every unsafe character as
a `\uXXXX` escape (`\UXXXXXXXX` above U+FFFF).

`github` (code in `bootstrap_github.py`) previews by default, one line per change grouped as
Labels, Board, Ruleset and Actions; `--apply` makes them and `--check` changes nothing and exits 1 on drift.
`--allow-weaken` lets `--apply` lower the approval count or turn off last-push approval of an existing ruleset; without it those changes only print a `Warning:`. Stale-review dismissal is never turned off: it is turned on with 2 or more approvals and otherwise left as it is. A `Note:` is printed when the existing ruleset has `bypass_actors`, because anyone or any App listed there can merge around the review requirement.
Exit codes: 0 nothing left to do or applied, 1 drift under `--check`, 2 error. Secrets and
variables are only reported, never set. All `gh` calls go through one injectable runner; the tests
use a fake and never reach GitHub. What it manages:

- **Labels:** `Epic`, `Story`, `Planning`, `needs-info`, `abandoned` and the human-only override
  labels `combined-spec-pr-approved` and `prerequisites-acknowledged` (the list is `LABELS`).
  Missing labels are created; a label whose color or description differs is updated.
- **Board** (`github_project.owner` and `number`, organization or personal): the `Status` options
  `Backlog`, `Planned`, `In progress`, `In review` and `Done`, the number field `Roadmap order`, and
  the link between the board and the repository. Existing options keep their ids. A fresh board has
  `Todo`, `In Progress` and `Done`: names match case-insensitively, so `In Progress` becomes
  `In progress`, and `Todo` becomes `Backlog` when there is no `Backlog` (otherwise `Todo` stays and
  is listed as unmanaged). Missing options are appended, the managed five come first and unmanaged
  ones follow; a board that already has all five is never reordered. A board that does not exist is
  an error (create one with `gh project create --owner <owner> --title <name>`, both shell-quoted
  as needed, and set `github_project.number`); so is a `Roadmap order` field of another type.
- **Actions** fork pull request workflow approval (`GET`/`PUT repos/{owner}/{repo}/actions/permissions/fork-pr-contributor-approval`): raised to `all_external_contributors` (preview line `Actions: fork PR approval first_time_contributors → all_external_contributors`). It is never lowered: a policy that is already that, or one this tool does not know, is left alone (an unknown one prints a `Note:`). Private repositories have no such policy (GitHub answers 422); they are skipped with a `Note:`. Reading or writing it needs repository admin rights; a 403 or 404 on a public repository says so.
- **Ruleset** `Default branch protection` on the default branch, created when no repository ruleset
  has that name: active, no branch deletion, no force pushes, a pull request with
  `required_approvals` approvals (approval of the last push when that is 1 or more, and dismissal of
  stale approvals on push when it is 2 or more), and the
  required checks `Spec and plan merge before tasks`, `Validate branch and link issue`,
  `Repository tools` and `Roadmap prerequisites` from GitHub Actions. When it exists, only these
  values change; every other rule and setting (Copilot code review, bypass actors) is kept. With
  `required_approvals = 0` the pull request rule stays, so a solo project still uses pull requests
  but needs no approval; GitHub's "Require an additional approval for unattributed Copilot pull
  requests" only applies to a non-zero count. Organization rulesets are ignored; a ruleset under
  another name that also protects the default branch stays untouched and overlaps (the strictest
  rule wins), so remove duplicates by hand. Writing needs repository admin rights, and rulesets need
  a public repository or a plan that supports them; a 403 or 404 names the likely cause.
  `--apply` never lowers the approval count or last-push approval of an
  existing ruleset unless `--allow-weaken` is given: it prints a `Warning:` and leaves the setting,
  and `--check` exits 1.

## project_context.py

Validates the non-secret settings in [`project-context.toml`](../project-context.toml)
and prints them as JSON:

```bash
python3 scripts/project_context.py
python3 -m unittest scripts.test_project_context
```

The description is one line of at most 350 characters (GitHub's repository description
limit), because the bootstrap guide passes it unchanged to skills.

Update the project name and description when reusing the agent instructions, and
set the GitHub Projects owner and number to the intended board. The
[`project-context.example.toml`](../project-context.example.toml) file provides
valid placeholder values; replace them before using it. The
`github-project-tasks` skill offers these as defaults but still confirms the
board and honors values explicitly supplied by the user. This file is not a
secret store and does not override the constitution, workflow gates, GitHub
CODEOWNERS, or Spec Kit configuration. `--config PATH` validates another file.
Missing or invalid settings cause an error rather than selecting a default board.
`project.name` and `project.description` must be a non-empty single line without
control characters, separators and format characters such as bidi controls, except the zero-width
joiner and non-joiner and the soft hyphen (an existing multi-line or tab-containing value is
rejected).
The project-status workflow validates this file before creating its App token.
`--board-owner-or-skip` (with `--repository-owner`, reading `HAS_BOARD_APP` and `HAS_BOARD_PAT`) is what the board workflows' shared "Read configured Project owner" step (in the local composite action `.github/actions/board-token`) calls: it prints `github_project.owner` on stdout, or an empty owner and a skip notice on stderr when no board credential is configured or the repository is not initialized yet. Once initialized, a missing or invalid file, or an owner that differs from the repository owner, is an error.
`sync-project-status.py` uses `github_project.owner` and `github_project.number`
for any board options not explicitly supplied on the command line.

The `[review]` section sets `required_approvals`, the independent approvals a
pull request needs: a non-negative integer, `0` for a solo project. It is required and has no
implicit default: a missing section or key fails validation and nothing falls back to 0 or 1.
`bootstrap.py init` proposes 0 for a new project; the constitution decides the number. See
"Upgrading an existing repository" in `docs/bootstrap.md` for adding it to an existing context.
`python3 scripts/project_context.py --required-approvals` prints the value.
`bootstrap.py github` applies the value to the branch ruleset, and the constitution's approval rule
should state the same number (give it to the `speckit-constitution` skill). The JSON output
always contains `review`.

### Not-yet-initialized state

`python3 scripts/project_context.py --bootstrap-state` prints `uninitialized` or
`initialized`. This is the single definition CI and the roadmap scripts share. The
marker files are `project-context.toml`, `ROADMAP.md`,
`.specify/memory/constitution.md` and any feature directory under `specs/`. A
repository is **uninitialized** only while none of them exists, which is what a
fresh repository created from the template looks like. Any one of them makes it **initialized**, so a
half-initialized repository (for example a `specs/` directory without
`ROADMAP.md`) fails the roadmap checks with an error naming the missing
`ROADMAP.md` and the step that creates it, and a missing `project-context.toml`
fails board sync when board credentials are configured; deleting `ROADMAP.md` or `specs/` later can never switch the
checks off once the constitution or `project-context.toml` exists. `--root DIR`
inspects another checkout; a `DIR` that is not a repository root (no
`scripts/project_context.py`) is an error, never "uninitialized". Plain
`project_context.py` without a `project-context.toml` also fails with a message
telling you to run `python3 scripts/bootstrap.py init`. The bootstrap steps are in
`docs/bootstrap.md`.

Project-specific scope, technology and quality requirements live in the
[constitution](../.specify/memory/constitution.md), not in a second copy in
`project-context.toml`. Reusable agent instructions refer to that source when
applying those requirements; changing the context file does not weaken the
constitution or existing review and workflow gates.

## Repositories created from the template

A new project is created from the
[jekelwork/spec-toolkit-template](https://github.com/jekelwork/spec-toolkit-template) template (see
[`docs/bootstrap.md`](../docs/bootstrap.md)). The template is kept in the not-yet-initialized
state (see [`project_context.py`](#not-yet-initialized-state)): it has no `ROADMAP.md`, `specs/`,
`project-context.toml` or constitution, so every new repository starts there too. Its `AGENTS.md`
and the docs under `docs/` still link to those files, which the new project creates during bootstrap.
Until then `check_roadmap.py`, `roadmap_status.py` and `sync-roadmap-issues.py` print
`Not initialized yet: no ROADMAP.md. See docs/bootstrap.md.` and exit 0, and the Toolkit workflow
skips the roadmap check with a `::notice::`.

The board workflows skip with a `::notice::` instead of failing while
the repository is not initialized (no `project-context.toml` yet) or no board
credential (App secrets or `PROJECT_BOARD_TOKEN`) is configured (`sync-project-status.yml`, and the board part of `sync-roadmap-issues.yml` and
`sync-phase-issues.yml`); once the repository is initialized, a missing
`project-context.toml` fails them with "Missing project context". The first bootstrap pull request needs an issue-backed
branch such as `<issue>-chore-bootstrap` with a `CHORE:` issue title; the
roadmap gate only checks Phase branches, so it passes there.

## Spec Kit workflow scripts

The Spec Kit plan setup, task setup, prerequisite check and feature creation
scripts in `.specify/scripts/bash/` are generated by Spec Kit and used
unmodified. `get_feature_paths` prints each value quoted with `printf '%q'`, and
the callers `eval` that output; feature creation reads arguments with `${!i}`.
This is upstream's design, and the team accepts it. Its safety rests on the
`%q` quoting, so the tests guard behaviour rather than keywords. They run the
real scripts with hostile names (`$(...)`, backticks, `;`, quotes, spaces,
newlines; defined in `scripts/speckit_hostile_names.py`), assert that no marker
file is created and that the output returns the exact name:

- `setup-plan.sh --json` (persisting): hostile feature directory and branch;
  `.specify/feature.json` must round-trip the relative path.
- `setup-tasks.sh --json` (persisting): hostile feature directory, with the
  `feature.json` round-trip.
- `check-prerequisites.sh --json` with `--require-spec --require-tasks`
  (persisting): the same.
- `check-prerequisites.sh --paths-only --json` (no persistence): hostile feature
  directory and hostile `SPECIFY_FEATURE` branch.
- `create-new-feature.sh --json --dry-run`: hostile `--short-name`,
  `--number`, description, empty and option-like values; the `BRANCH_NAME` must
  be sanitized or the input cleanly rejected.

For every eval caller above there is a mutation test that runs a copy of
`common.sh` with `%q` replaced by naive double quoting and asserts the marker
appears, which shows the guard can fail. Run the tests with
`python3 -m unittest scripts.test_setup_plan scripts.test_speckit_workflow_scripts`.

Never edit these generated scripts by hand; change them only through
`scripts/specify.sh` (see [Spec Kit CLI and version](../docs/coding-agents.md#spec-kit-cli-and-version)).

## check_roadmap.py

Validates the root, plan-only `ROADMAP.md`: entry heading and field shape,
unique three-digit IDs, the five plan statuses, required fields, dependency
references and cycles, and `Spec dir` paths. It also calls
`check_roadmap_spec_ids.py` to verify spec directory IDs, that each `Spec dir`
already contains `spec.md`, and the spec `Input` references.

```bash
python3 scripts/check_roadmap.py
python3 -m unittest discover -s scripts -p 'test_check_roadmap.py'
```

The checker is read-only. Put extra context in `Notes`; unknown fields are
rejected. In an uninitialized repository (see
[`project_context.py`](#not-yet-initialized-state)) it prints one line pointing to
`docs/bootstrap.md` and exits 0; once the repository is initialized, a missing
`ROADMAP.md` exits 1 with a message naming the `roadmap-write` skill. The
Toolkit workflow makes the same decision with `--bootstrap-state`.

## roadmap_gate.py

The `Roadmap prerequisites` job in
[`.github/workflows/roadmap-gate.yml`](../.github/workflows/roadmap-gate.yml)
runs on Phase implementation pull requests. It parses the trusted event's
branch, follows the issue's parent chain to its roadmap Epic, then invokes
`roadmap_status.py --id NNN --json`. A missing parent, malformed or ambiguous
identity, failed API read, or invalid status data fails closed. Only issues
with the exact existing maintainer-controlled `Epic` label establish roadmap
marker identity; malformed
label metadata and labelled duplicate Epics fail closed. This boundary does
not protect against a malicious trusted triager or issue editor; authorship
and `author_association` are not identity credentials.

The `prerequisites-acknowledged` label can waive known blockers only; it cannot
bypass any identity or data errors and must be applied by a human. Each gate run
fetches current PR labels from GitHub with pagination, never event labels. When
the label is present and the status is blocked, the gate needs evidence that a
human user applied it. The PR's issue events API (read with pagination) is
authoritative: the gate picks the latest `labeled` event for the label by
`(created_at, id)` and accepts it only if its actor is a `User` and it has no
`performed_via_github_app`, whatever the event payload says. Only while the
events API does not yet show any `labeled` event for the label (it is
eventually consistent) does the gate accept the run's own payload instead,
when the run was triggered by a user applying the label (`action: labeled` for
this label, `sender.type: User`). An events API error fails the gate; it never
falls back to the payload. Labels whose application by a human user cannot be
verified (bot account, applied through a GitHub App, or an undeterminable
event, timestamp or actor) are rejected and the gate stays blocked with a
message saying why; if it says the actor cannot be verified, re-run the job.
No check can tell an agent acting through a user's token from that
user, so the human-only rule still applies to agents in that case. A waived run lists the waived blocker IDs in the decision's
`blockers` field and prints `waived by prerequisites-acknowledged: <IDs>`.
While present, the label waives the current blockers: humans must remove it
and reconsider/reconfirm the exact blocker set and conditions whenever they
change, before reapplying it. Code does not bind it to a recorded blocker set.

Only Phase branches are gated. Spec branches and the other issue branch types
(`chore`, `ci`, `docs`, `fix`, `refactor`, `spec`, `test`) pass without roadmap
reads. Phase-only
attribution beyond that is a human review boundary, not code-path ownership
enforcement: reviewers must ensure implementation uses its proper Phase branch
and issue rather than a passing spec/chore branch. The workflow
executes only the exact trusted base commit; it never executes PR-head code.
The branch/base event snapshot and live API reads are not atomic and may become
stale. Prerequisite Epic reopen/close or other Epic changes do not retrigger this
PR-only workflow. Rerun the gate before merge whenever prerequisites, blockers,
or acknowledgement change; label add/remove and PR synchronize events also run
it, but an old passing result is not fresh evidence after external changes.

Offline behavioral tests run with the Repository tools suite:

```bash
python3 -m unittest discover -s scripts -p 'test_roadmap_gate.py'
```

After the workflow is merged, a repository admin must create that label and
add the **Roadmap prerequisites** job to the `main` ruleset's required checks.
Those GitHub settings are intentionally not changed by this repository tool.

## roadmap_status.py

Reads progress from GitHub without making changes. The default table and JSON list keep
roadmap file order, with ID, title, plan status, Epic number/state/reason, optional Project
Status, issue URL and unsatisfied prerequisite IDs. `--id` produces the dependency
evaluation object with the same keys as the historical file-based checker:

```bash
python3 scripts/roadmap_status.py
python3 scripts/roadmap_status.py --json
python3 scripts/roadmap_status.py --id 005 --json
python3 scripts/roadmap_status.py --skip-project
python3 -m unittest discover -s scripts -p 'test_roadmap*.py'
```

For `--id`, exit 0 means the ordinary completion baseline is satisfied, exit 1 reports
unfinished prerequisites, and exit 2 reports invalid/unreadable data or failed required
GitHub reads. Without `--id`, exit 0 means the listing succeeded, not that every entry is
ready. In an uninitialized repository the listing prints the not-initialized line and
exits 0 (`--json` prints `[]` and the line on stderr). `--id` stays exit 2 there, and an
initialized repository without `ROADMAP.md` is exit 2 too: an ID cannot exist, so no gate or
guard may read "ready". `sync-roadmap-issues.py` follows the same rule (exit 0 with the line
when uninitialized, a clear error otherwise). Only a unique roadmap Epic closed as completed satisfies an
ordinary prerequisite; marker identity requires the exact maintainer-controlled `Epic`
label. Unlabelled marker copies warn and are ignored;
invalid label metadata or labelled duplicate identities fail closed. Missing/open Epics
and closure as not planned do not satisfy dependencies. Project Status,
plan statuses and historical notes never establish completion.

The board scripts read the board token from `BOARD_GH_TOKEN`; without it they use `gh`'s own login
(`GH_TOKEN` or `gh auth login`). In CI it is the GitHub App token, or `PROJECT_BOARD_TOKEN` when no App is
configured; locally it is your own `gh` login. Issue writes use `GH_TOKEN` / `GITHUB_TOKEN`.

Authenticate `gh` with repository issue-read access. Board Status is optional: configure
the Project in `project-context.toml` and grant read access (or supply `BOARD_GH_TOKEN`).
Unavailable board reads or missing/invalid board configuration produce an explicit warning;
the listing still reports required issue data. `--skip-project` omits board configuration and reads.
No board access is needed for `--id`. That mode resolves only the requested Epic and its direct
prerequisites, so unrelated bad Epic records cannot block it; the whole dependency graph is still
validated. Planning markers do not determine Epic completion. Missing Epic "blocked by"
links do not waive prerequisites. Duplicate markers fail rather than choosing an Epic.

`check_roadmap_dependencies.py` remains a historical file-status checker and shared graph
parser/evaluator for synthetic tests. It is not the start-work guard: use `roadmap_status.py`.
The live command accepts only the five plan statuses.
The ledger must have exactly one `## Planned Specs` section with numbered
entries and `- **Depends on:**` fields containing `none` or comma-separated
IDs. Commented-out and fenced examples do not count; real entry headings
outside Planned Specs are rejected.
The checker rejects raw HTML and unclosed or multiline inline-code spans to
avoid mistaking examples for live entries; use Markdown links, single-line
code spans, or fenced examples instead. Use plain-text field labels rather
than linked labels. Nonstandard dependency-like fields and other structured
fields with three-digit values that could be roadmap IDs are ambiguous and
must be resolved rather than ignored.
`--roadmap PATH` is for synthetic fixtures, not a way to bypass the real
roadmap. The script reports entry notes but cannot interpret them or verify
schema milestones. A caller must follow
[`roadmap-dependency-check`](../.agents/skills/roadmap-dependency-check/SKILL.md),
including reviewing notes and evidence even after exit 0. There is no force
flag and nothing is persisted. Roadmap issue synchronization mirrors the
ledger; it does not start feature work or bypass this dependency guard.

Agent-level acceptance scenarios:

- **Unblocked specification:** for a synthetic planned entry with
  `Depends on: none`, `roadmap-entry-check` permits specification to continue through the
  normal review gates.
- **Specification with blockers:** ask to specify planned entry 005 while 014
  is incomplete. Require the explicit `specs/005-<slug>` directory and planned
  identity, warn about 014, and continue without a blocker acknowledgement.
  Identity, graph, and read errors still stop the entry check.
- **Planning and resume:** clarify, plan, tasks, analyze, and converge do not
  run the blocking dependency gate. Existing review and merge gates still apply.
- **Blocked implementation:** report all unfinished prerequisites and stop
  before implementation or its Phase/implementation branch, agents, or Phase
  In Progress transition. “Continue” is insufficient; “I understand 005 is
  blocked by 014; proceed with implementing Phase 1 of 005 anyway” authorizes
  only that work after recheck. On implementation resume or changed blockers,
  require the exact-set acknowledgement rules in the dependency skill.
- **Direct task or phase:** resolve its linked issue and spec to the roadmap
  entry before creating a branch, updating the board, or launching an agent.
  The same stop, acknowledgement, recheck, and resume rules apply without a
  Spec Kit hook.
- **Narrow milestone:** for an entry 020 whose roadmap notes allow work
  against mocks once 010's schema is merged, verify that the schema really
  merged to main before treating that dependency as satisfied; still report
  any other unmet dependencies. Missing or conflicting evidence never counts
  as approval. A demonstrably absent schema is an unmet condition; unavailable
  or ambiguous evidence is an error that cannot be overridden.
- **Invalid graph:** an unknown ID, missing dependency field, or cycle is an
  error, not a blocker that the user can waive. Resolve the data first.

## check-secrets.mjs

Scans files passed as arguments for credential-shaped values. The pre-commit hook
passes staged files, including `.env.example` files. OpenAI-style detection covers
legacy, project (`sk-proj-`), and service-account (`sk-svcacct-`) keys, including
underscores and hyphens in key material. Use placeholders in examples or mark
deliberate fixtures with `secret-scan:ignore` on the same line.

Run the regression tests (also run by the Repository tools CI job):

```bash
node --test scripts/check-secrets.test.mjs
```

## sync-roadmap-issues.py

Synchronizes `ROADMAP.md` entries with GitHub issues. It runs in dry-run mode by default and changes nothing unless `--apply` is passed:

```bash
python3 scripts/sync-roadmap-issues.py
python3 scripts/sync-roadmap-issues.py --id 008
python3 scripts/sync-roadmap-issues.py --apply --skip-project
python3 scripts/sync-roadmap-issues.py --apply-if-synced   # what a push to main runs
```

- `--id NNN` limits issue changes to one canonical roadmap ID; otherwise entries are processed in file order. Roadmap and planning issues are found by their markers, not their titles.
- The Epic body lists the entry's Description, Outcome, Scope (in), Scope (out), Depends on, Governed by and Notes, in that order, when present. Following lines (indented or not) continue the field and are joined with single spaces, as in the roadmap validator; blank lines are skipped and only a new field or a heading ends the field. Fenced code blocks, including their content, are dropped from the Epic body, and nested bullets and line breaks inside a field are flattened into one line, so keep roadmap fields to plain prose.
- `planned` creates or updates an Epic, the `SPEC: <ID> Create and approve spec and plan` planning issue and one provisional `Phase 1: Kickoff — <Epic name>` Story (stable `phase-1` marker). Both are sub-issues of the Epic, and Phase 1 is blocked by the spec-and-plan issue. When the spec-and-plan issue closes as completed, only that blocker is cleared (never re-added; other blockers are preserved). There is no tasks planning issue. Once `specs/<feature>/tasks.md` exists, roadmap sync stops updating Phase 1 and leaves it to the Phase sync.
- `needs-info` keeps the Epic and labels it `needs-info`; any existing planning and Phase issues are closed as not planned. Returning to `planned` creates missing planning issues and reopens the same paused Phase issues. `deferred` closes open Epic, planning and Phase issues as not planned; returning to `planned` reopens those same Phase issues. `abandoned` and removed entries are closed as not planned and labeled `abandoned`; their Phase issues are retired and never reopened automatically. Closing an entry is blocked while any Phase is In progress, In review or Done, so resolve that work explicitly first. `undecided` creates no issues. The roadmap sync does not reopen a completed Epic; the Phase sync reopens it if checkbox changes make Phase work incomplete again.
- Dependencies are synchronized as GitHub "blocked by" links between marked roadmap Epics. Only stale links between those epics are removed; unrelated links are preserved.
- Only trusted issues are matched by marker, for lookups, removals and dependencies alike: the author is a bot (any login ending in `[bot]`, so every App installed on the repository), or the `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`, or the issue carries an `Epic`, `Story` or `Planning` label (setting a label needs triage rights, so legitimate issues of a departed author or a private member reported as `CONTRIBUTOR` still match and no duplicate Epic is created). Other marker copies are ignored with a warning and are never closed, labelled or commented on.
- `--report-sync-state` prints `Roadmap Epic exists: yes|no` and changes nothing; the pull request preview uses it to say whether a merge will apply.
- `--apply-if-synced` applies only when a trusted roadmap Epic marker already exists; otherwise it prints that the first roadmap sync is a manual, reviewed step and stays a dry run. The workflow passes it on pushes to the default branch unless the repository variable `ROADMAP_SYNC_AUTO_APPLY` is `false` in any case; any value other than `true` or `false` fails the push run.
- Each created issue is fetched by number right after creation (the listing can lag) and merged into the reloaded list, so the run does not depend on the listing. As a fallback the script waits for the listing with a capped backoff of about a minute (2, 4, 8, then 12 seconds); `sync-task-issues.py` waits the same way. If they still are not visible it exits saying they exist and a rerun finishes the links.
- `--skip-project` skips Project updates. Otherwise the script uses `github_project.owner` and `github_project.number` from [`project-context.toml`](../project-context.toml), adds roadmapped Epics, planning issues and the provisional Phase 1, sets the Epic's one-based `Roadmap order` field, and sets Phase 1 to `Planned` when its status is unset or `Backlog`. `--project-owner` and `--project-number` override the corresponding settings. A missing field is reported without failing the sync. Adding an issue to the board is idempotent: when GitHub answers that the content already exists in the project (its built-in "Auto-add sub-issues to project" workflow adds sub-issues once their Epic is on the board), the existing item is used. The sync looks it up again a few times; if it is still not visible it stops with `... is already in project ..., but it is not visible yet; rerun the sync`, so rerun it. Any other refusal fails with GitHub's message.
- GitHub rate-limit 403/429 responses are retried with a 60-second pause; other 403 errors fail immediately. Issue creations are spaced briefly to avoid secondary limits.

The workflow uses `GITHUB_TOKEN` for repository issue operations. Board updates use a board credential: the `PROJECT_STATUS_APP_ID` and `PROJECT_BOARD_APP_PRIVATE_KEY` secrets (a scoped App token that reads repository issues and writes to organization Projects) or `PROJECT_BOARD_TOKEN` (see [Board credentials](../docs/bootstrap.md#board-credentials)). If neither is configured, CI passes `--skip-project`. A push to the default branch applies once a roadmap Epic exists (see `--apply-if-synced`), unless `ROADMAP_SYNC_AUTO_APPLY` is `false` (applying runs use the `issue-sync-roadmap` concurrency group, apart from the Phase sync's `issue-sync`); scheduled runs are read-only previews, and manual runs apply only with **Apply changes**. Closures apply like every other change. The **Preview Roadmap Sync** workflow dry-runs pull requests that change the watched files with the base branch's scripts on the merge commit's `ROADMAP.md` and `specs/` (read-only token, no secrets, `--skip-project`; symbolic links are rejected, and the summary notes any changed file outside those paths; review `.github/` and `scripts/` changes of same-repository pull requests, which run their own copy of the workflow), writes a summary built by `roadmap_sync_summary.py` to the job summary, and creates or updates one pull request comment, which the comment job marks with a hidden `<!-- roadmap-sync-preview -->` and fills with sanitized text, for same-repository pull requests. Before approving the first `--apply`, compare every proposed create, update, close, reopen and relationship change with `ROADMAP.md` and the existing GitHub issues. Confirm that only the intended managed Epics, planning issues and provisional Phase 1 issues are changed. Then approve a manual apply run, verify the resulting issue hierarchy and rerun the dry run. Before the first apply, create the labels with `python3 scripts/bootstrap.py github --apply` after obtaining settings approval.
If an applying roadmap sync is cancelled, rerun **Sync Roadmap to GitHub Issues** manually on `main` with `apply` enabled.

## sync-task-issues.py

Synchronizes the Phases of a Spec Kit feature's `tasks.md` with GitHub: each Phase is one `Story` issue under the roadmap Epic. **No issues are created for `T###` tasks.** Task IDs stay stable in `tasks.md`, and inline "(depends on T###)" text only orders implementation. The workflow passes `--roadmap-only`, so it skips feature directories without a matching entry in `ROADMAP.md`. When applying or checking it requires the roadmap-managed Epic; previews warn and skip a missing one. A daily run previews those features and applies only when the repository variable `PHASE_SYNC_AUTO_APPLY` is explicitly set to `true`.

```bash
gh auth login                                                        # once
python3 scripts/sync-task-issues.py --roadmap-only                   # dry run for roadmap-managed features
python3 scripts/sync-task-issues.py --roadmap-only --apply           # apply to roadmap-managed features
python3 scripts/sync-task-issues.py --roadmap-only --feature specs/008-… --apply
python3 scripts/sync-task-issues.py --roadmap-only --feature specs/008-… --check
python3 scripts/sync-task-issues.py --roadmap-only --apply --skip-project
```

- Roadmap Epics: the roadmap ID is the feature directory's prefix (`specs/008-…` → `008`). A directory without one is skipped with `--roadmap-only` and otherwise stops the run, in both cases before any GitHub read. The Epic created by `sync-roadmap-issues.py` is the feature's parent issue. This script finds it by its `speckit-roadmap: <ID>` marker alone and does not consult labels, so every issue carrying the marker counts and a copy stops the run as a duplicate (the read-only gate and `roadmap_status.py` instead ignore copies without the `Epic` label). The Epic's body is regenerated from `ROADMAP.md` with artifact links. Its spec-plan planning issue is never edited, re-parented or closed here. This script never creates an Epic. When the Epic does not exist yet, a `--roadmap-only` preview warns and skips the feature (an Actions warning annotation in CI); `--apply`, `--check` and runs without `--roadmap-only` stop and ask you to run `python3 scripts/sync-roadmap-issues.py --id <ID> --apply` first. Duplicate Epics stop the run with their numbers.
- The Phase issue title and generated body follow `tasks.md` (heading `Phase N: Name` and the task checklist). When `tasks.md` first appears, the provisional Phase 1 created by roadmap sync (marker `phase-1`) is updated in place and the other Phases are created as Epic children. All open Phase issues are added to the Project as `Planned` when their status is unset or `Backlog`; later statuses are preserved.
- Phase numbers are immutable identities, matched by the `speckit-phase: <feature>#phase-N` marker. A Phase closed as not planned is retired and its number cannot be reused: `tasks.md` defining it again stops the run. Roadmap-deferred/needs-info phases carry a pause marker and may be reopened only when the roadmap entry returns to `planned`. If such a Phase is removed from `tasks.md` while paused, it becomes retired.
- A Phase deleted from `tasks.md` while still `Planned` is closed as not planned with a comment and does not block the Epic. A deleted Phase that is In progress (phase branch exists), In review (open PR) or Done (closed as completed) stops the run with an actionable message and no changes: restore the Phase in `tasks.md` or resolve the branch/PR/issue by hand.
- A retired Phase (closed as not planned, not paused) is removed from the Project through GraphQL with the board credential, on the run that retires it or any later run that still finds it on the board; a dry run prints `remove #N from project ...`, `--skip-project` leaves the board alone, and `sync-project-status.py` removes such a Phase instead of setting a Status. The issue stays closed as not planned; reopening it by hand adds it back as `Planned`.
- Checkboxes govern completion: a Phase closes as completed when all its tasks are checked (a Phase with no tasks is never complete), and unchecked or new tasks reopen a completed Phase. The Epic closes only when the spec-plan issue is closed as completed and every currently defined Phase is complete; it reopens automatically if incomplete Phase work returns. Progress is recorded on GitHub, not in roadmap statuses. When a run closes or reopens a Phase and the Project is not skipped, it also sets that Phase's board Status with the status sync's rules (closed: `Done`; reopened: In progress, In review or Planned by its branch and PR), because changes made with `GITHUB_TOKEN` do not trigger the status workflow; a dry run prints the change.
- `--check` makes no changes, like a dry run, and exits 1 when any create, update, close, reopen or removal is pending, 0 when the issues are in sync. Use it to verify the post-merge sync, not as a prerequisite to starting implementation.
- Only creates issues in the repository named by `git remote origin`. Create the labels first with `python3 scripts/bootstrap.py github --apply`.
- If an issue-sync run is cancelled while waiting for the shared `issue-sync` concurrency group, the
  daily preview identifies missed Phase updates. To apply a recovery immediately, run
  **Sync Phase Issues** on `main` with **Apply changes** selected and no feature
  input; review the preview first if applying changes has not been approved.
- Maintaining `tasks.md` directly in reviewed implementation PRs is allowed. Rerun `speckit-analyze` after structural edits (Phases, tasks, dependencies), not after checkbox-only or wording changes.

## sync-project-status.py

Reconciles Phase (`Story`) statuses in the configured Project. Only `Story`
(Phase) issues are changed; Epics, Planning issues and all other issues are left
untouched.

```bash
# Uses the board in project-context.toml; without --apply these commands only preview changes.
python3 scripts/sync-project-status.py --issue 123
python3 scripts/sync-project-status.py --all
python3 scripts/sync-project-status.py --all --apply
# Explicit overrides take precedence over the corresponding TOML settings.
python3 scripts/sync-project-status.py --all --project-owner example-org --project-number 42
```

- **Setup checklist (including repositories created from a template):** configure
  `github_project.owner` and `github_project.number` in
  [`project-context.toml`](../project-context.toml) for the intended board. Agents and
  automation use this shared configuration; the workflow validates it before creating
  an App token. There are no hardcoded board defaults. Each local CLI option overrides
  its corresponding TOML setting; missing options require valid context. Supplying
  both options allows a local run without the context file.
- A Phase is `Planned` until its matching phase branch exists (the branch's issue
  number must match the issue, and its `phase-N` must match `Phase N:` in the
  issue title), `In progress` while that branch exists, `In review` while any
  open PR from its phase branch or closing it exists (including drafts), and `Done` when the issue is closed.
  `Backlog` is treated as not started. Closed issues with `state_reason=not_planned`
  move to Backlog rather than Done. When an issue reopens, its status returns to
  Planned unless an active branch or PR moves it to a later state.
- The workflow uses a board credential: a GitHub App installation token or, for a personal-account
  board, the `PROJECT_BOARD_TOKEN` classic PAT (see
  [Board credentials](../docs/bootstrap.md#board-credentials)). For the App, configure repository secrets
  `PROJECT_STATUS_APP_ID` and `PROJECT_BOARD_APP_PRIVATE_KEY`; the App needs
  to be installed for this repository, with Issues read access and organization Projects write
  access; repository reads (including phase branches) use the workflow's `GITHUB_TOKEN`. The
  issue-sync workflow uses its permissions-scoped `GITHUB_TOKEN` for repository issues and a separate
  App token restricted to organization Projects write access. A personal PAT is fine for a solo
  project (`required_approvals = 0`) or one where you are the only person who can push; if
  `required_approvals` is 1 or more and several people can push, use the App or a dedicated machine
  account (see the Board credentials guide). Only
  same-repository PRs, or PRs by an `OWNER`, `MEMBER` or `COLLABORATOR`, count for In review; other
  fork PRs are ignored.
  The token action defaults to the current repository; configure the App installation
  to grant access to that repository and the target organization's Project.
- Configure two Project views so their Done lanes stay separate: Features filtered
  by `label:Epic`, and Phases filtered by `label:Story`. Planning issues and Tasks
  are not included. Hide the `Planned` column in the Phases view.
- **Review status convention:** `In review` means an open PR exists for the Phase,
  not necessarily that its author has marked it ready for review. A draft PR from the Phase branch
  (`Part of #<phase>`, or one that closes it) moves the Phase to `In review`;
  marking it ready or converting it back to draft does not change that status.
  Closing the PR without closing the Phase returns it to `In progress` if its
  branch still exists, or `Planned` otherwise. Reopened issues leave `Done`;
  roadmap-paused or retired issues go to `Backlog`.
- In Project > Workflows, keep "Pull request linked to issue" enabled with
  Status = `In review`. Its draft-PR behavior now agrees with the script, and it
  continues to provide review-status updates for Planning and other issues the
  script ignores. Phase PRs use `Part of #<phase>` and the script maps them by their
  branch name, so the built-in workflow does not apply to them. Keep "Item closed", "Pull request merged", and
  the auto-add workflows unchanged. If the linked-PR workflow was disabled during
  rollout, re-enable it after this change is merged. These are manual Project
  settings; merging code does not change the workflow toggle.
- The workflow reconciles phase-branch pushes, phase PR changes, issue close/reopen
  and label events, and runs a daily `--all` safety pass. Dispatch it with an issue
  number for a targeted run or leave it empty to apply to all issues.

## Roadmap item walkthrough

From roadmap entry to synchronized Phase issues, with 008 as the example:

1. **Sync roadmap issues.** The **Sync Roadmap to GitHub Issues** workflow creates the Epic, the spec-plan planning issue and the provisional Phase 1 story after a planned entry merges to `main`. The first sync is manual: review its dry-run output, then run it with **Apply changes** selected. Afterwards a push to `main` applies automatically (turn this off with `ROADMAP_SYNC_AUTO_APPLY=false`), and a pull request that changes the roadmap or specs gets a preview comment of the planned changes before it merges.
2. **Spec and plan.** Create the branch from the `SPEC: 008 Create and approve spec and plan` issue (`gh issue develop <issue-number> --base main --name <issue-number>-spec-008-create-and-approve-spec-and-plan --checkout`) and follow its checklist: every `before_`/`after_` hook from `.specify/extensions.yml` (before specifying, `roadmap-entry.check` validates `SPECIFY_FEATURE_DIRECTORY=specs/008-<slug>` and warns if prerequisites remain), `speckit-specify`, `speckit-clarify`, user approval of the spec, `speckit-plan`, review and a non-author approval. Keep the roadmap plan-only. Merging the pull request closes the issue and the workflow refreshes artifact links; the sync run on that merge also clears the Phase 1 blocker (with automatic apply turned off, a manual apply does).
3. **Tasks.** Pull `main`, create the Phase 1 implementation branch from the provisional Phase 1 issue, and run `plan-merged.check`, `speckit-tasks` with its hooks and `speckit-analyze` with CRITICAL findings resolved, then commit `tasks.md` and push the branch with the user's approval. There is no separate tasks PR; `tasks.md` is reviewed with the implementation PR and can be maintained directly in later reviewed implementation PRs.
4. **Automatic sync.** The merge of a PR that adds `tasks.md` runs the Sync Phase Issues workflow, which updates Phase 1 from `tasks.md` and creates the other Phase issues under the existing Epic.
5. **Confirm the post-merge sync.** Check that the workflow run succeeded and that `python3 scripts/sync-task-issues.py --feature specs/008-<slug> --check` exits 0. This verifies Phase issues after the implementation PR merges; it is not a gate before implementation.
6. **Recover a failed sync.** Re-run the workflow from the Actions tab (**Run workflow** on `main`, `feature` = `specs/008-<slug>`), or run `python3 scripts/sync-task-issues.py --feature specs/008-<slug> --apply` locally; then repeat the `--check`.

## test_sync_task_issues.py and test_sync_roadmap_issues.py

Unit tests for both synchronization scripts. They replace `gh` with the in-memory fake in `fake_github.py`, so they never call GitHub, and use temporary fixture roadmaps with made-up IDs, so they do not depend on the live roadmap:

```bash
python3 -m unittest scripts/test_sync_task_issues.py scripts/test_sync_roadmap_issues.py
```

## check_spec_pr_phases.py

Enforces that a feature's spec and plan merge before its tasks. `plan-merged --feature specs/<feature>` is run by the `before_tasks` hook (`plan-merged.check`); `pr-files` is run by the [Spec PR phases workflow](../.github/workflows/spec-pr-phases.yml) and fails a PR that adds a new `tasks.md` together with `spec.md` or `plan.md`, or whose feature lacks `spec.md` or `plan.md` on the base branch (`--base-ref`), unless it has the `combined-spec-pr-approved` label. Tests: `python3 -m unittest scripts/test_check_spec_pr_phases.py`.

## pr_conventions.py

Validates issue-backed and Spec Kit branch names and adds a closing reference to
pull request bodies (`Closes #N`; for a Phase branch `update-body --phase` adds `Part of #N`,
because a Phase PR is often partial and the Phase sync closes the Phase when all its tasks are
checked; a closing keyword the author wrote is left alone, since the sync reopens an incomplete Phase). The PR workflow uses `parse-pr-branch` to prefer a matching
numbered spec directory when a feature branch resembles an issue branch, and
`validate-spec-branch` to require changed files under that directory. It is run by the
[PR conventions workflow](../.github/workflows/pr-conventions.yml) and, through
`check-push`, by the `branch-name` pre-push hook in
[lefthook.yml](../lefthook.yml). `check-push` reads Git's pre-push lines from
stdin and checks the format of each pushed remote branch name, skipping tags,
deletions and `main`. See the
[branch and pull request conventions](../docs/ci-and-conventions.md#branch-and-pull-request-conventions)
for the issue/roadmap ID collision caveat.

```bash
python3 scripts/pr_conventions.py parse \
  101-phase-1-build-feature
python3 scripts/pr_conventions.py parse-pr-branch \
  100-spec-create-feature --changed-paths-file /tmp/pr-changed-paths.txt
python3 scripts/pr_conventions.py validate-spec-branch \
  100-spec-create-feature --changed-paths-file /tmp/pr-changed-paths.txt
echo "refs/heads/x $(git rev-parse HEAD) refs/heads/my-branch 0000000000000000000000000000000000000000" \
  | python3 scripts/pr_conventions.py check-push
python3 -m unittest scripts/test_pr_conventions.py
```

## agent_setup.py

Keeps one copy of each agent skill and sub-agent and one Spec Kit version (see [Coding agents](../docs/coding-agents.md#coding-agents)).

`check` is run by the Repository tools CI job and the `agent-setup` pre-commit hook. It does not depend on which tools a developer generates, and fails when:

- the versions recorded in `.specify/` disagree;
- a file listed in `.specify/integrations/*.manifest.json` is missing or edited;
- a shell script the skills run under `.specify/` is not executable (Spec Kit 1.2.0 `integration switch` drops the bit);
- an extension skill is missing from `.agents/skills/`;
- `.claude/skills` is not the expected symlink, or `.claude/agents` is a symlink;
- `.github/skills/` exists;
- an integration other than Codex is installed or default;
- a sub-agent source in `agent-definitions/` or `agent-definitions/agents.toml` is invalid (a source setting `model`, `effort` or `reasoning-effort`, a source without an `[agents]` entry or an entry without a source, an unknown or unused profile, a value of the wrong type, an unknown tool or key, or an effort level not every tool accepts);
- an old-layout `.github/agents/<name>.md` exists;
- a lefthook-generated `post-checkout`, `post-merge` or `post-rewrite` hook is left in the Git hooks directory (`core.hooksPath` is respected; other tools' hooks, such as Git LFS, are ignored). `lefthook install` does not delete hooks removed from the config, and a leftover one runs the checked-out branch's `lefthook.yml`, so the error prints `rm -f .git/hooks/post-checkout .git/hooks/post-merge .git/hooks/post-rewrite`. This fails only on a machine that has the files; CI and fresh clones pass. Lefthook also reads the config from the working tree whenever any hook runs, so use `git -c core.hooksPath=/dev/null` for checkouts and commits on untrusted branches;
- Git tracks any file under `.github/agents/`, `.claude/agents/` or `.codex/agents/` (the three folders are git-ignored as a whole; run `git rm --cached <path>`). It is skipped outside a Git work tree; other Git failures (Git missing, dubious ownership, broken index) are reported.

The generated files themselves are git-ignored, so `check` does not look at them.

`sync` generates `.github/agents/<name>.agent.md`, `.claude/agents/<name>.md` and `.codex/agents/<name>.toml` from `agent-definitions/<name>.md` and `agents.toml`, and removes generated files without a source. It is run by `npm ci` and `npm run setup-agents` (the latter after asking which tools you use); no Git hook runs it, because checking out a branch must not run its code, so run `python3 scripts/agent_setup.py sync` after switching branches (`bootstrap.py check` reports out-of-date files). The `agent-setup` pre-commit hook only runs `check`. Files you put in these folders yourself are not versioned and Git may delete them, so keep personal agents in your user-level folder (for example `~/.claude/agents/`). Run it after changing `agent-definitions/`.

- Choose the tools with `setup` (below), the repeatable `--tool {claude,copilot,codex}` flag, for example `sync --tool copilot --tool claude`, or with the Git config `agent-setup.tools` (`git config agent-setup.tools copilot`, comma-separated), which plain `sync` and the hooks honour. Without any, all tools are generated, and a `sync` that changed something prints `Generated sub-agents for all tools (Claude Code, GitHub Copilot, Codex). Optional: run npm run setup-agents to keep only the ones you use; run it again whenever you change coding agents.` (a sync that changes nothing stays silent). An unknown tool name fails with an error and writes nothing. Generated files of the tools not selected are removed, and so are their agents folders once empty (`.codex/` too; never `.github/` or `.claude/`, nor any folder that still holds anything). Folders are created only for the selected tools.
- `setup` (`npm run setup-agents`) is interactive: it asks which agents you use (numbers `1) GitHub Copilot  2) Claude Code  3) Codex` or tool names, comma or space separated, asking again on invalid input), offers the current `agent-setup.tools` as the default (Enter keeps it; all tools when none is set), saves it with `git config --local agent-setup.tools <tools>`, prints `Saved: ...` and runs `sync` for those tools. After a successful sync it prints `Run npm run setup-agents again whenever you change coding agents, to generate the files for the new ones.` Without a terminal it exits 1 and tells you to use `git config agent-setup.tools <tools>` and `sync`.
- Only files carrying the generated marker count as generated. Hand-written files in those folders (for example a personal Claude Code agent in `.claude/agents/`) are never removed by `sync`, but the folders are git-ignored as a whole, so Git may delete such files on checkout. They must stay unversioned: `check` fails when Git tracks any file there, so shared agents belong in `agent-definitions/`. Keep personal agents in your user-level folder (for example `~/.claude/agents/`).
- Personal (unversioned) Copilot agents in `.github/agents/` must use the `.agent.md` suffix, because any other `.md` file there fails as the old layout.

If `check` reports `.github/skills: must not exist`, remove the obsolete directory (including any local-only files such as `.DS_Store`) and rerun `check`; `sync` does not remove it. Restore or regenerate Spec Kit files only when `check` reports generated-file problems.

```bash
python3 scripts/agent_setup.py check
python3 scripts/agent_setup.py sync                  # all tools, or those in git config agent-setup.tools
python3 scripts/agent_setup.py sync --tool copilot   # only the given tool(s); repeatable
python3 scripts/agent_setup.py setup                 # interactive; same as npm run setup-agents
python3 -m unittest scripts/test_agent_setup.py    # Python 3.11+ for every test
```

## specify.sh

Runs the Spec Kit CLI through `uvx` at the version pinned in `.specify/integration.json`, so maintainers regenerate the same files. Set `SPECKIT_VERSION` only in a pull request that upgrades Spec Kit.

```bash
scripts/specify.sh integration status
SPECKIT_VERSION=<new> scripts/specify.sh integration upgrade codex
```

## check_roadmap_spec_ids.py

Checks that each roadmap Spec dir and each spec Input entry match the spec directory prefix, and that each Spec dir already contains `spec.md`. Run `python3 scripts/check_roadmap_spec_ids.py`; Repository tools CI invokes it indirectly through `python3 scripts/check_roadmap.py` on pull requests. Tests: `python3 -m unittest scripts/test_check_roadmap_spec_ids.py`.
