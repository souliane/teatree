#!/usr/bin/env bash
# Pre-push hook: public-repo privacy gate (#685).
#
# Refuses `git push` when the `origin` remote resolves to a PUBLIC
# repository and the branch-vs-base diff fails `t3 tool privacy-scan`
# (a planted secret, an internal `/Users/`-`/home/` path, a private IP,
# an API token, an internal hostname, or a T3_BANNED_TERM). Pushes to a
# private remote, and clean pushes to a public remote, pass through.
#
# This is the deterministic enforcement home for the contribute-mode
# rule "no customer/internal identifier reaches a public repo": the
# skill prose states the policy, this hook blocks the action.
#
# Git invokes a pre-push hook as:  hook <remote-name> <remote-url>
# and feeds ref updates on stdin, one per line:
#   <local-ref> <local-sha> <remote-ref> <remote-sha>
# A deleted ref has local-sha all-zeros (skip it). For a new remote ref
# the remote-sha is all-zeros, so the gate falls back to the merge-base
# with the remote's default branch as the comparison base.
#
# Visibility is resolved via `gh repo view <owner>/<repo> --json
# visibility`. When `gh` is unavailable or the visibility cannot be
# determined, the gate fails OPEN (passes through) — it is a safety net
# layered on top of the privacy scan in retro/contribute, not the only
# line of defence, and blocking every push on a gh-less machine would
# break the workflow.
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

# Extract owner/repo from common GitHub URL shapes:
#   https://github.com/owner/repo(.git)
#   git@github.com:owner/repo(.git)
slug=$(printf '%s' "${remote_url}" \
  | sed -E 's#^[^:]+://[^/]+/##; s#^git@[^:]+:##; s#\.git$##')
case "${slug}" in
  */*) : ;;
  *) exit 0 ;;  # not an owner/repo shape — cannot ask gh, fail open
esac

command -v gh >/dev/null 2>&1 || exit 0  # no gh — fail open

visibility=$(gh repo view "${slug}" --json visibility \
  --jq '.visibility' 2>/dev/null || true)
# Normalise (gh emits PUBLIC/PRIVATE/INTERNAL).
visibility=$(printf '%s' "${visibility}" | tr '[:lower:]' '[:upper:]')
[ "${visibility}" = "PUBLIC" ] || exit 0  # private/internal/unknown → pass

scan_cmd=${T3_PRIVACY_SCAN_CMD:-t3 tool privacy-scan}

default_ref=$(git symbolic-ref --short refs/remotes/"${remote_name}"/HEAD 2>/dev/null || true)
default_branch=${default_ref#"${remote_name}"/}
default_branch=${default_branch:-main}

blocked=0
while read -r local_ref local_sha remote_ref remote_sha; do
  [ -n "${local_sha:-}" ] || continue
  [ "${local_sha}" != "${ZERO}" ] || continue  # branch deletion — skip

  if [ "${remote_sha}" != "${ZERO}" ] && [ -n "${remote_sha}" ]; then
    base="${remote_sha}"
  else
    base=$(git merge-base "${local_sha}" \
      "refs/remotes/${remote_name}/${default_branch}" 2>/dev/null || true)
  fi

  if [ -n "${base}" ]; then
    diff=$(git diff "${base}" "${local_sha}" 2>/dev/null || true)
  else
    # No comparison point (brand-new repo / unknown base): scan the
    # whole tree at the pushed sha rather than skipping the gate.
    diff=$(git show "${local_sha}" 2>/dev/null || true)
  fi

  [ -n "${diff}" ] || continue

  report=$(mktemp "${TMPDIR:-/tmp}/t3-privacy-gate.XXXXXX")
  if ! printf '%s\n' "${diff}" | ${scan_cmd} - >"${report}" 2>&1; then
    echo "✗ refuse: push to PUBLIC repo '${slug}' carries privacy findings on '${local_ref}'."
    cat "${report}" 2>/dev/null || true
    echo "  Scrub the diff (generic placeholders) before pushing to a public repo."
    echo "  (public-repo privacy gate — see /t3:rules § Verify Repo Visibility Before Filing External Issues)"
    blocked=1
  fi
  rm -f "${report}"
done

exit "${blocked}"
