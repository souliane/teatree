# Worktree salvage — 1442-orchestrator-investigation-gate

- **path**: ~/workspace/teatree-wt/orch-investigation
- **branch**: 1442-orchestrator-investigation-gate
- **ticket**: #1442
- **head**: 6c8742cae02e96d411f258c2816189bb9b65f719
- **patch-id-unique unpushed commits**: 83
- **merged_pr**: False  open_pr: False
- **commits.patch commits**: 83 (13712861B)
- **worktree.diff bytes**: 12503

## Uncommitted tracked files

- `hooks/scripts/hook_router.py`
- `tests/test_gate_liveness_corpus.py`

## Untracked files (NOT in diff; restore from worktree on disk)

- `tests/test_orchestrator_investigation_boundary_hook.py`

## NOTE — commits NOT inlined (oversized)

The 83 patch-id-unique commits total ~13MB and are intentionally
NOT committed here. They persist in the branch ref `1442-orchestrator-investigation-gate` in the repo's
.git, so they survive `git worktree remove`. Restore locally with:

```bash
git format-patch origin/main...1442-orchestrator-investigation-gate --cherry-pick --right-only --no-merges --stdout > 1442-orchestrator-investigation-gate.patch
```

Only the uncommitted working-tree diff (at risk on worktree removal) is salvaged in this PR.
