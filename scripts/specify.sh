#!/usr/bin/env bash
# Runs the Spec Kit CLI at the version this repository was generated with (.specify/integration.json),
# so every maintainer regenerates the same files. Needs uv (https://docs.astral.sh/uv/).
# Set SPECKIT_VERSION only when upgrading Spec Kit on purpose, for example:
#   SPECKIT_VERSION=<new-version> scripts/specify.sh integration upgrade codex
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
VERSION="${SPECKIT_VERSION:-$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$ROOT/.specify/integration.json")}"

cd "$ROOT"
exec uvx --from "specify-cli==$VERSION" specify "$@"
