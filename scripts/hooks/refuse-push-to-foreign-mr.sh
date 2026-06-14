#!/usr/bin/env bash
# Pre-push hook: foreign-open-MR guard (#2211).
#
# Refuses `git push` to a branch that backs an OPEN MR/PR authored by
# someone OTHER than the configured user identity — a teammate's open
# MR. Pushing to such a branch silently modifies their MR (our changes
# belong on OUR branch). A worktree opened to INSPECT a colleague's MR
# is read-only; this gate is the deterministic enforcement of that rule.
#
# For each ref being pushed:
#   1. Resolve the target branch's backing OPEN PR via `gh pr list`.
#   2. If an OPEN PR exists whose author != our login, BLOCK and name the
#      author + PR number.
#   3. Our own MR branch, a branch with no open MR, and a foreign
#      CLOSED/merged MR (which `--state open` excludes) all pass.
#
# Override: a genuine co-authoring push carries the token
#   [push-to-foreign-mr-ok: <reason>]
# in any commit message in the push range — the gate then allows it.
#
# Sibling of `refuse-public-push-with-leak.sh` (#685/#730): same Phase-0
# pre-push prek block, same fail-OPEN posture. When `gh` is unavailable,
# the slug is not an owner/repo shape, our login can't be resolved, or
# the `gh pr list` call fails, the gate fails OPEN (passes through) — a
# transient forge-API failure must never brick a legitimate push, and
# this is a safety net layered on top of the behavioural rule, not the
# only line of defence.
#
# Git invokes a pre-push hook as:  hook <remote-name> <remote-url>
# and feeds ref updates on stdin, one per line:
#   <local-ref> <local-sha> <remote-ref> <remote-sha>
# A deleted ref has local-sha all-zeros (skip it).
#
# Wired via prek in `.pre-commit-config.yaml` (stages: [push]) so it
# ships with the repo and needs no per-machine bootstrap.
set -euo pipefail

ZERO="0000000000000000000000000000000000000000"
remote_name="${1:-origin}"
remote_url="${2:-}"

if [ -z "${remote_url}" ]; then
  remote_url=$(git remote get-url "${remote_name}" 2>/dev/null || true)
fi
[ -n "${remote_url}" ] || exit 0  # no remote URL — nothing to gate

# Extract owner/repo from common GitHub URL shapes (the ssh-shape
# example below carries the inline allow-annotation so this hook's own
# header does not self-trip the privacy gate):
#   https://github.com/owner/repo(.git)
#   git@github.com:owner/repo(.git)  # privacy-scan:allow doc example
slug=$(printf '%s' "${remote_url}" \
  | sed -E 's#^[^:]+://[^/]+/##; s#^git@[^:]+:##; s#\.git$##')
case "${slug}" in
  */*) : ;;
  *) exit 0 ;;  # not an owner/repo shape — cannot ask gh, fail open
esac

command -v gh >/dev/null 2>&1 || exit 0  # no gh — fail open

# Our forge identity. An unresolved login means we cannot tell our own
# MR from a teammate's, so fail OPEN rather than block every push.
our_login=$(gh api user --jq '.login' 2>/dev/null | tr -d '[:space:]' || true)
[ -n "${our_login}" ] || exit 0
our_login_lc=$(printf '%s' "${our_login}" | tr '[:upper:]' '[:lower:]')

blocked=0
while read -r local_ref local_sha _remote_ref _remote_sha; do
  [ -n "${local_sha:-}" ] || continue
  [ "${local_sha}" != "${ZERO}" ] || continue  # branch deletion — skip

  branch=${local_ref#refs/heads/}
  [ -n "${branch}" ] || continue

  # Resolve the OPEN PR(s) whose head branch is exactly this branch. Use
  # `gh`'s built-in `--jq` to emit `<number>\t<login>` for each match
  # (no system `jq` dependency). A `gh` failure here is inconclusive —
  # fail OPEN (skip this ref).
  pr_rows=$(gh pr list --repo "${slug}" --head "${branch}" --state open \
    --json number,author --jq '.[] | "\(.number)\t\(.author.login)"' \
    2>/dev/null) || continue
  [ -n "${pr_rows}" ] || continue  # no open PR for this branch — allow

  # Take the first matching open PR (one branch backs at most one).
  first_row=$(printf '%s\n' "${pr_rows}" | head -n1)
  pr_number=$(printf '%s' "${first_row}" | cut -f1)
  pr_author=$(printf '%s' "${first_row}" | cut -f2)
  [ -n "${pr_author}" ] || continue

  pr_author_lc=$(printf '%s' "${pr_author}" | tr '[:upper:]' '[:lower:]')
  [ "${pr_author_lc}" != "${our_login_lc}" ] || continue  # our own MR — allow

  # A foreign OPEN MR backs this branch. Allow only with an explicit
  # co-authoring override token in the push range's commit messages.
  if git log --format='%B' "${local_sha}" 2>/dev/null \
    | grep -qiE '\[push-to-foreign-mr-ok:'; then
    continue
  fi

  echo "✗ refuse: '${branch}' backs an OPEN MR (#${pr_number}) authored by '${pr_author}', not you ('${our_login}')."
  echo "  Pushing would silently modify a teammate's MR. Your changes belong on YOUR own branch."
  echo "  A worktree opened to INSPECT a colleague's MR is read-only (see /t3:rules § never push to a colleague's open MR branch)."
  echo "  For a genuine co-authoring push, add [push-to-foreign-mr-ok: <reason>] to a commit message in the push range."
  blocked=1
done

exit "${blocked}"
