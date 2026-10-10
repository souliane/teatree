#!/usr/bin/env bash
# Pre-push hook: public-repo privacy gate (#685, #730).
#
# Refuses `git push` when the `origin` remote resolves to a PUBLIC
# repository and the patch OR the message of any commit in the push
# range fails `scripts/privacy_scan.py` (a planted secret, an internal
# `/Users/`-`/home/` path, a private IP, an API token, an internal
# hostname, or a configured banned term). Commit messages and trailers reach
# public history just like file content, so they are scanned too (#703), and so
# does the pushed ref NAME — it is published the moment the push lands and
# survives branch deletion in `refs/pull/*`, so a later rename cannot unpublish it.
#
# It ALSO refuses (#730) when any commit in the push range has an author
# OR committer email that is not a GitHub noreply address. A real /
# deliverable address (e.g. a customer/personal-domain address
# inherited from local git config) in PUBLIC history is a permanent PII
# leak that GitHub's own "block pushes that expose my email" does not
# catch for third-party domains. The accepted shape is the GitHub
# noreply pattern `([0-9]+\+)?<login>@users.noreply.github.com`, which
# covers every GitHub identity (souliane and any other login)
# without hardcoding one specific login. That rule applies on every verdict
# that is not KNOWN private, an undetermined one included: an internal address
# bound for a remote nobody could classify is refused exactly as on a public
# one, and the refusal says how to confirm the visibility. A private remote
# passes because the resolver PROVES it private (the `private_repos` allowlist,
# or a forge probe), never because the gate stopped asking.
#
# No refusal ever recommends rewriting the pushed branch. The pushed branch
# is the reviewed branch; rewriting it moves every commit an open merge
# request's comments are anchored to. The remedy is a new branch cut clean
# of the finding, and a merge request opened on that.
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
# commits the remote does NOT already have — `<local-sha> --not <the remote's
# heads and the reported remote-sha>`, read from the remote itself and never from
# this clone's remote-tracking refs, which a pusher can write — never a single linear
# `<remote-sha>..<local-sha>` range (a merge-forward inflates it with the whole of
# `main`, #3523) and never `--not <origin/default>` alone (on a fork whose default
# branch is thousands of commits behind, that re-scans the entire already-public
# history on every push and turns the gate into an outage).
#
# Visibility is resolved by `teatree.hooks.repo_visibility_cli`, which answers
# from the offline `private_repos` allowlist first and only then probes, routing
# the probe by the remote's HOST (`gh` for GitHub, `glab` for GitLab) and
# host-caching the verdict per slug. The allowlist is what makes the verdict
# deterministic: a probe carries a network budget a loaded machine loses, and a
# lost probe used to decide a declared-private repo's visibility. The gate SKIPS the scan only when the
# remote is KNOWN to be private/internal. Every undetermined case — an
# unparsable remote, no forge CLI for that host, a probe error, or an
# unrecognised answer — fails CLOSED and the diff is scanned anyway, so a
# leak never rides out on a tool-less machine or an unparsable remote
# (§3f #14; was fail-open). A clean push on a machine without a forge CLI is
# unaffected: the scan passes it. A scan that cannot RUN (no Python 3.13+, a missing or
# crashing scanner, an unset registry) refuses the push as well: a gate that cannot
# look has not shown the push is clean. An allowlist the resolver could not READ (a locked, corrupt or
# absent config DB) is reported as ALLOWLIST_UNREADABLE, enforced the same way but
# named apart, so the refusal points at the store instead of at redeclaring a repo.
#
# The scanner and the visibility resolver run from THIS checkout's tree
# (`<repo>/scripts/privacy_scan.py`, `<repo>/src`), never from an installed copy, so a push
# that fixes the gate is judged by the fixed gate. The hook reads no override variable of its
# own. The term registry and the private_repos allowlist still resolve through the environment
# (TEATREE_TERM_REGISTRY, T3_CONFIG_DB, T3_DATA_DIR, XDG_DATA_HOME, HOME), so before it trusts any
# verdict, a private one included, the hook holds the lists it LOADED to the digests committed at this
# checkout's HEAD in .github/term-source-fingerprint.json (digests only, never printed) and refuses on a
# mismatch, on a fingerprint it cannot read, and on one that is missing, untracked or edited since. The
# PreToolUse guards also refuse an inline assignment of any of those names, or of
# SKIP/GIT_CONFIG_*/XDG_CONFIG_HOME, on a command that commits, pushes or runs prek or a t3 verb that
# pushes (src/teatree/hooks/git_bypass_detect.py, hooks/scripts/direct_command_guard.py). The push
# range is bounded by what the REMOTE holds (`git ls-remote --heads` at the URL git pushes to, and the
# sha git reports for the ref), never by this clone's remote-tracking refs, which anyone can write.
# A hook a pusher can skip by controlling its environment (HOME, SKIP, a hooks path) cannot be made
# to run from inside; the CI scan jobs banned-terms-tree and overlay-leak-tree are the backstop.
#
# Wired via prek in `.pre-commit-config.yaml` (stages: [push]) so it
# ships with the repo and needs no per-machine bootstrap.
set -euo pipefail

ZERO="0000000000000000000000000000000000000000"
# What git passes as arguments wins over what a runner exports: prek passes none and sets the PRE_COMMIT_* variables
# itself, while an exported one must not redirect a hook git ran natively.
remote_name="${1:-${PRE_COMMIT_REMOTE_NAME:-origin}}"
remote_url="${2:-${PRE_COMMIT_REMOTE_URL:-}}"

if [ -z "${remote_url}" ]; then
  remote_url=$(git remote get-url "${remote_name}" 2>/dev/null || true)
fi
[ -n "${remote_url}" ] || exit 0  # no remote URL — nothing to gate

# Human-readable repo label for the messages only — NOT the probe key (the
# ssh-shape example below carries the inline allow-annotation so this hook's
# own header does not self-trip the privacy gate it powers):
#   https://github.com/owner/repo(.git)
#   git@github.com:owner/repo(.git)  # privacy-scan:allow doc example
slug=$(printf '%s' "${remote_url}" \
  | sed -E 's#^[^:]+://[^/]+/##; s#^git@[^:]+:##; s#\.git$##')

# Suggest a host-qualified entry. A dotless host needs its canonical HostName;
# without one, show the expected form instead of suggesting a value that cannot match.
entry_slug=$(printf '%s' "${remote_url}" \
  | sed -E 's#^[^:]+://([^@/]*@)?([^/:]+)(:[0-9]+)?/#\2/#; s#^([^/:@]+@)?([^/:]+):#\2/#; s#\.git$##')
entry_host="${entry_slug%%/*}"
if [ "${entry_host}" != "${entry_slug}" ] && [[ "${entry_host}" != *.* ]] && [ "${entry_host}" != "localhost" ]; then
  entry_slug=""
fi

# Resolve the repo root from this script's own location
# (scripts/hooks/<this>.sh -> repo root) so the visibility CLI runs against
# THIS clone's teatree regardless of the caller's cwd.
script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

# The FULL remote URL is passed (never the host-stripped `slug`) because the
# host segment is what selects the forge tool. Shelling `gh repo view` here
# hard-coded ONE forge: a gitlab.com remote errored on every push, so
# visibility was permanently undetermined and this gate re-scanned a PRIVATE
# repo forever.
#
# The interpreter fallback offers core's package root in BOTH supported layouts —
# `<root>/src/teatree` in a standalone checkout, `<root>/vendor/teatree/src/teatree`
# in a fork that vendors core — and tries version-explicit interpreters before a
# bare `python3`, which on a stock Mac is the Command Line Tools 3.9 stub and
# cannot import core (requires-python >=3.13) whatever PYTHONPATH says.
#
# The uv branch gets the same PYTHONPATH. `uv run --project <root> --no-sync` in a
# checkout whose project env does not carry core (a fresh worktree, where uv creates
# an EMPTY .venv on the spot, or a vendoring fork whose env never installed it) runs
# a perfectly good 3.13+ interpreter that cannot import `teatree`; with the bare
# `python3` being the Mac stub, the same allowlisted remote then resolved PRIVATE in
# one worktree and UNKNOWN in the next. Core's import chain here is stdlib-only, so
# the path is all that interpreter lacks.
#
# A probe failure is REPORTED, never swallowed. Discarding it is precisely how a
# PRIVATE repo gets silently downgraded to "assume public" and re-scanned on
# every push: the verdict is undetermined, the gate says only "could not
# confirm", and the REASON it could not confirm is invisible forever. A resolver
# that could not run at all says so (RESOLVER_FAILED) rather than posing as an
# answer, because its remedy is the checkout, not the repo's declared visibility.
_resolve_visibility() {
  local err py probe_path out="" rc=1
  err=$(mktemp "${TMPDIR:-/tmp}/t3-visibility-probe.XXXXXX" 2>/dev/null) || err=/dev/null
  probe_path="${repo_root}/src:${repo_root}/vendor/teatree/src"

  if command -v uv >/dev/null 2>&1; then
    if out=$(PYTHONPATH="${probe_path}" uv run --project "${repo_root}" --no-sync \
        python -m teatree.hooks.repo_visibility_cli "${remote_url}" 2>>"${err}"); then
      rc=0
    fi
  fi

  if [ "${rc}" -ne 0 ]; then
    for py in python3.13 python3.14 python3; do
      command -v "${py}" >/dev/null 2>&1 || continue
      if out=$(PYTHONPATH="${probe_path}" \
          "${py}" -m teatree.hooks.repo_visibility_cli "${remote_url}" 2>>"${err}"); then
        rc=0
        break
      fi
    done
  fi

  if [ "${rc}" -ne 0 ]; then
    out="RESOLVER_FAILED"  # no verdict, whatever a failed attempt printed first
  fi
  if [ "${rc}" -ne 0 ] && [ -s "${err}" ]; then
    echo "⚠ push privacy gate: visibility probe failed — diagnostics below." >&2
    sed 's/^/    /' "${err}" >&2 2>/dev/null || true
  elif [ "${out}" = "ALLOWLIST_UNREADABLE" ]; then
    # The resolver names WHICH store it could not read; that path is the whole diagnosis.
    grep '^could not read private_repos' "${err}" 2>/dev/null | sed 's/^/    /' >&2 || true
  fi
  [ "${err}" = /dev/null ] || rm -f "${err}"
  printf '%s\n' "${out}"
  return "${rc}"
}

visibility=$(_resolve_visibility || true)
# Normalise (PUBLIC/PRIVATE/INTERNAL/UNKNOWN); anything else is undetermined.
visibility=$(printf '%s' "${visibility}" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')

# Both rules below run on every verdict that is not KNOWN private. An undetermined
# verdict is "could not confirm private", so it is enforced like a public one
# (hooks/CLAUDE.md: public-surface leak gates fail CLOSED always); only the wording
# differs, so no refusal calls a repo PUBLIC on a verdict that did not say so.
known_private=0
case "${visibility}" in
  PRIVATE | INTERNAL)
    # KNOWN non-public remote — nothing reaches public history. The verdict comes from the same
    # environment-resolved store as the term registry, so it is only trusted once that registry is verified below.
    known_private=1
    target="private repo '${slug}'"
    confirm_hint=""
    ;;
  PUBLIC)
    target="PUBLIC repo '${slug}'"
    confirm_hint=""
    ;;
  RESOLVER_FAILED)
    echo "⚠ push privacy gate: could not run the visibility resolver for '${slug:-<remote>}' (diagnostics above) — scanning the diff and checking commit identities anyway (fail closed, §3f #14)." >&2
    target="repo '${slug:-<remote>}' (could not run the visibility resolver — visibility unknown, refusing push)"
    confirm_hint="  The visibility resolver could not start in this checkout (diagnostics above), so neither private_repos nor the forge was asked. Run 'uv sync' in ${repo_root}, or install Python 3.13+, and push again."
    ;;
  ALLOWLIST_UNREADABLE)
    # Not "undeclared": the store that would say whether it is declared could not be
    # read, so the hint below sends the operator to that store, not to redeclaring.
    echo "⚠ push privacy gate: could not read private_repos, and no forge probe confirmed '${slug:-<remote>}' — scanning the diff and checking commit identities anyway (fail closed, §3f #14)." >&2
    target="repo '${slug:-<remote>}' (could not read private_repos — visibility unknown, refusing push)"
    confirm_hint="  The private_repos allowlist could not be read from the config DB this hook resolves (the path is printed above). Repair or unlock that store (t3 doctor check names it); redeclaring the repo does not help while the store is unreadable."
    ;;
  *)
    # Undetermined visibility (unparsable remote, no forge CLI for that host,
    # a probe error, or an unrecognised answer). Fail CLOSED: scan anyway. A clean
    # push on a tool-less machine still passes; a leak, or a scan that cannot run,
    # is stopped. Warn loudly so the undetermined path shows.
    echo "⚠ push privacy gate: could not confirm '${slug:-<remote>}' visibility (no forge CLI for this host, or an unrecognised answer) — scanning the diff and checking commit identities anyway (fail closed, §3f #14)." >&2
    target="repo '${slug:-<remote>}' (visibility could not be confirmed)"
    confirm_hint="  To confirm visibility: authenticate this host's forge CLI (gh auth login / glab auth login), or declare the repo private with: t3 <overlay> config_setting set private_repos '[\"${entry_slug:-<host>/<owner>/<repo>}\"]'"
    ;;
esac

scanner="${repo_root}/scripts/privacy_scan.py"
scan_pythonpath="${repo_root}/src:${repo_root}/vendor/teatree/src"
# The dedicated "findings present" exit code of scripts/privacy_scan.py. Any other
# non-zero exit is a scan that could not run, and refuses the push as well.
findings_code=3

# A Python 3.13+ to run the stdlib-only scanner. Every PATH match of a version-explicit name is
# tried before the next name, because a bare `python3` on a stock Mac is the Command Line Tools
# 3.9 stub and a real interpreter may sit behind it. `uv python find` is the last resort for a
# host whose only 3.13+ is a uv-managed one.
_find_python() {
  local name candidate
  for name in python3.13 python3.14 python3 python; do
    while IFS= read -r candidate; do
      if "${candidate}" -S -c 'import sys; sys.exit(sys.version_info < (3, 13))' >/dev/null 2>&1; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    done < <(type -aP "${name}" 2>/dev/null)
  done
  command -v uv >/dev/null 2>&1 || return 1
  candidate=$(uv python find '>=3.13' 2>/dev/null) || return 1
  [ -n "${candidate}" ] || return 1
  printf '%s\n' "${candidate}"
}

# `-S`: no site-packages, no `.pth` file and no `sitecustomize` runs before the scanner does,
# and PYTHONPATH is this checkout's own source only, so nothing outside the tree can stand in
# for the gate or end it early. The scanner imports the standard library and `teatree` alone.
_scan() {
  PYTHONPATH="${scan_pythonpath}" "${scan_py}" -S "${scanner}" "$@"
}

scan_py=$(_find_python || true)
if [ ! -f "${scanner}" ] || [ -z "${scan_py}" ]; then
  echo "✗ refuse: push to ${target} cannot be checked: the privacy scan could not start (fail closed)."
  [ -f "${scanner}" ] || echo "  ${scanner} is missing from this checkout."
  [ -n "${scan_py}" ] || echo "  No Python 3.13+ interpreter was found on PATH (python3.13, python3.14, python3, python, uv)."
  echo "  Nothing was published. Install Python 3.13+ (or uv) and restore this checkout's scripts/, then push again."
  [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
  exit 1
fi

# The term registry and the private_repos verdict resolve through the environment, which a pusher can fill with
# a store of their own in ways no PreToolUse guard sees (`export`, `printf -v`, `read`, `source`, a script setting
# os.environ). So the lists this hook LOADED are held to the digests committed at this checkout's HEAD, which the
# required term-source-drift CI job holds equal to the real secret. Only the fingerprint as committed counts: a file
# that is missing, untracked, edited since, or unreadable is refused like a mismatch. No digest and no term is printed.
fingerprint_rel=".github/term-source-fingerprint.json"
_checkout_git() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY \
    -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_NAMESPACE -u GIT_PREFIX -u GIT_CEILING_DIRECTORIES \
    git -C "${repo_root}" "$@" </dev/null
}
_fingerprint_is_committed() {
  local at_head on_disk
  at_head=$(_checkout_git rev-parse --verify --quiet "HEAD:${fingerprint_rel}" 2>/dev/null) || return 1
  on_disk=$(_checkout_git hash-object -- "${repo_root}/${fingerprint_rel}" 2>/dev/null) || return 1
  [ -n "${at_head}" ] && [ "${at_head}" = "${on_disk}" ]
}

registry_rc=0
registry_report=""
if ! _fingerprint_is_committed; then
  registry_rc=126
else
  registry_report=$(_scan --verify-registry "${repo_root}/${fingerprint_rel}" 2>&1 </dev/null) || registry_rc=$?
fi
if [ "${registry_rc}" -ne 0 ]; then
  echo "✗ refuse: push to ${target} cannot be checked: the term registry this hook loaded could not be verified (fail closed)."
  if [ "${registry_rc}" -eq 126 ]; then
    echo "  ${fingerprint_rel} is not committed unchanged at HEAD of ${repo_root}, so there is nothing to hold the registry to."
  else
    printf '%s\n' "${registry_report}" | sed 's/^/  /'
    if [ "${registry_rc}" -eq 5 ]; then
      echo "  A registry of the caller's choosing (an exported term registry or config store, a redirected data dir or HOME) is not"
      echo "  the one whose digests this checkout commits. If the owner's registry itself changed, the owner resyncs the CI secret and"
      echo "  this file together (scripts/term_source_drift.py sync --apply) and the refreshed file is committed."
    fi
  fi
  echo "  Nothing was published."
  [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
  exit 1
fi
[ "${known_private}" = 0 ] || exit 0

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

# What the remote already holds, as commit shas this clone has (src/teatree/hooks/remote_holds.py, which `t3 fast-push`
# shares): the tip of every head the REMOTE lists at the URL git is pushing to, plus the sha git reports for each ref
# being updated. Never a local remote-tracking ref: those are files the pusher can write (`update-ref`, `packed-refs`,
# `git fetch . HEAD:refs/remotes/...`, a `set-url` + `fetch` from another repository), and one that says the remote
# holds the leaking commit empties the range. A listing that would not come from the place the push goes (a
# `url.<base>.insteadOf` aimed elsewhere with an identity `pushInsteadOf`) or that fails counts for nothing: the range
# gets wider, never narrower. On the prek synthesized path the reported sha is a DERIVED `PRE_COMMIT_FROM_REF` (prek
# computes it from this clone's remote-tracking refs), so it is not offered.
reported_shas=()
if [ "${synthesized}" != "1" ]; then
  while read -r _ _ _ reported_sha; do
    if [ -n "${reported_sha:-}" ] && [ "${reported_sha}" != "${ZERO}" ]; then
      reported_shas+=("${reported_sha}")
    fi
  done <<< "${refs_input}"
fi
tips_rc=0
tips_out=$(PYTHONPATH="${scan_pythonpath}" "${scan_py}" -S -m teatree.hooks.remote_holds \
  "${remote_url}" "${remote_name}" ${reported_shas[@]+"${reported_shas[@]}"} </dev/null) || tips_rc=$?
if [ "${tips_rc}" -ne 0 ]; then
  echo "⚠ push privacy gate: could not determine what the remote holds (exit ${tips_rc}) — scanning every commit reachable from the pushed sha (wider, never narrower)." >&2
  tips_out=""
fi
public_tips=()
while IFS= read -r tip; do
  [ -z "${tip}" ] || public_tips+=("${tip}")
done <<< "${tips_out}"

# Text a refusal has to show (an identity, a ref name) goes through here: when it is, or holds, a
# registry term it is shown as that term's `class#index`, never by value. A check that cannot run
# withholds the text, because it could be a term.
_display() {
  local text="$1" out refs rc=0
  out=$(printf '%s\n' "${text}" | _scan --text --terms-only - 2>&1) || rc=$?
  if [ "${rc}" -eq 0 ]; then
    printf '%s\n' "${text}"
    return 0
  fi
  refs=""
  [ "${rc}" -ne "${findings_code}" ] || refs=$(printf '%s\n' "${out}" | sed -n 's/.*banned_term: //p' | paste -sd, -)
  if [ -n "${refs}" ]; then
    printf '(registry term %s)\n' "${refs}"
  else
    printf '(withheld: it holds an id or the term check could not run)\n'
  fi
}

# Messages of the commits named by the `git log` args, scanned apart from any patch.
# A message is published verbatim, never a diff, so it goes to the scanner on stdin in
# text mode: no file is written, so no temp directory can be missing or unwritable.
# `git log` gets a closed stdin: every caller sits in a `while read` loop it must not drain.
_scan_messages_as_text() {
  git log --format='%B' "$@" 2>/dev/null </dev/null | _scan --text -
}

# On a blocked push, re-scan the newly-public commits ONE AT A TIME — and each
# changed file within a commit — so the refusal names the exact commit + file a
# finding lives in. Without this the finding's line number is only an offset into
# a concatenated multi-commit blob, unlocatable in public history; an operator who
# cannot find the reported finding learns to distrust (and bypass) the gate. Only
# the RARE blocked path pays this per-commit cost: a clean push is decided by the
# whole-range message and patch scans and never enters here. Args are the pushed tip commit, then
# the `<sha> --not <public-tips>` range; uses the global ${findings_code}.
# Returns 0 if it attributed at least one finding, 1 if none (caller then prints the raw report).
_attribute_findings() {
  local tip="$1"
  shift
  local printed=1
  local sha where f sub sub_rc
  while read -r sha; do
    [ -n "${sha}" ] || continue
    where="commit ${sha}"
    # A fix-up at the tip leaves this commit's finding in public history all the same.
    if [ -n "${tip}" ] && [ "${sha}" != "${tip}" ]; then
      where="${where} (earlier than the pushed tip)"
    fi
    # A finding that lives only in the commit MESSAGE (a Co-authored-by line, a
    # banned term in the subject) has no file, so scan the message on its own.
    sub_rc=0
    sub=$(_scan_messages_as_text -1 "${sha}" 2>&1) || sub_rc=$?
    if [ "${sub_rc}" -eq "${findings_code}" ]; then
      echo "  ${where} (commit message):"
      printf '%s\n' "${sub}" | sed 's/^/    /'
      printed=0
    fi
    # Then each changed file's patch in this commit, named individually.
    while read -r f; do
      [ -n "${f}" ] || continue
      sub_rc=0
      sub=$(git show --format= --patch --cc "${sha}" -- "${f}" 2>/dev/null </dev/null | _scan - 2>&1) || sub_rc=$?
      if [ "${sub_rc}" -eq "${findings_code}" ]; then
        echo "  ${where} file ${f}:"
        printf '%s\n' "${sub}" | sed 's/^/    /'
        printed=0
      fi
    done < <(git show --no-commit-id --name-only --format= "${sha}" 2>/dev/null)
  done < <(git rev-list --reverse "$@" 2>/dev/null)
  return "${printed}"
}

# The pushed ref's REMOTE name is published the moment the push lands, and a
# forge keeps it under refs/pull/* after the branch is deleted, so it is scanned
# like content. The local name is never sent, so it is not judged.
_scan_ref_name() {
  local published="$1" name_report name_rc=0
  published="${published#refs/*/}"
  [ -n "${published}" ] || return 0
  name_report=$(printf '%s\n' "${published}" | _scan --text - 2>&1) || name_rc=$?
  if [ "${name_rc}" -eq "${findings_code}" ]; then
    echo "✗ refuse: push to ${target} publishes a ref name that carries privacy findings."
    printf '%s\n' "${name_report}" | sed 's/^/    /'
    echo "  A pushed ref name is permanent: the forge keeps it under refs/pull/* even after the branch is deleted."
    echo "  Push under a clean remote name instead: git push <remote> HEAD:refs/heads/<clean-name>"
    [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
    return 1
  elif [ "${name_rc}" -ne 0 ]; then
    echo "✗ refuse: the ref-name privacy scan could not run (exit ${name_rc}) — refusing (fail closed)."
    printf '%s\n' "${name_report}" | sed 's/^/    /'
    return 1
  fi
  return 0
}

# An annotated tag is published with its own message and tagger line, which no commit range
# contains, so the tag object is read for registry terms and opaque ids as well. A ref that
# does not point at a tag object has nothing more to read.
_scan_tag_object() {
  local sha="$1" tag_report tag_rc=0
  [ "$(git cat-file -t "${sha}" 2>/dev/null || true)" = "tag" ] || return 0
  tag_report=$(git cat-file -p "${sha}" 2>/dev/null </dev/null | _scan --text --terms-only - 2>&1) || tag_rc=$?
  [ "${tag_rc}" -ne 0 ] || return 0
  if [ "${tag_rc}" -eq "${findings_code}" ]; then
    echo "✗ refuse: push to ${target} publishes an annotated tag whose message or tagger line carries privacy findings."
  else
    echo "✗ refuse: the annotated-tag privacy scan could not run (exit ${tag_rc}) — refusing (fail closed)."
  fi
  printf '%s\n' "${tag_report}" | sed 's/^/    /'
  [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
  return 1
}

blocked=0
while read -r local_ref local_sha remote_ref _; do
  [ -n "${local_sha:-}" ] || continue
  [ "${local_sha}" != "${ZERO}" ] || continue  # branch deletion — skip

  _scan_ref_name "${remote_ref:-${local_ref}}" || blocked=1
  _scan_tag_object "${local_sha}" || blocked=1

  # The push newly exposes the commits reachable from the pushed sha but from NO commit the remote already
  # holds (`public_tips` above: read from the remote, never from this clone's remote-tracking refs).
  #
  # READ THIS BEFORE "improving" it back to `--not origin/<default>`. That
  # looks more thorough and is not: it is the MERGE-REQUEST range, not the
  # PUSH range. They are different questions. An MR's range is legitimately
  # everything the branch adds to the default branch — on a vendored fork's
  # import branch that is thousands of commits, and reviewing them is the
  # MR's job. A PUSH only ever exposes what the remote does not already
  # have. Using the MR range as the push range re-scans the whole MR on
  # every push, so a 3-commit push scans 2276 commits: minutes of CPU, and —
  # worse — it can never pass, because somewhere in a history that large the
  # scanner always finds something, and every one of those hits is in
  # already-pushed, immutable, already-remote content that this push is not
  # exposing. A gate that cannot pass is not a strict gate; it is an outage,
  # and the leak it lets through is the one in the commit nobody could push
  # (#3523). For the same reason it is never the single linear
  # `<remote-sha>..<local-sha>` range: a branch that merged `main` forward carries all of
  # `main`'s newer commits in that range, and the remote's own heads subtract them.
  #
  # A branch with no upstream yet (a first push) needs no special case: the remote's OTHER heads are still
  # subtracted, so a new branch off an already-pushed base scans only its own new commits, not all of history.

  # Nothing the remote is known to hold (a brand-new repo, a remote that cannot be listed): fail CLOSED by
  # subtracting nothing, so the whole history reachable from the pushed sha is scanned — wider, never narrower.
  new_commits=("${local_sha}")
  if [ ${#public_tips[@]} -gt 0 ]; then
    new_commits+=("--not" "${public_tips[@]}")
  fi

  # Author / committer email is metadata `git diff` and `%B` never show,
  # yet it lands in public history forever. On a PUBLIC remote every
  # commit's author AND committer email must be a GitHub noreply address
  # (`([0-9]+\+)?<login>@users.noreply.github.com`); anything else — a
  # real/deliverable address such as a customer-domain email inherited
  # from local git config — is blocked (#730).
  #
  # Every verdict that is not KNOWN private, the undetermined one included. An
  # internal address is the ordinary identity on a private remote, which is why a
  # PROVEN-private remote exits above; a remote nobody could classify may be
  # public, and an address that reaches public history cannot be taken back.
  noreply_re='^([0-9]+\+)?[A-Za-z0-9-]+@users\.noreply\.github\.com$'
  bad_idents=$(git log --format='%ae%n%ce' "${new_commits[@]}" 2>/dev/null \
    | grep -v -E "${noreply_re}" | sort -u || true)
  if [ -n "${bad_idents}" ]; then
    echo "✗ refuse: push to ${target} has a non-noreply commit identity on '$(_display "${local_ref}")'."
    echo "  A real/deliverable email in public git history is a permanent PII leak."
    echo "  Offending author/committer email(s):"
    while IFS= read -r ident; do
      printf '    %s\n' "$(_display "${ident}")"
    done <<< "${bad_idents}"
    echo "  Allowed shape: <id>+<login>@users.noreply.github.com (GitHub noreply)."
    echo "  Nothing here has reached the remote yet. Start a new branch from the remote base,"
    echo "  re-apply the work under the noreply identity, and open a merge request on that branch."
    echo "  Never rewrite a branch that already backs an open merge request: it moves every commit"
    echo "  the reviewers' comments are anchored to, and the whole review has to start again."
    echo "  (public-repo privacy gate #730 — see /t3:rules § public-repo commit author identity)"
    [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
    blocked=1
  fi

  # Commit messages and trailers reach public history exactly like file
  # content does (a `Co-authored-by:` line carrying an internal/customer
  # address is the canonical case), so they are scanned as well as the
  # patches rather than excluded the way `git diff` excludes them (#703).
  # The patches alone carry no commit header, so the author/committer emails
  # above are judged only by the noreply guard and never by the scanner's
  # generic email matcher; `--cc` keeps a merge's conflict resolutions — content
  # in neither parent — in scope while leaving the already-public content the merge
  # carried over out of it. A `git log` that fails is a scan that did not happen.
  message_rc=0
  report=$(_scan_messages_as_text "${new_commits[@]}" 2>&1) || message_rc=$?
  patch_rc=0
  patch_report=$(git log --format= --patch --cc "${new_commits[@]}" 2>/dev/null </dev/null | _scan - 2>&1) || patch_rc=$?
  report="${report}${patch_report:+$'\n'}${patch_report}"
  scan_rc=$((message_rc != 0 ? message_rc : patch_rc))
  if [ "${message_rc}" -eq "${findings_code}" ] || [ "${patch_rc}" -eq "${findings_code}" ]; then
    echo "✗ refuse: push to ${target} carries privacy findings on '$(_display "${local_ref}")'."
    echo "  Findings by commit + file (locate each in the pushed history and scrub it):"
    # Attribute each finding to its commit + file so the operator can find it.
    # Fall back to the whole-range report (a deterministic per-line summary the
    # scanner writes to stdout, captured via 2>&1) only if attribution found
    # nothing to pin — e.g. a merge combined-diff finding — so a finding is
    # never hidden behind an empty locatable list.
    pushed_tip=$(git rev-parse --verify --quiet "${local_sha}^{commit}" 2>/dev/null || true)
    if ! _attribute_findings "${pushed_tip}" "${new_commits[@]}"; then
      printf '%s\n' "${report}" | sed 's/^/  /'
    fi
    echo "  Every commit this push publishes is scanned on its own — patch and message — never just the tip"
    echo "  or the net diff, so a later commit that deletes or annotates a flagged line does not clear it."
    echo "  Nothing was published. Cut a new branch from the remote's tip, re-create the unpushed commits on it"
    echo "  with generic placeholders so the flagged value is in none of them, and push that branch."
    echo "  A deliberate fake value (a test-fixture email, path, address or key) instead carries an inline"
    echo "  'privacy-scan:allow <reason>' marker on that same line, in the commit that introduces it. The marker"
    echo "  never silences a registry term, and 'leak-scan:allow' is the only marker that silences an opaque id."
    echo "  Never rewrite a branch that already backs an open merge request: it moves every commit"
    echo "  the reviewers' comments are anchored to, and the whole review has to start again."
    echo "  (public-repo privacy gate — see /t3:rules § Verify Repo Visibility Before Filing External Issues)"
    [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
    blocked=1
  elif [ "${scan_rc}" -ne 0 ]; then
    # Any other non-zero is a scan that could not run (crash, unset registry, usage error,
    # a `git log` that failed). Nothing was shown to be clean, so the push is refused.
    echo "✗ refuse: push to ${target} cannot be checked: the privacy scan could not run (exit ${scan_rc}) on '$(_display "${local_ref}")' (fail closed)."
    printf '%s\n' "${report}" | sed 's/^/  /'
    echo "  Nothing was published. Fix the scanner or its registry, then push again."
    [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
    blocked=1
  fi
done <<< "${refs_input}"

exit "${blocked}"
