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

REPO_ROOT="${1:?usage: fast-forward-checkout.sh <repo-root>}"

git -C "$REPO_ROOT" fetch --prune origin

# The exact ref `git pull --ff-only` would merge. Without an upstream there is
# nothing to compare against, so no dirt is provably lossless and none is touched.
FF_TARGET="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"

retained=()

# Blob sha of <rev>:<path>, or empty when that rev does not carry the path.
blob_at() {
    git -C "$REPO_ROOT" rev-parse --quiet --verify "$1:$2" 2>/dev/null || true
}

# True when discarding the local state of <path> destroys nothing: either the
# working tree already holds the target's exact bytes, or the path is absent from
# the working tree (a deletion has no content to lose) and the target restores it.
lossless_to_discard() {
    local path="$1" target_blob="$2"
    [ -n "$target_blob" ] || return 1
    if [ ! -e "$REPO_ROOT/$path" ]; then
        return 0
    fi
    [ "$(git -C "$REPO_ROOT" hash-object -- "$path" 2>/dev/null || true)" = "$target_blob" ]
}

if [ -n "$FF_TARGET" ]; then
    # Tracked paths that differ from HEAD, staged or unstaged, deletions included.
    # `< <(...)` (process substitution, never a pipe) keeps the loop in this shell
    # so `retained` survives it.
    while IFS= read -r -d '' path; do
        if [ -n "$(blob_at HEAD "$path")" ] && lossless_to_discard "$path" "$(blob_at "$FF_TARGET" "$path")"; then
            echo "deploy: discarding the local change to '$path' — its content already equals ${FF_TARGET}'s, so the fast-forward restores it byte-for-byte."
            git -C "$REPO_ROOT" checkout HEAD -- "$path"
        else
            retained+=("$path")
        fi
    done < <(git -C "$REPO_ROOT" diff --name-only -z HEAD)

    # Untracked paths the incoming commits would create. git refuses the merge for
    # these too, and removing one whose bytes the target already carries is equally
    # lossless.
    while IFS= read -r -d '' path; do
        if lossless_to_discard "$path" "$(blob_at "$FF_TARGET" "$path")"; then
            echo "deploy: removing the untracked '$path' — ${FF_TARGET} carries that exact content."
            rm -f "$REPO_ROOT/$path"
        else
            retained+=("$path")
        fi
    done < <(git -C "$REPO_ROOT" ls-files --others --exclude-standard -z)
fi

if git -C "$REPO_ROOT" pull --ff-only; then
    exit 0
fi

echo "deploy: FATAL — could not fast-forward $REPO_ROOT to ${FF_TARGET:-its upstream}." >&2
if [ "${#retained[@]}" -gt 0 ]; then
    echo "deploy: these paths hold content that is NOT in ${FF_TARGET:-the upstream}, so they were kept, never discarded:" >&2
    printf 'deploy:   %s\n' "${retained[@]}" >&2
    echo "deploy: inspect each with 'git -C $REPO_ROOT diff -- <path>', then either move it to a branch or discard it with 'git -C $REPO_ROOT checkout HEAD -- <path>', and re-run the deploy." >&2
else
    echo "deploy: no local changes were retained, so the dirt is not the cause — read the git error above (diverged history, or an index lock / permission problem)." >&2
fi
exit 1
