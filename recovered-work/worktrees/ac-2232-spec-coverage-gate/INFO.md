# Worktree salvage — ac-2232-spec-coverage-gate

- **path**: ~/workspace/ac-2232-spec-coverage-gate/teatree
- **branch**: ac/2232-spec-coverage-gate
- **ticket**: #2232
- **head**: 75da1355a7dbb3080fbe5118828fe2b767d835d4
- **patch-id-unique unpushed commits**: 99
- **merged_pr**: False  open_pr: False
- **commits.patch commits**: 99 (15331400B)
- **worktree.diff bytes**: 4818

## Uncommitted tracked files

- `src/teatree/config/loader.py`
- `src/teatree/config/settings.py`
- `src/teatree/core/models/types.py`

## Untracked files (NOT in diff; restore from worktree on disk)

- `tests/teatree_core/test_spec_coverage_gate.py`

## NOTE — commits NOT inlined (oversized)

The 99 patch-id-unique commits total ~14MB and are intentionally
NOT committed here. They persist in the branch ref `ac/2232-spec-coverage-gate` in the repo's
.git, so they survive `git worktree remove`. Restore locally with:

```bash
git format-patch origin/main...ac/2232-spec-coverage-gate --cherry-pick --right-only --no-merges --stdout > ac-2232-spec-coverage-gate.patch
```

Only the uncommitted working-tree diff (at risk on worktree removal) is salvaged in this PR.
