# Review, approvals and decision records

Reference detail for the pull request rules in [`AGENTS.md`](../AGENTS.md). The hard rules stay there.

## Approvals

- **Approvals are enforced by the branch ruleset** with the count from `[review] required_approvals` in `project-context.toml` (required, with no implicit default; 0 is valid only where the constitution allows a solo project). The constitution sets the count; `required_approvals` mirrors it, and the constitution wins if they differ. GitHub records each review (reviewer, state, reviewed commit, time) on the pull request; nothing is written into spec, plan or task documents.
- **To see who approved a change,** find its pull request (the merge commit names it) and run `gh pr view <n> --json reviews --jq '.reviews[] | "\(.author.login) \(.state) \(.commit.oid[0:7]) \(.submittedAt)"'`. Count only reviews whose state is `APPROVED` and whose author is not the PR author.

## Bootstrap review exception

`AGENTS.md` says that every review finding is relayed to the user. There is one exception, for the
bootstrap pull request only (the one the `pull_request` step of `bootstrap.py start` opens, see
[One bootstrap pull request](bootstrap.md#one-bootstrap-pull-request)), so that the user is not
handed a list of findings to decide on a first constitution and roadmap:

- The agent fixes the review's Critical and Required findings itself through the skills
  (`speckit-constitution` for the constitution, `roadmap-write` for the roadmap), and in the first
  round also its Optional and Nit findings where feasible, without asking, and reports them in one
  line.
- A second round fixes only Critical and Required findings and drops the rest.
- A finding whose fix would change what the constitution or roadmap decides is still asked of the
  user, together in one multi-select question: those decisions stay with the user.
- No other finding is handed to the user to decide: dropped and FYI findings are counted in the
  chat and listed in the pull request body. The agent writes them, one line each under
  `## Review notes`, to
  `bootstrap-review-notes.md` at the repository root (the only file it writes outside the skills;
  Git ignores it) and passes `--review-notes` to the `pull_request` step. With none of them, there
  is no notes file and no flag.
- `security-auditor` findings are not covered by this exception: Critical and High findings are
  fixed and the others relayed as usual.
- The rule that every finding is relayed to the user applies again after the bootstrap, for every
  other pull request.

## Linking the issue in a pull request

For a non-Phase issue branch, the body includes `Closes #<github-issue>`. For a Phase branch it uses `Part of #<phase issue>` instead, because a Phase PR is often partial and a closing keyword would close the Phase on merge. The Phase sync closes a Phase when all its tasks are checked and reopens it when unchecked tasks remain. The PR conventions workflow derives the issue from the validated branch name and adds the right line when it is missing (it leaves a closing keyword the author wrote on a Phase PR alone, because the Phase sync reopens an incomplete Phase).

## Keeping the "Open decisions" comment

- Edit the comment in place. Find its ID and use `gh api -X PATCH repos/{owner}/{repo}/issues/comments/<id> -f body=...`; `gh issue comment --edit-last` works only while it is still your last comment.
- Write one line per item: a label that says what it is (not only "O2" or "items a–i"), its state (open or decided), and a one-line reason or context.
- When a pull request is created, move the decided items into its body; with several PRs per Phase, move only the items decided since the previous one. Then cut the comment down to the items still open; when none are left, delete it.
- A decision that changes the spec or design also goes through the Spec Kit skills when it is decided, before the final review (`speckit-clarify` for the spec, `speckit-plan` for the plan; rerun `speckit-analyze` if `tasks.md` is affected). The PR body still records it with its reason.
- Unfixed security findings never go in the comment or the PR body; see "Keeping Track Across Sessions" in `AGENTS.md`.
