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
#   1. Ask `teatree.hooks.foreign_mr_cli` for the branch's backing OPEN
#      MR — it routes by the remote's host to `gh` or `glab`, so a
#      GitLab remote is gated too. Shelling `gh` here hard-coded ONE
#      forge: on a GitLab remote the guard could never fire at all.
#   2. On a `FOREIGN` verdict, BLOCK and name the author + MR number.
#   3. Our own MR branch, a branch with no open MR, and a foreign
#      CLOSED/merged MR (which the open-state query excludes) all pass.
#
# Override: a genuine co-authoring push carries the token
#   [push-to-foreign-mr-ok: <reason>]
# in any commit message in the push range — the gate then allows it.
#
# Sibling of `refuse-public-push-with-leak.sh` (#685/#730): same Phase-0
# pre-push prek block, same fail-OPEN posture, same interpreter fallback
# chain. When no forge CLI is available, the slug is not an owner/repo
# shape, our login can't be resolved, or the MR query fails, the CLI
# answers NONE and the gate passes through — a transient forge-API
# failure must never brick a legitimate push, and this is a safety net
# layered on top of the behavioural rule, not the only line of defence.
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
remote_name="${PRE_COMMIT_REMOTE_NAME:-${1:-origin}}"
remote_url="${PRE_COMMIT_REMOTE_URL:-${2:-}}"

if [ -z "${remote_url}" ]; then
  remote_url=$(git remote get-url "${remote_name}" 2>/dev/null || true)
fi
[ -n "${remote_url}" ] || exit 0  # no remote URL — nothing to gate

# A cheap owner/repo shape check so a remote no forge could name never
# pays an interpreter start (the ssh-shape example below carries the
# inline allow-annotation so this hook's own header does not self-trip
# the privacy gate):
#   https://github.com/owner/repo(.git)
#   git@github.com:owner/repo(.git)  # privacy-scan:allow doc example
# The authoritative normalisation is the CLI's own `slug_for_remote_url`.
slug=$(printf '%s' "${remote_url}" \
  | sed -E 's#^[^:]+://[^/]+/##; s#^git@[^:]+:##; s#\.git$##')
case "${slug}" in
  */*) : ;;
  *) exit 0 ;;  # not an owner/repo shape — no forge to ask, fail open
esac

# Resolve the repo root from this script's own location
# (scripts/hooks/<this>.sh -> repo root) so the resolver CLI runs against
# THIS clone's teatree regardless of the caller's cwd. The interpreter
# fallback offers core's package root in BOTH supported layouts and tries
# version-explicit interpreters before a bare `python3`, which on a stock
# Mac is the Command Line Tools stub and cannot import core whatever
# PYTHONPATH says. Mirrors `refuse-public-push-with-leak.sh`.
script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

_resolve_foreign_mr() {
  local branch="$1" py probe_path
  probe_path="${repo_root}/src:${repo_root}/vendor/teatree/src${PYTHONPATH:+:${PYTHONPATH}}"

  if command -v uv >/dev/null 2>&1 && uv run --project "${repo_root}" --no-sync \
      python -m teatree.hooks.foreign_mr_cli "${remote_url}" "${branch}" 2>/dev/null; then
    return 0
  fi
  for py in python3.13 python3.14 python3; do
    command -v "${py}" >/dev/null 2>&1 || continue
    if PYTHONPATH="${probe_path}" \
        "${py}" -m teatree.hooks.foreign_mr_cli "${remote_url}" "${branch}" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

# Every remote-tracking ref of the pushed-to remote, or empty when there is
# none locally — the same "what does the remote already have?" answer
# refuse-public-push-with-leak.sh subtracts.
_remote_exclusion() {
  local first
  first=$(git for-each-ref --count=1 --format='%(refname)' \
    "refs/remotes/${remote_name}" 2>/dev/null || true)
  [ -n "${first}" ] || return 1
  printf '%s' "--remotes=${remote_name}"
}

# See refuse-public-push-with-leak.sh: prek/pre-commit consume the pre-push
# stdin and expose PRE_COMMIT_* instead, so a stdin-only hook is inert under the
# prek wrapper. Fall back to the env-synthesized ref line when stdin is empty.
refs_input=$(cat)
if [ -z "${refs_input//[[:space:]]/}" ] && [ -n "${PRE_COMMIT_TO_REF:-}" ]; then
  refs_input=$(printf '%s %s %s %s\n' \
    "${PRE_COMMIT_LOCAL_BRANCH:-HEAD}" "${PRE_COMMIT_TO_REF}" \
    "${PRE_COMMIT_REMOTE_BRANCH:-HEAD}" "${PRE_COMMIT_FROM_REF:-$ZERO}")
fi

blocked=0
while read -r local_ref local_sha _remote_ref remote_sha; do
  [ -n "${local_sha:-}" ] || continue
  [ "${local_sha}" != "${ZERO}" ] || continue  # branch deletion — skip

  branch=${local_ref#refs/heads/}
  [ -n "${branch}" ] || continue

  # Ask the host-routed resolver. An unresolvable answer (no forge CLI, no
  # login, a probe error) comes back NONE, so only a CONFIRMED foreign open
  # MR reaches the block below. `T3_FOREIGN_MR_CMD` overrides the resolver
  # for testing, mirroring `T3_REPO_VISIBILITY_CMD`.
  if [ -n "${T3_FOREIGN_MR_CMD:-}" ]; then
    verdict=$(${T3_FOREIGN_MR_CMD} "${remote_url}" "${branch}" 2>/dev/null || true)
  else
    verdict=$(_resolve_foreign_mr "${branch}" || true)
  fi
  read -r kind pr_number pr_author our_login <<<"${verdict}" || true
  [ "${kind:-}" = "FOREIGN" ] || continue

  # A foreign OPEN MR backs this branch. Allow only with an explicit
  # co-authoring override token in the commit messages the push INTRODUCES —
  # the pushed sha's whole ancestry would turn one already-pushed token into a
  # permanent blanket waiver for every later push to the teammate's branch.
  # Subtract what the remote already has, mirroring the leak gate: its
  # tracking refs, plus the protocol's remote-side tip when it resolves here.
  push_range=("${local_sha}")
  exclusions=()
  remote_exclusion=$(_remote_exclusion || true)
  if [ -n "${remote_exclusion}" ]; then
    exclusions+=("${remote_exclusion}")
  fi
  if [ -n "${remote_sha:-}" ] && [ "${remote_sha}" != "${ZERO}" ]; then
    remote_tip=$(git rev-parse --verify --quiet "${remote_sha}^{commit}" 2>/dev/null || true)
    if [ -n "${remote_tip}" ]; then
      exclusions+=("${remote_tip}")
    fi
  fi
  if [ ${#exclusions[@]} -gt 0 ]; then
    push_range+=("--not" "${exclusions[@]}")
  fi

  # Capture the log into a variable and match it with a here-string (no pipe):
  # a `git log | grep -q` pipeline is a SIGPIPE hazard under `set -o pipefail` —
  # grep -q exits on the first match and closes the pipe, so git log dies with
  # 141 and pipefail propagates that non-zero status, making the `if` false even
  # though the token WAS present (a load-dependent flake). The here-string has no
  # producer process to receive SIGPIPE, so the match is deterministic.
  push_range_messages=$(git log --format='%B' "${push_range[@]}" 2>/dev/null || true)
  if grep -qiE '\[push-to-foreign-mr-ok:' <<<"${push_range_messages}"; then
    continue
  fi

  echo "✗ refuse: '${branch}' backs an OPEN MR (#${pr_number}) authored by '${pr_author}', not you ('${our_login}')."
  echo "  Pushing would silently modify a teammate's MR. Your changes belong on YOUR own branch."
  echo "  A worktree opened to INSPECT a colleague's MR is read-only (see /t3:rules § never push to a colleague's open MR branch)."
  echo "  For a genuine co-authoring push, add [push-to-foreign-mr-ok: <reason>] to a commit message in the push range."
  blocked=1
done <<< "${refs_input}"

exit "${blocked}"
