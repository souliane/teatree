#!/usr/bin/env bash
# Pre-push hook: public-repo privacy gate (#685, #730).
#
# Refuses `git push` when the `origin` remote resolves to a PUBLIC
# repository and the branch-vs-base diff OR the commit messages in the
# push range fail `t3 tool privacy-scan` (a planted secret, an internal
# `/Users/`-`/home/` path, a private IP, an API token, an internal
# hostname, or a T3_BANNED_TERM). Commit messages and trailers reach
# public history just like file content, so they are scanned too (#703).
#
# It ALSO refuses (#730) when any commit in the push range has an author
# OR committer email that is not a GitHub noreply address. A real /
# deliverable address (e.g. a customer/personal-domain address
# inherited from local git config) in PUBLIC history is a permanent PII
# leak that GitHub's own "block pushes that expose my email" does not
# catch for third-party domains. The accepted shape is the GitHub
# noreply pattern `([0-9]+\+)?<login>@users.noreply.github.com`, which
# covers every GitHub identity (souliane and any other login)
# without hardcoding one specific login.
#
# Pushes to a private remote, and clean pushes to a public remote, pass
# through.
#
# This is the deterministic enforcement home for the contribute-mode
# rule "no customer/internal identifier reaches a public repo": the
# skill prose states the policy, this hook blocks the action.
#
# Git invokes a pre-push hook as:  hook <remote-name> <remote-url>
# and feeds ref updates on stdin, one per line:
#   <local-ref> <local-sha> <remote-ref> <remote-sha>
# A deleted ref has local-sha all-zeros (skip it). The scanned set is the
# commits reachable from the pushed sha but from no already-public ref —
# `<local-sha> --not <origin/default> [<remote-sha>]` — never a single
# linear `<remote-sha>..<local-sha>` range, which a merge-forward inflates
# with the whole of `main` (#3523).
#
# Visibility is resolved via `gh repo view <owner>/<repo> --json
# visibility`. The gate SKIPS the scan only when the remote is KNOWN to be
# private/internal. Every undetermined case — no owner/repo shape, no
# `gh`, a `gh` error, or an unrecognised answer — fails CLOSED and the
# diff is scanned anyway, so a leak never rides out on a gh-less machine
# or an unparsable remote (§3f #14; was fail-open). "Fail closed" here
# means "scan anyway", NOT "block anyway": the scan still fails OPEN on a
# scanner crash and blocks ONLY on a real finding, so a clean push on a
# machine without `gh` is unaffected.
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

# Extract owner/repo from common GitHub URL shapes (the ssh-shape
# example below carries the inline allow-annotation so this hook's own
# header does not self-trip the privacy gate it powers):
#   https://github.com/owner/repo(.git)
#   git@github.com:owner/repo(.git)  # privacy-scan:allow doc example
slug=$(printf '%s' "${remote_url}" \
  | sed -E 's#^[^:]+://[^/]+/##; s#^git@[^:]+:##; s#\.git$##')

# Resolve visibility only when we have an owner/repo shape AND gh. Any
# other path leaves it empty (undetermined).
visibility=""
case "${slug}" in
  */*)
    if command -v gh >/dev/null 2>&1; then
      visibility=$(gh repo view "${slug}" --json visibility \
        --jq '.visibility' 2>/dev/null || true)
      # Normalise (gh emits PUBLIC/PRIVATE/INTERNAL).
      visibility=$(printf '%s' "${visibility}" | tr '[:lower:]' '[:upper:]')
    fi
    ;;
esac

case "${visibility}" in
  PRIVATE | INTERNAL)
    exit 0  # KNOWN non-public remote — nothing reaches public history
    ;;
  PUBLIC)
    : ;;  # confirmed public — scan
  *)
    # Undetermined visibility (no owner/repo shape, no gh, a gh error, or
    # an unrecognised answer). Fail CLOSED: scan anyway. The scan itself
    # still fails OPEN on a scanner crash and blocks ONLY on a real
    # finding, so a clean push on a gh-less machine still passes — only an
    # actual leak is stopped. Warn loudly so the undetermined path shows.
    echo "⚠ push privacy gate: could not confirm '${slug:-<remote>}' visibility (gh unavailable or unrecognised) — scanning anyway (fail closed, §3f #14)." >&2
    ;;
esac

scan_cmd=${T3_PRIVACY_SCAN_CMD:-t3 tool privacy-scan}

# Dedicated "findings present" exit code from scripts/privacy_scan.py
# (PRIVACY_FINDINGS_EXIT_CODE). The gate blocks ONLY on this code and fails
# OPEN on any other non-zero (a scanner crash, a missing script, an argparse
# usage error). Conflating "findings" with "crash" wedged every push closed
# whenever the scanner itself failed (#126 gap 3). Overridable for testing.
findings_code=${T3_PRIVACY_FINDINGS_EXIT_CODE:-3}

default_ref=$(git symbolic-ref --short refs/remotes/"${remote_name}"/HEAD 2>/dev/null || true)
default_branch=${default_ref#"${remote_name}"/}
default_branch=${default_branch:-main}

# Ref updates arrive on stdin under git's native pre-push protocol. But when the
# hook runs through prek/pre-commit (the `.pre-commit-config.yaml` wiring), the
# runner CONSUMES stdin itself and exposes the push range via PRE_COMMIT_* env
# vars — the hook then reads an EMPTY stdin and silently passes every push (the
# gate is inert). Capture stdin; when empty but PRE_COMMIT_TO_REF is set,
# synthesize the one ref-update line from the env so the loop below enforces
# under BOTH invocation paths (souliane/teatree: prek does not forward pre-push
# stdin to `language: system` hooks).
refs_input=$(cat)
synthesized=0
if [ -z "${refs_input//[[:space:]]/}" ] && [ -n "${PRE_COMMIT_TO_REF:-}" ]; then
  synthesized=1
  refs_input=$(printf '%s %s %s %s\n' \
    "${PRE_COMMIT_LOCAL_BRANCH:-HEAD}" "${PRE_COMMIT_TO_REF}" \
    "${PRE_COMMIT_REMOTE_BRANCH:-HEAD}" "${PRE_COMMIT_FROM_REF:-$ZERO}")
fi

# Is ${sha} the TRUE base to diff against for this ref update? On git's
# native pre-push protocol the reported remote_sha IS the real remote-side
# tip, so it is always authoritative. On the prek synthesized-from-env path
# the value is `PRE_COMMIT_FROM_REF`, which git reports as a STALE ancestor
# (a weeks-old `main` commit) for the first push of a long-lived branch that
# merged `main` since it was created (#3414) — NOT all-zeros and NOT the
# current tip. Trusting it re-includes dozens of already-public, immutable
# commits and false-positives the identity guard and the content scan.
# Trust the synthesized remote_sha ONLY when it is confirmed to be the
# current tip of the branch's remote-tracking ref (i.e. the branch already
# exists on the remote and this is an update push); otherwise fall back to
# the merge-base with the remote default branch, the true new-content range.
_remote_sha_is_trusted_base() {
  local remote_ref="$1" sha="$2"
  [ "${synthesized}" = "1" ] || return 0  # native stdin — git's sha is authoritative
  local branch="${remote_ref#refs/heads/}"
  [ -n "${branch}" ] && [ "${branch}" != "HEAD" ] || return 1
  local tracked
  tracked=$(git rev-parse --verify --quiet \
    "refs/remotes/${remote_name}/${branch}" 2>/dev/null || true)
  [ -n "${tracked}" ] && [ "${tracked}" = "${sha}" ]
}

blocked=0
while read -r local_ref local_sha remote_ref remote_sha; do
  [ -n "${local_sha:-}" ] || continue
  [ "${local_sha}" != "${ZERO}" ] || continue  # branch deletion — skip

  # The push newly exposes the commits reachable from the pushed sha but
  # from NO already-public ref: `origin/<default>` plus, when it is
  # confirmed to be the branch's own remote tip, the reported remote_sha.
  # A single linear `remote_sha..HEAD` range instead spans every commit a
  # merge-forward brought in from `main` — all of it already public, and
  # every finding and non-noreply identity in it a false positive (#3523).
  public_tips=()
  default_tip=$(git rev-parse --verify --quiet \
    "refs/remotes/${remote_name}/${default_branch}^{commit}" 2>/dev/null || true)
  if [ -n "${default_tip}" ]; then
    public_tips+=("${default_tip}")
  fi
  if [ "${remote_sha}" != "${ZERO}" ] && [ -n "${remote_sha}" ] \
    && _remote_sha_is_trusted_base "${remote_ref}" "${remote_sha}"; then
    remote_tip=$(git rev-parse --verify --quiet "${remote_sha}^{commit}" 2>/dev/null || true)
    if [ -n "${remote_tip}" ]; then
      public_tips+=("${remote_tip}")
    fi
  fi

  # No resolvable public tip (brand-new repo, unknown sha, shallow clone):
  # fail CLOSED by subtracting nothing, so the whole history reachable from
  # the pushed sha is scanned — wider, never narrower.
  new_commits=("${local_sha}")
  if [ ${#public_tips[@]} -gt 0 ]; then
    new_commits+=("--not" "${public_tips[@]}")
  fi

  # `--patch --cc` under an explicit `--format` emits each newly-public
  # commit's message body and its patch with no commit header, so the
  # author/committer emails below are judged only by the noreply guard and
  # never by the scanner's generic email matcher. `--cc` keeps a merge's
  # conflict resolutions — content in neither parent — in scope while
  # leaving the already-public content the merge carried over out of it.
  content=$(git log --format='%B' --patch --cc "${new_commits[@]}" 2>/dev/null || true)

  # Author / committer email is metadata `git diff` and `%B` never show,
  # yet it lands in public history forever. On a PUBLIC remote every
  # commit's author AND committer email must be a GitHub noreply address
  # (`([0-9]+\+)?<login>@users.noreply.github.com`); anything else — a
  # real/deliverable address such as a customer-domain email inherited
  # from local git config — is blocked (#730).
  noreply_re='^([0-9]+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com$'
  bad_idents=$(git log --format='%ae%n%ce' "${new_commits[@]}" 2>/dev/null \
    | grep -v -E "${noreply_re}" | sort -u || true)
  if [ -n "${bad_idents}" ]; then
    echo "✗ refuse: push to PUBLIC repo '${slug}' has a non-noreply commit identity on '${local_ref}'."
    echo "  A real/deliverable email in public git history is a permanent PII leak."
    echo "  Offending author/committer email(s):"
    printf '%s\n' "${bad_idents}" | sed 's/^/    /'
    echo "  Allowed shape: <id>+<login>@users.noreply.github.com (GitHub noreply)."
    echo "  Rewrite the range's author/committer to the repo's GitHub noreply identity, then re-push:"
    echo "    git filter-branch --env-filter '...' -- ${new_commits[*]}"
    echo "  (public-repo privacy gate #730 — see /t3:rules § public-repo commit author identity)"
    blocked=1
  fi

  # Commit messages and trailers reach public history exactly like file
  # content does (a `Co-authored-by:` line carrying an internal/customer
  # address is the canonical case), so they are scanned alongside the
  # patches rather than excluded the way `git diff` excludes them (#703).
  [ -n "${content}" ] || continue

  report=$(mktemp "${TMPDIR:-/tmp}/t3-privacy-gate.XXXXXX")
  scan_rc=0
  printf '%s\n' "${content}" | ${scan_cmd} - >"${report}" 2>&1 || scan_rc=$?
  if [ "${scan_rc}" -eq "${findings_code}" ]; then
    echo "✗ refuse: push to PUBLIC repo '${slug}' carries privacy findings on '${local_ref}'."
    echo "  Findings (line / category / redacted match):"
    # The scanner writes a deterministic plain-text summary to stdout
    # (captured here via 2>&1), so the user sees exactly which line/
    # category tripped the gate — not just a generic "carries findings".
    sed 's/^/  /' "${report}" 2>/dev/null || cat "${report}" 2>/dev/null || true
    echo "  Scrub the diff (generic placeholders) before pushing to a public repo."
    echo "  (public-repo privacy gate — see /t3:rules § Verify Repo Visibility Before Filing External Issues)"
    blocked=1
  elif [ "${scan_rc}" -ne 0 ]; then
    # Any other non-zero is a scanner failure (crash, missing script,
    # argparse error), NOT a finding. Fail OPEN — the gate is a safety net
    # layered on top of the retro/contribute privacy scan, and blocking
    # every push because the scanner itself broke is the over-deny lockout
    # this gate must not be (#126 gap 3). Warn so the failure is visible.
    echo "⚠ privacy scan could not run (exit ${scan_rc}) on '${local_ref}' — failing OPEN (push allowed)." >&2
    sed 's/^/  /' "${report}" 2>/dev/null || cat "${report}" 2>/dev/null || true
  fi
  rm -f "${report}"
done <<< "${refs_input}"

exit "${blocked}"
