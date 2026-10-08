#!/usr/bin/env bash
# Runs the INSTALLED t3, never the checkout's own: its editable install resolves a per-worktree isolated control DB,
# so its rows were invisible to the merge gates. Only a branch push writes; a ship push opens its own PR, so it skips.
set -euo pipefail

case "${PRE_COMMIT_REMOTE_BRANCH:-}" in refs/heads/*) ;; *) exit 0 ;; esac
if [ -n "${TEATREE_SHIP_PUSH:-}" ]; then
  echo "ensure-pr skipped: TEATREE_SHIP_PUSH is set, so this push belongs to a ship that opens its own PR" >&2
  exit 0
fi

top="$(git rev-parse --show-toplevel)"
kept=""
IFS=: read -ra dirs <<<"$PATH"
for dir in "${dirs[@]}"; do
  case "$dir" in "$top" | "$top"/*) ;; *) kept="${kept:+$kept:}$dir" ;; esac
done
export PATH="$kept"

rc=0
t3 teatree pr ensure-pr --repo "$top" || rc=$?
case "$rc" in 69 | 75 | 126 | 127 | 137)
  echo "ensure-pr skipped: the installed t3 cannot run here (exit $rc). Once it runs, open the PR with: t3 <overlay> pr ensure-pr --repo $top --branch ${PRE_COMMIT_REMOTE_BRANCH#refs/heads/}" >&2
  exit 0
  ;;
esac
exit "$rc"
