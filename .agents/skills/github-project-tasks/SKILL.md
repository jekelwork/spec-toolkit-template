---
name: github-project-tasks
description: Find GitHub Issues assigned to a user on a GitHub Projects (v2) board. Use when the user asks to check their assigned tasks, what's on their plate, or their items on a GitHub project board/org project.
allowed-tools: Bash(gh:*) Bash(python3:*)
license: MIT
metadata:
  author: user
  version: "1.0"
---

Find items on a GitHub Projects (v2) board assigned to a specific user.

**Requirements:** `gh` CLI authenticated (`gh auth status`) with `project` and `read:org` token scopes.

**Fallback (no `gh` CLI):** If `gh` is not installed or not authenticated, and a GitHub MCP server tool is available (e.g. `github` MCP tools for searching issues/projects), use that instead: search issues assigned to the user with its issue/PR search tool, then look up the org project board (GraphQL `organization.projectV2` query) via the MCP's generic GraphQL/API tool if it exposes one. Prefer `gh` when both are available since it needs no additional query construction for Projects v2 items.

**Steps:**

1. Confirm the project number, org/owner, and GitHub username. When a board is not specified and the repository has `project-context.toml`, run `python3 scripts/project_context.py` from the repository root and offer its `github_project.owner` and `github_project.number` as defaults for confirmation. Explicit values from the user take precedence. Ask for the username if it was not given. If the context command fails, report the error rather than silently using a different board.
2. List project items as JSON and filter by assignee login in Python:

```bash
gh project item-list <PROJECT_NUMBER> --owner <ORG> --format json --limit 200 | python3 -c "
import json,sys
data=json.load(sys.stdin)
items=data.get('items',[])
mine=[i for i in items if i.get('assignees') and '<USERNAME>' in i.get('assignees')]
print('Total items:', len(items))
print('Assigned to <USERNAME>:', len(mine))
for i in mine:
    print('-', i.get('title'), '|', i.get('status'), '|', i.get('content',{}).get('url') if isinstance(i.get('content'), dict) else i.get('url'))
"
```

3. For each matching item, fetch full issue details for a summary:

```bash
gh issue view <ISSUE_NUMBER> --repo <OWNER>/<REPO> --json title,state,body,labels,assignees,url,milestone
```

**Notes:**
- `gh project item-list` returns items across all repos linked to the org project; `--limit` defaults low, raise it (e.g. 200) to avoid truncation on large boards.
- Filtering in Python avoids fragile `--jq` assignee array matching.
- Replace `<PROJECT_NUMBER>`, `<ORG>`, and `<USERNAME>` with actual values from the request.
