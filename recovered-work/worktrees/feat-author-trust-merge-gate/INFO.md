# Worktree salvage — feat-author-trust-merge-gate (WITHHELD)

- **path**: ~/workspace/teatree-1773
- **branch**: feat/author-trust-merge-gate
- **status**: WITHHELD from this public repo

## Why withheld

Both halves are kept off this public repo:

- The 87 patch-id-unique commits (~13MB) are oversized and were never inlined;
  they persist in the branch ref `feat/author-trust-merge-gate` in the repo's
  .git and survive `git worktree remove`.
- The ~91KB uncommitted working-tree diff hardcodes a personal forge handle in
  a trust-list feature dozens of times, so it is not safe to publish here.

## Restore locally

The branch and the live worktree remain on the owner's machine. Restore the
uncommitted diff straight from the worktree, and the commits from the branch ref:

```bash
git -C ~/workspace/teatree-1773 diff HEAD > feat-author-trust-merge-gate.worktree.diff
git format-patch origin/main...feat/author-trust-merge-gate --cherry-pick --right-only --no-merges --stdout > feat-author-trust-merge-gate.commits.patch
```
