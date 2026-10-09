#!/usr/bin/env bash
# Fast-forward the deploy build-context checkout to its upstream, surviving the
# one kind of local dirt that can never be lost by doing so.
#
# WHY THIS EXISTS. The checkout is the deploy build context AND the host's main
# clone that agents branch worktrees from, so host-side tooling writes into its
# working tree. The recorded wedge: a dependabot PR bumps a pin in
# `pyproject.toml` WITHOUT regenerating `uv.lock` (the pip ecosystem does not
# know about uv — that gap is why `.github/workflows/uv-lock-upgrade.yml`
# exists), and the next `uv run` in this clone silently re-locks `uv.lock` to
# match. That leaves ONE modified tracked file, and from then on
# `git pull --ff-only` aborts with
#
#     error: Your local changes to the following files would be overwritten by merge
#
# on EVERY subsequent deploy. Nothing retries, nothing reports it, and the box
# silently stops tracking main while merges keep landing — it sat 42 commits
# behind before anyone noticed that "the deploy is red" meant "production is a
# different codebase".
#
# THE SAFETY STANDARD IS CONTENT-EQUIVALENCE, NOT "LOOKS REGENERABLE". A blanket
# `git reset --hard` / `git clean -fd` would unwedge every case and destroy
# uncommitted work in a clone that other agents share. So dirt is discarded ONLY
# when the working-tree blob already equals the blob at the fast-forward target:
# the fast-forward then recreates that exact content, so nothing unique can be
# lost. Every other modified, deleted, or untracked path is RETAINED untouched —
# and if it blocks the merge the deploy fails loud NAMING each file and the
# recovery command, instead of a raw git error that identifies neither.
set -euo pipefail

USAGE="usage: fast-forward-checkout.sh --fetch-only <repo-root> | fast-forward-checkout.sh <repo-root> [<commit>]"

# `--fetch-only` runs before deploy.sh builds and drains anything, and the later
# `<repo-root> <commit>` fast-forwards the live tree to exactly that commit without fetching
# again. So the fetch step must prove the later fast-forward can happen: it touches nothing,
# and refuses a diverged checkout or retained dirt the merge would overwrite.
FETCH_ONLY=false
if [ "${1:-}" = --fetch-only ]; then
    FETCH_ONLY=true
    shift
fi
REPO_ROOT="${1:?$USAGE}"
TARGET_COMMIT="${2:-}"

if [ "$FETCH_ONLY" = true ] || [ -z "$TARGET_COMMIT" ]; then
    git -C "$REPO_ROOT" fetch --prune origin >&2
fi

# Every path git reports below is relative to the checkout's TOPLEVEL, which is not
# always the argument: a fork that vendors teatree passes `<fork>/vendor/teatree`, a
# SUBDIRECTORY of the checkout git manages. Resolving those paths against the argument
# doubles the prefix, so each dirty file reads as absent — `lossless_to_discard` then
# calls it a lossless deletion and the `checkout` that follows dies on `pathspec ... did
# not match`, wedging every deploy on a checkout carrying any dirty tracked file.
TOPLEVEL="$(git -C "$REPO_ROOT" rev-parse --show-toplevel)"

# The exact ref `git pull --ff-only` would merge. Without an upstream there is
# nothing to compare against, so no dirt is provably lossless and none is touched.
FF_TARGET="${TARGET_COMMIT:-$(git -C "$TOPLEVEL" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)}"


# Blob sha of <rev>:<path>, or empty when that rev does not carry the path.
blob_at() {
    git -C "$TOPLEVEL" rev-parse --quiet --verify "$1:$2" 2>/dev/null || true
}

# True when discarding the local state of <path> destroys nothing: either the
# working tree already holds the target's exact bytes, or the path is absent from
# the working tree (a deletion has no content to lose) and the target restores it.
lossless_to_discard() {
    local path="$1" target_blob="$2"
    [ -n "$target_blob" ] || return 1
    if [ ! -e "$TOPLEVEL/$path" ]; then
        return 0
    fi
    [ "$(git -C "$TOPLEVEL" hash-object -- "$path" 2>/dev/null || true)" = "$target_blob" ]
}

# Sorts local dirt against FF_TARGET, touching nothing: tracked paths that differ from HEAD
# (staged or unstaged, deletions included) and untracked paths. Lossless ones go to
# DISCARD_TRACKED / DISCARD_UNTRACKED, everything else to RETAINED. `< <(...)` (process
# substitution, never a pipe) keeps the loops in this shell so the arrays survive them.
DISCARD_TRACKED=()
DISCARD_UNTRACKED=()
RETAINED=()
classify_dirt() {
    local path
    while IFS= read -r -d '' path; do
        if [ -n "$(blob_at HEAD "$path")" ] && lossless_to_discard "$path" "$(blob_at "$FF_TARGET" "$path")"; then
            DISCARD_TRACKED+=("$path")
        else
            RETAINED+=("$path")
        fi
    done < <(git -C "$TOPLEVEL" diff --name-only -z HEAD)
    while IFS= read -r -d '' path; do
        if lossless_to_discard "$path" "$(blob_at "$FF_TARGET" "$path")"; then
            DISCARD_UNTRACKED+=("$path")
        else
            RETAINED+=("$path")
        fi
    done < <(git -C "$TOPLEVEL" ls-files --others --exclude-standard -z)
}

report_retained() {
    echo "deploy: these paths hold content that is NOT in ${FF_TARGET:-the upstream}, so they were kept, never discarded:" >&2
    printf 'deploy:   %s\n' "$@" >&2
    echo "deploy: inspect each with 'git -C $TOPLEVEL diff -- <path>', then either move it to a branch or discard it with 'git -C $TOPLEVEL checkout HEAD -- <path>', and re-run the deploy." >&2
}

[ -z "$FF_TARGET" ] || classify_dirt

if [ "$FETCH_ONLY" = true ]; then
    target="$(git -C "$TOPLEVEL" rev-parse --verify "${FF_TARGET:-HEAD}^{commit}")"
    if ! git -C "$TOPLEVEL" merge-base --is-ancestor HEAD "$target"; then
        echo "deploy: FATAL — $TOPLEVEL has diverged from ${FF_TARGET}: HEAD carries commits it does not, so it can never fast-forward. Reconcile the checkout and re-run the deploy." >&2
        exit 1
    fi
    # --no-renames lists a rename's old path too, so a local edit to it counts as an overlap.
    touched="$(git -C "$TOPLEVEL" diff --name-only --no-renames "HEAD" "$target")"
    blocking=()
    for path in ${RETAINED[@]+"${RETAINED[@]}"}; do
        if grep -Fxq -- "$path" <<<"$touched"; then
            blocking+=("$path")
        fi
    done
    if [ "${#blocking[@]}" -gt 0 ]; then
        echo "deploy: FATAL — $TOPLEVEL cannot fast-forward to ${FF_TARGET}: local changes would be overwritten by the merge. Nothing was built, drained or moved." >&2
        report_retained "${blocking[@]}"
        exit 1
    fi
    printf '%s\n' "$target"
    exit 0
fi

for path in ${DISCARD_TRACKED[@]+"${DISCARD_TRACKED[@]}"}; do
    echo "deploy: discarding the local change to '$path' — its content already equals ${FF_TARGET}'s, so the fast-forward restores it byte-for-byte."
    git -C "$TOPLEVEL" checkout HEAD -- "$path"
done
for path in ${DISCARD_UNTRACKED[@]+"${DISCARD_UNTRACKED[@]}"}; do
    echo "deploy: removing the untracked '$path' — ${FF_TARGET} carries that exact content."
    rm -f "$TOPLEVEL/$path"
done

# `merge.autostash` in the operator's own git config would silently stash the dirt this
# script just decided to RETAIN, merge, and re-apply — leaving conflict markers in a
# tree the contract above promises to leave untouched. Pinned off so the verdict is
# this script's, not the ambient config's.
if [ -n "$TARGET_COMMIT" ]; then
    git -C "$TOPLEVEL" -c merge.autostash=false merge --ff-only "$TARGET_COMMIT" && exit 0
elif git -C "$TOPLEVEL" -c merge.autostash=false -c rebase.autostash=false pull --ff-only; then
    exit 0
fi

echo "deploy: FATAL — could not fast-forward $TOPLEVEL to ${FF_TARGET:-its upstream}." >&2
if [ "${#RETAINED[@]}" -gt 0 ]; then
    report_retained "${RETAINED[@]}"
else
    echo "deploy: no local changes were retained, so the dirt is not the cause — read the git error above (diverged history, or an index lock / permission problem)." >&2
fi
exit 1
