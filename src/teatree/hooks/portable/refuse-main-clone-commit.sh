#!/usr/bin/env bash
# Pre-commit hook: enforce worktree-first (#638, staged-edit gap #2614).
#
# Refuses a commit when it is attempted from a *main clone* (a checkout
# whose ``.git`` is a directory) in either of two worktree-first
# violations:
#
#   1. the clone is on a non-default branch (feature work landing in the
#      shared clone), or
#   2. the clone is on the default branch but has *tracked working-tree
#      changes staged for commit* (the #2614 incident — an agent
#      ``git add``-staged a hand-edit directly in the managed main clone
#      while it sat on ``main``; the branch-only gate exited 0 and the
#      staged edit committed into the shared clone, later leaving a stale
#      byte-identical duplicate that blocked ``pull_main_clone``).
#
# Feature work belongs in a worktree, never in the shared main clone —
# branches and commits landing there pollute shared state and risk
# committing to the wrong place. Worktrees (``.git`` is a file) are
# always allowed, and a clean default-branch tree (the fast-forward
# merge-keeping flow stages nothing) stays allowed.
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

# On the default branch but with tracked changes staged for commit: the
# #2614 staged-edit gap. ``git diff --cached --name-only`` lists paths
# staged in the index against HEAD; a non-empty list is a tracked
# working-tree change staged directly in the managed main clone. Refuse
# independent of branch state — the staged state IS the violation.
if [ -n "$(git diff --cached --name-only 2>/dev/null)" ]; then
  echo "✗ refuse: tracked changes are staged directly in the main clone (on '${current}') — develop in a worktree."
  echo "  Run: t3 <overlay> workspace ticket <issue_url>, move the change there, and discard the staged copy here:"
  echo "       git restore --staged --worktree -- <path>"
  echo "  (worktree-first is non-negotiable — see /t3:rules § Worktree-First Work)"
  exit 1
fi

exit 0
