# Worktree salvage — fix-loop-task-lifecycle

- **path**: ~/workspace/fix-task-lifecycle/teatree
- **branch**: fix/loop-task-lifecycle
- **ticket**: (none)
- **head**: 278dabd65d1525624fd6554b41067104ef8b11ab
- **patch-id-unique unpushed commits**: 83
- **merged_pr**: False  open_pr: False
- **commits.patch commits**: 83 (13712861B)
- **worktree.diff bytes**: 11894

## Uncommitted tracked files

- `src/teatree/core/managers.py`
- `src/teatree/core/models/task.py`
- `src/teatree/loop/scanners/active_tickets.py`
- `src/teatree/loop/scanners/architectural_review.py`
- `src/teatree/loop/scanners/pending_tasks.py`
- `src/teatree/loop/scanners/provision_smoke.py`
- `src/teatree/loop/scanners/scanning_news.py`
- `src/teatree/loop/tick_recovery.py`
- `tests/teatree_loop/test_pending_tasks.py`

## Untracked files (NOT in diff; restore from worktree on disk)

- `src/teatree/core/migrations/0045_task_created_at_task_subject.py`
- `src/teatree/core/migrations/0046_backfill_task_subject.py`
- `tests/teatree_core/test_task_lifecycle.py`
- `tests/test_migration_0046_backfill_task_subject.py`

## NOTE — commits NOT inlined (oversized)

The 83 patch-id-unique commits total ~13MB and are intentionally
NOT committed here. They persist in the branch ref `fix/loop-task-lifecycle` in the repo's
.git, so they survive `git worktree remove`. Restore locally with:

```bash
git format-patch origin/main...fix/loop-task-lifecycle --cherry-pick --right-only --no-merges --stdout > fix-loop-task-lifecycle.patch
```

Only the uncommitted working-tree diff (at risk on worktree removal) is salvaged in this PR.
