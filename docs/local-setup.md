# Local setup and Git hooks

Per-clone setup, Git hooks and platform notes.

## Getting started

Run these steps once per clone, in the repository root, before your first commit.

0. Create your repository from the template, unless you already have one. Do not clone the template repository directly. Follow [docs/bootstrap.md](bootstrap.md), or run `gh repo create <owner>/<name> --template jekelwork/spec-toolkit-template --public --clone`. Bootstrap then sets up your project's governance and GitHub settings.

1. Install the Git hooks (see [Git hooks](#git-hooks)) and generate the sub-agent files:

   ```bash
   npm ci
   ```

2. Choose the coding agent you use, so only its sub-agent files are generated (see [Coding agents](coding-agents.md#coding-agents)):

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

The repository contains a symlink (`.claude/skills`; see [Coding agents](coding-agents.md#coding-agents)). A clone without symlink support turns it into a small text file: Claude Code then finds no skills, and the `agent-setup` pre-commit hook fails with `.claude/skills: must be a symlink`. Work in WSL, where symlinks work without setup:

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

Git hooks are managed with [Lefthook](https://lefthook.dev) and configured in [lefthook.yml](../lefthook.yml). They are installed by `npm ci` (see [Getting started](#getting-started)), whose `prepare` script runs `lefthook install` and `python3 scripts/agent_setup.py sync`. The `pre-commit` hook runs in parallel:

- **prettier** formats staged `*.yml`/`*.yaml` files, including Spec Kit-generated ones, using the root [.prettierrc.json](../.prettierrc.json), and re-stages them.
- **secrets** runs [scripts/check-secrets.mjs](../scripts/check-secrets.mjs) on the staged files, including `.env.example` files, and refuses the commit if one looks like a credential. Use obvious placeholders in example files; add `secret-scan:ignore` to a line to mark a deliberate fixture.
- **agent-setup** runs `python3 scripts/agent_setup.py check` (the same check as CI) when agent or Spec Kit files are staged (see [Coding agents](coding-agents.md#coding-agents)). It never runs `sync`, because the generated files are git-ignored and the commit does not need them.

There are no `post-checkout`, `post-merge` or `post-rewrite` hooks: checking out a branch must not run its code. Run `python3 scripts/agent_setup.py sync` after switching branches (see [Coding agents](coding-agents.md#coding-agents)).

**Existing clones:** older versions of this toolkit installed those three hooks, and `lefthook install` does not delete hooks that were removed from the config. They still run the checked-out branch's `lefthook.yml`. Remove them once: `rm -f .git/hooks/post-checkout .git/hooks/post-merge .git/hooks/post-rewrite` (in the hooks directory of `core.hooksPath` when you set one). `python3 scripts/agent_setup.py check`, which runs in the `agent-setup` pre-commit hook and in CI, fails locally and prints this command while any of them is left; it never fails in CI or a fresh clone, where they do not exist.

The `pre-push` hook runs **branch-name**, [.lefthook/pre-push/branch-name.sh](../.lefthook/pre-push/branch-name.sh), which calls `python3 scripts/pr_conventions.py check-push`. It rejects a push whose remote branch name does not follow the [branch conventions](ci-and-conventions.md#branch-and-pull-request-conventions), so you can rename the branch before opening a pull request. It does not check the linked issue or matching Spec Kit directory; those checks run in the PR workflow. It skips tags, branch deletions and `main`. Bypass it with `git push --no-verify` or `LEFTHOOK=0` only when you have to; CI still validates the pull request.

For issue branches with unsupported work IDs and numeric prefixes of three or more digits, the
local syntax check may mistake the name for a Spec Kit branch; the PR workflow rejects it unless
the matching spec directory changed.
