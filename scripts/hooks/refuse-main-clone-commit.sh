#!/usr/bin/env bash
# Pre-commit hook: enforce worktree-first (#638).
#
# Refuses a commit when it is attempted from a *main clone* (a checkout
# whose ``.git`` is a directory) while on a non-default branch. Feature
# work belongs in a worktree, never in the shared main clone — branches
# and commits landing there pollute shared state and risk committing to
# the wrong place. Worktrees (``.git`` is a file) are always allowed.
#
# The default branch is detected from ``origin/HEAD`` so this works for
# teatree (``main``) and overlays whose default is ``development``
# without any per-repo configuration; it falls back to ``main`` when
# ``origin/HEAD`` is unset.
#
# Wired via prek in ``.pre-commit-config.yaml`` so it ships with the
# repo and needs no per-machine bootstrap.
set -euo pipefail

git_dir=$(git rev-parse --git-dir 2>/dev/null) || exit 0

# Worktrees have ``.git`` as a *file*; ``git rev-parse --git-dir`` in a
# worktree resolves to ``…/.git/worktrees/<name>``. Only a main clone
# has a top-level ``.git`` directory. If the resolved git dir is exactly
# ``<toplevel>/.git`` and that is a directory, this is the main clone.
toplevel=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
if [ ! -d "${toplevel}/.git" ] || [ "$(cd "${git_dir}" && pwd)" != "$(cd "${toplevel}/.git" && pwd)" ]; then
  exit 0  # a worktree (or detached/odd layout) — not the main clone
fi

current=$(git symbolic-ref --short HEAD 2>/dev/null) || exit 0  # detached HEAD: let other hooks decide

default_ref=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)
default_branch=${default_ref#origin/}
default_branch=${default_branch:-main}

if [ "${current}" != "${default_branch}" ]; then
  echo "✗ refuse: main clone is on '${current}' (default is '${default_branch}') — develop in a worktree."
  echo "  Run: t3 <overlay> workspace ticket <issue_url>"
  echo "  (worktree-first is non-negotiable — see /t3:rules § Worktree-First Work)"
  exit 1
fi

exit 0
