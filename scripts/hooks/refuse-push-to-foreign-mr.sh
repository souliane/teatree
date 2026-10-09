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
#   3. On an `UNKNOWN` verdict — an open MR backs the branch and this
#      venue could not resolve who we are — BLOCK too (#83). The guard
#      used to answer NONE there, which made its protection depend on
#      which venue it ran in: the same push was refused on the host and
#      permitted inside the worker container, and the container route
#      minted none of the override token below, so the bypass left no
#      audit trail at all.
#   4. Our own MR branch, a branch with no open MR, and a foreign
#      CLOSED/merged MR (which the open-state query excludes) all pass.
#
# Override: a genuine co-authoring push carries the token
#   [push-to-foreign-mr-ok: <reason>]
# in any commit message in the push range — the gate then allows it.
#
# Sibling of `refuse-public-push-with-leak.sh` (#685/#730): same Phase-0
# pre-push prek block, same interpreter fallback chain. When no forge CLI
# is available, the slug is not an owner/repo shape, or the MR query
# fails, the CLI answers NONE and the gate passes through — a transient
# forge-API failure must never brick a legitimate push, and this is a
# safety net layered on top of the behavioural rule, not the only line of
# defence. The one step that does NOT fail open is the identity, because
# it is only ever asked once a colleague's MR is already on the table.
#
# That asymmetry costs the operator nothing when the answer was already
# available: an MR authored by a login declared in `self_forge_identities`
# is OWN from a cold config read, before any forge call, so a venue whose
# probe cannot answer still pushes to its own MRs. When the identity is
# genuinely unsettled the refusal reports the cause it OBSERVED — a
# timeout is not an unauthenticated CLI, and naming it as one sends the
# operator to `auth status` on a credential that is working.
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

  # Ask the host-routed resolver. A question it never got to ask (no forge
  # CLI, no owner/repo shape, a failed MR query) comes back NONE and passes
  # through; FOREIGN is a confirmed colleague's MR and UNKNOWN is one whose
  # owner this venue cannot name. `T3_FOREIGN_MR_CMD` overrides the resolver
  # for testing, mirroring `T3_REPO_VISIBILITY_CMD`.
  if [ -n "${T3_FOREIGN_MR_CMD:-}" ]; then
    verdict=$(${T3_FOREIGN_MR_CMD} "${remote_url}" "${branch}" 2>/dev/null || true)
  else
    verdict=$(_resolve_foreign_mr "${branch}" || true)
  fi
  # Field 3 is the MR author on BOTH blocking verdicts, so the shared prefix is
  # read once and only the kind-specific tail is split below.
  read -r kind mr_number mr_author verdict_tail <<<"${verdict}" || true
  case "${kind:-}" in
    FOREIGN | UNKNOWN | ALIAS_UNRESOLVED | REMOTE_EMPTY) : ;;
    *) continue ;;
  esac

  # An open MR backs this branch that is not established as ours. Allow only
  # with an explicit co-authoring override token in the messages the push INTRODUCES —
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

  if [ "${kind}" = "REMOTE_EMPTY" ]; then
    echo "✗ refuse: the push names no remote URL, so no MR backing '${branch}' could be looked up."
    echo "  Set the origin remote ('git remote set-url origin <url>'), then retry the push."
    echo "  For a deliberate override, add [push-to-foreign-mr-ok: <reason>] to a commit message in the push range."
  elif [ "${kind}" = "ALIAS_UNRESOLVED" ]; then
    echo "✗ refuse: SSH alias '${mr_number}' could not be resolved to a forge host."
    echo "  Set HostName for '${mr_number}' in ~/.ssh/config, then retry the push."
    echo "  For a deliberate override, add [push-to-foreign-mr-ok: <reason>] to a commit message in the push range."
  elif [ "${kind}" = "UNKNOWN" ]; then
    read -r probe_tool probe_cause <<<"${verdict_tail}" || true
    echo "✗ refuse: '${branch}' backs an OPEN MR (#${mr_number}) authored by '${mr_author}'; this venue cannot tell whether that is you."
    # Report the cause the probe was OBSERVED to produce. Asserting an
    # unauthenticated CLI on a TIMEOUT sent the operator to `auth status`,
    # which answers "Logged in" and hides the real, transient cause.
    # Judged on the LAST attempt: an earlier one was a transient the retry outlived.
    case "${probe_cause##*, then }" in
      "timeout of "*)
        echo "  Observed: '${probe_tool} api user' did not answer (${probe_cause}) — the identity probe TIMED OUT."
        echo "  A timeout is not evidence the CLI is unauthenticated; it may well answer on the next attempt."
        remedy="Retry the push. If it keeps timing out, push from a venue where '${probe_tool}' answers promptly."
        ;;
      "a process start the host refused"*)
        echo "  Observed: this host briefly refused to start '${probe_tool}' (EAGAIN) — it is short of processes, not missing '${probe_tool}'."
        remedy="Retry the push once the host is less loaded."
        ;;
      tool-absent | exec-failed)
        echo "  Observed: '${probe_tool}' could not be run in this venue, so no identity was ever asked for."
        remedy="Install '${probe_tool}' here, or push from a venue that has it."
        ;;
      "an answer naming no login")
        echo "  Observed: '${probe_tool} api user' answered here, but the payload named no login."
        remedy="Re-authenticate '${probe_tool}' in THIS venue ('${probe_tool} auth status'), or push from a venue that resolves an identity."
        ;;
      *)
        echo "  Observed: '${probe_tool} api user' ran here and exited non-zero (${probe_cause})."
        remedy="If that names a credential problem, authenticate '${probe_tool}' in THIS venue ('${probe_tool} auth status'); otherwise retry, or push from a venue that resolves an identity."
        ;;
    esac
    echo "  An unresolvable identity is not evidence the push is harmless — the guard refuses rather than letting the check evaporate."
    echo "  ${remedy}"
    echo "  If '${mr_author}' is one of YOUR OWN logins, declare it once under 'self_forge_identities' — that is a cold config read, so it answers even here."
    echo "  For a genuine co-authoring push, add [push-to-foreign-mr-ok: <reason>] to a commit message in the push range."
  else
    echo "✗ refuse: '${branch}' backs an OPEN MR (#${mr_number}) authored by '${mr_author}', not you ('${verdict_tail}')."
    echo "  Pushing would silently modify a teammate's MR. Your changes belong on YOUR own branch."
    echo "  A worktree opened to INSPECT a colleague's MR is read-only (see /t3:rules § never push to a colleague's open MR branch)."
    echo "  For a genuine co-authoring push, add [push-to-foreign-mr-ok: <reason>] to a commit message in the push range."
  fi
  blocked=1
done <<< "${refs_input}"

exit "${blocked}"
