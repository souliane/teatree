#!/usr/bin/env bash
# Print the version <uv.lock> pins for <package>; print nothing when it pins none.
set -euo pipefail
LOCK="${1:?usage: locked-version.sh <uv.lock> <package>}"
PACKAGE="${2:?usage: locked-version.sh <uv.lock> <package>}"
awk -v want="name = \"$PACKAGE\"" '$0 == want { getline; gsub(/^version = "|"$/, ""); print; exit }' "$LOCK"
