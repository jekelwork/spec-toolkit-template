# Spec-driven development toolkit

Build software with coding agents, one reviewed spec at a time. This toolkit gives your team
[Spec Kit](https://github.github.com/spec-kit/) workflows, a plan-only roadmap, GitHub issue and Project
automation, CI gates and shared sub-agents for **GitHub Copilot, Codex and Claude Code**. You don't
install Spec Kit yourself: the template already includes it.

Setting it up is a conversation: **you tell your coding agent to bootstrap the project, and it
walks you through the rest.**

You need `gh` 2.40+ (logged in, with the `project` scope: `gh auth refresh -s project`), Python 3.11+,
Node as pinned in `.nvmrc`, and admin rights on the repository. The agent checks your `gh` login and scope
first; see [prerequisites](docs/bootstrap.md#prerequisites) for details.

## Bootstrap in three steps

**1. Create your repository from the template**

```bash
# --public makes your code, specs and roadmap visible to everyone. Rulesets on a private repository
# need GitHub Pro, Team or Enterprise; use --private only on those plans.
gh repo create <owner>/my-project --template jekelwork/spec-toolkit-template --public --clone
cd my-project
npm ci
```

**2. Open your coding agent in the repository and say:**

> Bootstrap this project.

**3. Answer its questions.**

That is all. The agent reads where you are, explains each step, and does the work with you. It first
asks whether you want to run the commands yourself (**guide me**) or have it run them
(**do it for me**):

| The agent handles | You decide |
| --- | --- |
| Creating the bootstrap issue and branch | What the project does |
| Writing `project-context.toml`, and creating the Project board if you want a new one | Which Project board to use, and how many approvals a pull request needs |
| Labels, board fields and the `main` branch ruleset | The content of your constitution and roadmap (it can draft a first version for you) |
| Committing, pushing and opening the bootstrap pull request | Approving each preview, and merging the pull request |
| Running the first roadmap sync | |

Every change the agent makes on GitHub is shown as a preview first and applied only after your
approval. A few things are yours to do: set the optional board credential, turn on the board's "Pull
request linked to issue" workflow and set its Status to In review, and review and merge the
bootstrap pull request. With one or more required approvals, that pull request needs a second person
to approve it; a solo project should choose 0. Stop at any point and come back later: progress is read from the repository and
GitHub, so asking to "continue the bootstrap" picks up where you left off.

If the agent does not pick up the request by itself, call the skill by name: `/bootstrap-guide` in
Copilot and Claude Code, `$bootstrap-guide` in Codex.

### Prefer to run the commands yourself?

You have two ways to stay in control of every command:

- **In the agent, choose _guide me_.** The agent shows each command that changes something, explains
  it and reads the result, and you run it in your terminal. (It still runs the read-only checks
  itself.)
- **Without the agent driving:** run `python3 scripts/bootstrap.py start`. It previews each step and
  asks for confirmation. The constitution and roadmap steps still run as skills in your coding agent;
  `start` stops there and names the skill to run.

At any time, `python3 scripts/bootstrap.py` prints what is done and what comes next. It changes nothing.

## What you get

When bootstrap is complete, your repository has:

- **A constitution and a roadmap** that describe your governance and your plan, written with your
  agent.
- **A fixed path for every feature:** specify → clarify → plan → tasks → analyze → implement, with
  hooks that agents must run, and a CI check that the spec-and-plan pull request merges before any
  tasks exist.
- **GitHub kept in step:** every planned roadmap entry gets an **Epic** issue, the parent for all of
  its work. Under it sit one planning issue for the spec and plan, and one issue per **Phase**: a group
  of related tasks that is built and reviewed in one or more pull requests. Roadmap issues (including
  Phase 1) are created when you approve a sync run, and the other Phase issues when a pull request that
  changes `tasks.md` merges (see [how the syncs behave](docs/bootstrap.md#how-the-syncs-behave)). With an
  optional [board credential](docs/bootstrap.md#board-credentials), they also appear on your Project
  board and the cards move on their own as branches and pull requests open and merge.
- **Guardrails:** a protected `main`, required checks, required approvals (as many as you choose), and a
  code review that agents run before every pull request.
- **The same rules for every agent:** one copy of the skills and sub-agents, shared by Copilot, Codex
  and Claude Code.

After bootstrap, replace this README with your project's own.

## Learn more

- [Bootstrap guide](docs/bootstrap.md): every step, flag and troubleshooting tip
- [Local setup and Git hooks](docs/local-setup.md), including Windows and troubleshooting
- [Coding agents and Spec Kit](docs/coding-agents.md): shared skills, sub-agents, models and versions
- [Spec-driven workflow](docs/spec-workflow.md): the step order, hooks and Phase issues
- [Branch conventions, CI and rulesets](docs/ci-and-conventions.md)
- [Roadmap format](docs/roadmap-format.md) and [review and approvals](docs/review-and-approvals.md)
- [Agent instructions](AGENTS.md) and [sub-agents](docs/sub-agents.md)
- [Scripts](scripts/README.md)
