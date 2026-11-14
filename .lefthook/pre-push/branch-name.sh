#!/bin/sh
# Run by the lefthook pre-push hook. Git passes '<local ref> <local sha> <remote ref> <remote sha>'
# lines on stdin. A script, unlike a `run` command, is not skipped when the push has no changed files.
exec python3 scripts/pr_conventions.py check-push
