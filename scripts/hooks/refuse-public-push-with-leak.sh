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
# passes because the forge's own answer, asked now, says it is private, never
# because the gate stopped asking.
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
# Visibility is resolved by `teatree.hooks.repo_visibility_cli --live`, which asks the
# forge now, routing the probe by the remote's HOST (`gh` for GitHub, `glab` for GitLab).
# It reads neither the day cache nor the `private_repos` allowlist: both are files a
# pusher can write (the allowlist in a store T3_CONFIG_DB or T3_DATA_DIR points
# anywhere), so a PRIVATE they hold would skip a leak scan on nothing but the pusher's
# word. The gate SKIPS the scan only when the forge itself says the remote is
# private/internal. Every undetermined case — an unparsable remote, no forge CLI for
# that host, a probe error, or an unrecognised answer — fails CLOSED and the diff is
# scanned anyway, so a leak never rides out on a tool-less machine or an unparsable
# remote (§3f #14; was fail-open). A clean push on a machine without a forge CLI is
# unaffected: the scan passes it. A scan that cannot RUN (no Python 3.13+, a missing or
# crashing scanner, an unset registry) refuses the push as well: a gate that cannot
# look has not shown the push is clean.
#
# The scanner and the visibility resolver run from THIS checkout's tree
# (`<repo>/scripts/privacy_scan.py`, `<repo>/src`), never from an installed copy, so a push
# that fixes the gate is judged by the fixed gate. The hook reads no override variable of its
# own. The term registry still resolves through the environment
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

# Resolve the repo root from this script's own location
# (scripts/hooks/<this>.sh -> repo root) so the visibility CLI runs against
# THIS clone's teatree regardless of the caller's cwd.
script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

# Repo-local git state changes what git hands this hook without touching a tracked file or the environment: a replace
# ref (`git replace`) makes git show another object under the same sha, a smudge filter or a textconv driver in
# `.git/config` and `.git/info/attributes` rewrites a file's bytes as git writes or diffs them, and `i18n.logOutputEncoding`
# re-encodes a message. So the hook reads history unreplaced, copies sources as raw blobs, and asks `git log`/`git show`
# for the unconverted text in UTF-8; what no flag reaches (a file marked `-diff` or binary, shown as "Binary files differ")
# is a named residual of the push hook.
export GIT_NO_REPLACE_OBJECTS=1
git_read_flags=(--no-textconv --no-ext-diff --no-color)

# git at THIS checkout, blind to the variables that relocate a repository or its object store (a fleet claim push
# exports a scratch object directory, and a hook run from another repository would otherwise read that one).
_checkout_git() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY \
    -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_NAMESPACE -u GIT_PREFIX -u GIT_CEILING_DIRECTORIES \
    git -C "${repo_root}" "$@"
}

# Prek may invoke this hook from a vendored directory; git diff.relative must not narrow the pushed history.
if pushed_top=$(git rev-parse --show-toplevel 2>/dev/null); then
  cd "${pushed_top}"
fi

# The hook runs the scanner, the visibility resolver, `remote_holds` and `commit_objects` from this working tree, and
# everything they import. An uncommitted edit of any of them (a scanner that reports nothing, a check that always
# verifies, a resolver that always answers private) would be the pusher's word that the push is clean, so the files
# named by the list committed at HEAD must hash to their HEAD blobs. Hashing, not `git diff`: `--assume-unchanged` and
# `--skip-worktree` make git stop looking at an edited file. Prints the differing paths, and only paths, never content.
sources_list_rel="scripts/hooks/leak-gate-sources.txt"
_listed_sources() {
  { _checkout_git show "HEAD:./${sources_list_rel}" 2>/dev/null </dev/null || true; } | grep -v '^$' | LC_ALL=C sort -u || true
}
_sources_differing_from_head() {
  local paths path want hashes have
  local -a pathspec=()
  paths=$(_listed_sources)
  if [ -z "${paths}" ]; then
    printf '%s\n' "${sources_list_rel}"
    return 0
  fi
  while IFS= read -r path; do
    pathspec+=("${path}")
  done <<< "${paths}"
  want=$(_checkout_git ls-tree HEAD -- "${pathspec[@]}" 2>/dev/null </dev/null \
    | awk -F'\t' '{ split($1, mode, " "); print mode[3] "\t" $2 }' || true)
  if ! hashes=$(_checkout_git hash-object -- "${pathspec[@]}" 2>/dev/null </dev/null); then
    printf '%s\n' "${paths}"
    return 0
  fi
  have=$(paste <(printf '%s\n' "${hashes}") <(printf '%s\n' "${paths}"))
  {
    printf '%s\n%s\n' "${want}" "${have}" | LC_ALL=C sort | uniq -u | awk -F'\t' 'NF { print $(NF) }'
    _import_shadows "${paths}"
  } | LC_ALL=C sort -u
}

# Python imports an extension module (`x.so`, `x.pyd`) or a package directory (`x/`) before the module `x.py`, and neither
# is hashed above, so an untracked one beside a listed module would run in its place. Prints what shadows, by path.
_import_shadows() {
  local dir path found
  while IFS= read -r dir; do
    while IFS= read -r found; do
      [ -z "${found}" ] || printf '%s\n' "${found#"${repo_root}"/}"
    done < <(find "${repo_root}/${dir}" -maxdepth 1 \( -name '*.so' -o -name '*.pyd' \) 2>/dev/null)
  done < <(printf '%s\n' "$1" | sed 's#/[^/]*$##' | LC_ALL=C sort -u)
  while IFS= read -r path; do
    case "${path}" in
      */__init__.py) ;;
      *.py) [ ! -d "${repo_root}/${path%.py}" ] || printf '%s/\n' "${path%.py}" ;;
    esac
  done <<< "$1"
}

differing_sources=$(_sources_differing_from_head)
if [ -n "${differing_sources}" ]; then
  differing_count=$(printf '%s\n' "${differing_sources}" | wc -l | tr -d ' ')
  echo "✗ refuse: push to '${slug:-<remote>}' cannot be checked: ${differing_count} file(s) this hook runs differ from HEAD of ${repo_root}, are not committed there, or are shadowed by an extension module or package that is not:"
  printf '%s\n' "${differing_sources}" | head -5 | sed 's/^/    /'
  [ "${differing_count}" -le 5 ] || echo "    ... and $((differing_count - 5)) more"
  echo "  The push gate runs only committed code: an uncommitted edit of it would be the pusher's word that the push is clean."
  echo "  Commit or restore the file(s), then push again. Nothing was published."
  exit 1
fi

# Nothing is run from the checkout. Its directories are searched by Python before the standard library (the script's own
# directory, `PYTHONPATH`, and for `-m` the working directory, which is the repo being pushed), so an untracked
# `argparse.py` or `dataclasses.py` anywhere there would run in place of the real module, and a planted
# `__pycache__/*.pyc` in place of a verified source. The files the list names are therefore copied out of HEAD into a
# directory of this hook's own, removed on exit, and every Python this hook starts runs from that copy with `-P` (no
# script directory, no working directory on `sys.path`) and `-S` (no site) and writes no bytecode. The checks above stay:
# they give a clear refusal for an edit, where the copy alone would silently ignore it.
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/t3-gate.XXXXXX" 2>/dev/null) || work_dir=""
if [ -z "${work_dir}" ]; then
  echo "✗ refuse: push to '${slug:-<remote>}' cannot be checked: a directory for the committed sources could not be made under ${TMPDIR:-/tmp} (fail closed). Nothing was published."
  exit 1
fi
trap 'rm -rf "${work_dir}"' EXIT
gate_tree="${work_dir}/tree"
mkdir "${gate_tree}"
while IFS= read -r source_path; do
  mkdir -p "${gate_tree}/${source_path%/*}"
  # The raw blob, not `git archive`: archive runs the repo-local smudge filter over every file it writes.
  if ! _checkout_git cat-file blob "HEAD:./${source_path}" > "${gate_tree}/${source_path}" 2>/dev/null </dev/null; then
    echo "✗ refuse: push to '${slug:-<remote>}' cannot be checked: ${source_path} could not be copied out of HEAD of ${repo_root} (fail closed). Nothing was published."
    exit 1
  fi
done < <(_listed_sources)
export PYTHONDONTWRITEBYTECODE=1

scanner="${gate_tree}/scripts/privacy_scan.py"
scan_pythonpath="${gate_tree}/src:${gate_tree}/vendor/teatree/src"
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
      if "${candidate}" -P -S -c 'import sys; sys.exit(sys.version_info < (3, 13))' >/dev/null 2>&1; then
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

# `-P -S`: no script or working directory on `sys.path`, no site-packages, no `.pth` file and no `sitecustomize` runs
# before the scanner does, and PYTHONPATH is the copy of the committed sources alone, so nothing outside it can stand
# in for the gate or end it early. The scanner imports the standard library and `teatree` alone.
_scan() {
  PYTHONPATH="${scan_pythonpath}" "${scan_py}" -P -S "${scanner}" "$@"
}

scan_py=$(_find_python || true)

# The resolver starts like the scanner (`-P -S` over the committed copy, in both layouts) and gets the FULL remote URL:
# the host segment of it, which `slug` drops, is what selects the forge tool.
#
# A probe failure is REPORTED, never swallowed. Discarding it is precisely how a
# PRIVATE repo gets silently downgraded to "assume public" and re-scanned on
# every push: the verdict is undetermined, the gate says only "could not
# confirm", and the REASON it could not confirm is invisible forever. A resolver
# that could not run at all says so (RESOLVER_FAILED) rather than posing as an
# answer, because its remedy is the checkout, not the repo's declared visibility.
_resolve_visibility() {
  local err out="" rc=1
  err=$(mktemp "${TMPDIR:-/tmp}/t3-visibility-probe.XXXXXX" 2>/dev/null) || err=/dev/null

  if [ -n "${scan_py}" ]; then
    if out=$(PYTHONPATH="${scan_pythonpath}" \
        "${scan_py}" -P -S -m teatree.hooks.repo_visibility_cli --live "${remote_url}" 2>>"${err}" </dev/null); then
      rc=0
    fi
  fi

  if [ "${rc}" -ne 0 ]; then
    out="RESOLVER_FAILED"  # no verdict, whatever a failed attempt printed first
  fi
  if [ "${rc}" -ne 0 ] && [ -s "${err}" ]; then
    echo "⚠ push privacy gate: visibility probe failed — diagnostics below." >&2
    sed 's/^/    /' "${err}" >&2 2>/dev/null || true
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
    confirm_hint="  The visibility resolver could not start in this checkout (diagnostics above), so the forge was not asked. Install Python 3.13+, or restore this checkout's src/, and push again."
    ;;
  *)
    # Undetermined visibility (unparsable remote, no forge CLI for that host,
    # a probe error, or an unrecognised answer). Fail CLOSED: scan anyway. A clean
    # push on a tool-less machine still passes; a leak, or a scan that cannot run,
    # is stopped. Warn loudly so the undetermined path shows.
    echo "⚠ push privacy gate: could not confirm '${slug:-<remote>}' visibility (no forge CLI for this host, or an unrecognised answer) — scanning the diff and checking commit identities anyway (fail closed, §3f #14)." >&2
    target="repo '${slug:-<remote>}' (visibility could not be confirmed)"
    confirm_hint="  To confirm visibility: authenticate this host's forge CLI (gh auth login / glab auth login). A cached verdict or a private_repos declaration does not establish a private remote for a push: only the forge's answer, asked now, does."
    ;;
esac

if [ ! -f "${scanner}" ] || [ -z "${scan_py}" ]; then
  echo "✗ refuse: push to ${target} cannot be checked: the privacy scan could not start (fail closed)."
  [ -f "${scanner}" ] || echo "  scripts/privacy_scan.py is missing from this checkout's committed sources."
  [ -n "${scan_py}" ] || echo "  No Python 3.13+ interpreter was found on PATH (python3.13, python3.14, python3, python, uv)."
  echo "  Nothing was published. Install Python 3.13+ (or uv) and restore this checkout's scripts/, then push again."
  [ -z "${confirm_hint}" ] || echo "${confirm_hint}"
  exit 1
fi

# The term registry resolves through the environment, which a pusher can fill with
# a store of their own in ways no PreToolUse guard sees (`export`, `printf -v`, `read`, `source`, a script setting
# os.environ). So the lists this hook LOADED are held to the digests committed at this checkout's HEAD, which the
# required term-source-drift CI job holds equal to the real secret. Only the fingerprint as committed counts: a file
# that is missing, untracked, edited since, or unreadable is refused like a mismatch. No digest and no term is printed.
fingerprint_rel=".github/term-source-fingerprint.json"
_fingerprint_is_committed() {
  local at_head on_disk
  at_head=$(_checkout_git rev-parse --verify --quiet "HEAD:./${fingerprint_rel}" 2>/dev/null </dev/null) || return 1
  on_disk=$(_checkout_git hash-object -- "${repo_root}/${fingerprint_rel}" 2>/dev/null </dev/null) || return 1
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
tips_out=$(PYTHONPATH="${scan_pythonpath}" "${scan_py}" -P -S -m teatree.hooks.remote_holds \
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

# The commit and tag objects a push publishes, read by `teatree.hooks.commit_objects` (the one reader `t3 fast-push`
# shares) from the copied sources: prints nothing and exits non-zero when git could not read them, so the scan fails closed.
_objects() {
  PYTHONPATH="${scan_pythonpath}" "${scan_py}" -P -S -m teatree.hooks.commit_objects "$@" </dev/null
}

# Messages of the commits named by the `git log` args, scanned apart from any patch, on stdin in text mode (no temp file
# that can be missing or unwritable); each is read as git decodes it and as the object stores it.
_scan_message_text() {
  _objects messages "$@" | _scan --text -
}

# A scan that could not run outranks a finding, and a finding outranks a clean scan: the code to report for these.
_worse_scan_rc() {
  local code worst=0
  for code in "$@"; do
    if [ "${code}" -ne 0 ] && [ "${code}" -ne "${findings_code}" ]; then
      echo "${code}"
      return 0
    fi
    [ "${code}" -eq 0 ] || worst="${findings_code}"
  done
  echo "${worst}"
}

# The WHOLE raw object, headers and all, because an extra header, an author or committer name and a tag's own lines travel
# with the pushed object like its message does, and no CI job reads objects. Two passes, one report: registry terms and
# opaque ids over the object as it is, so a term in a name or an address is still found; and EVERY detector over it with
# the noreply domain cut from the identity lines, so a secret or a home path in a header is found, and so is a token worn
# as a noreply login, while a noreply address is no email finding. Only deliberately encoded content is left unread.
_scan_objects_from() {
  local kind="$1" terms_rc=0 full_rc=0 terms_report full_report
  shift
  terms_report=$(_objects "${kind}" raw "$@" | _scan --text --terms-only - 2>&1) || terms_rc=$?
  full_report=$(_objects "${kind}" blank "$@" | _scan --text - 2>&1) || full_rc=$?
  if [ "${terms_rc}" -ne 0 ]; then
    printf '%s\n' "${terms_report}"
  fi
  if [ "${full_rc}" -ne 0 ] || [ "${terms_rc}" -eq 0 ]; then
    printf '%s\n' "${full_report}"
  fi
  return "$(_worse_scan_rc "${terms_rc}" "${full_rc}")"
}

_scan_commit_objects() {
  _scan_objects_from commit "$@"
}

# Both reads, one report.
_scan_messages_as_text() {
  local message_rc=0 object_rc=0 message_report object_report
  message_report=$(_scan_message_text "$@" 2>&1) || message_rc=$?
  object_report=$(_scan_commit_objects "$@" 2>&1) || object_rc=$?
  printf '%s\n' "${message_report}"
  [ "${object_rc}" -eq 0 ] || printf '%s\n' "${object_report}"
  return "$(_worse_scan_rc "${message_rc}" "${object_rc}")"
}

# Blocked pushes only: re-scan per commit and file, since a finding's line number in the whole-range blob is not locatable.
# Args: the pushed tip, then the `<sha> --not <public-tips>` range. Returns 0 if it attributed a finding, 1 if none.
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
    sub=$(_scan_message_text -1 "${sha}" 2>&1) || sub_rc=$?
    if [ "${sub_rc}" -eq "${findings_code}" ]; then
      echo "  ${where} (commit message):"
      printf '%s\n' "${sub}" | sed 's/^/    /'
      printed=0
    elif [ "${sub_rc}" -eq 0 ]; then
      # Clean message: a term can still sit in an extra header or in the author or committer name.
      sub=$(_scan_commit_objects -1 "${sha}" 2>&1) || sub_rc=$?
      if [ "${sub_rc}" -eq "${findings_code}" ]; then
        echo "  ${where} (commit object headers):"
        printf '%s\n' "${sub}" | sed 's/^/    /'
        printed=0
      fi
    fi
    # Then each changed file's patch in this commit, named individually.
    while read -r f; do
      [ -n "${f}" ] || continue
      sub_rc=0
      sub=$(git show "${git_read_flags[@]}" --format= --patch --cc "${sha}" -- "${f}" 2>/dev/null </dev/null | _scan - 2>&1) || sub_rc=$?
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
  tag_report=$(_scan_objects_from tag "${sha}" 2>&1) || tag_rc=$?
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
  bad_idents=$(git log --encoding=UTF-8 --format='%ae%n%ce' "${new_commits[@]}" 2>/dev/null \
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
  patch_report=$(git log "${git_read_flags[@]}" --format= --patch --cc "${new_commits[@]}" 2>/dev/null </dev/null | _scan - 2>&1) || patch_rc=$?
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
