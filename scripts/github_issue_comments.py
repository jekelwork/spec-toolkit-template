"""Idempotent comments for GitHub issue synchronization."""

import json


def ensure_issue_comment(gh, api, issue_number, comment, marker, apply):
    """Post an issue comment once, even when a later close request must be retried."""
    if not apply:
        print(f"[dry-run] comment on #{issue_number}: {comment}")
        return True

    output = gh(
        "api",
        "--paginate",
        f"{api}/issues/{issue_number}/comments?per_page=100",
        "--jq",
        ".[] | {body}",
    ).stdout
    comments = [json.loads(line).get("body", "") for line in output.splitlines()]
    if any(marker in body or comment == body.strip() for body in comments):
        return False

    gh(
        "api",
        "-X",
        "POST",
        f"{api}/issues/{issue_number}/comments",
        "-f",
        f"body={comment}\n\n{marker}",
    )
    return True
