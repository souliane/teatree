# Worktree Drain — Read-Only Salvage Manifest (PHASE 2)

Read-only pass over every worktree of this repo. **No worktree was removed,
moved, reset, or modified** — this pass only READ each one and added patches to
this branch. The destructive wipe is a separate step the orchestrator performs
later from this manifest.

## Counts

| Class | Count |
|---|---|
| redundant | 1 |
| unfinished-salvaged | 28 |
| unfinished-salvaged (partial-withheld) | 4 |
| unfinished-WITHHELD | 1 |
| active-protected | 47 |
| main-clone (skipped) | 1 |
| **total** | 82 |

wipe-safe: YES=31  NO=51

**wipe-safe** = YES means nothing is lost by `git worktree remove`: committed
commits always survive in their branch ref, and any uncommitted content is in
this PR (or there is none). NO means active/protected, a detached commit with no
branch ref, or uncommitted/withheld content that is NOT in this PR — read it from
the live worktree before any wipe. An oversized-commit branch is wipe-safe but its
branch ref must be kept.

## Worktrees

| Path | Branch | Class | Wipe-safe | Notes |
|---|---|---|---|---|
| `/private/tmp/claude-501/-Users-adrien-workspace/43b900d7-caa1-497c-b610-f89470f02e6e/scratchpad/pr2720-wt` | `(detached 00343ff3)` | unfinished-salvaged | YES | 1 commits in PR; detached HEAD; commits captured in PR |
| `/private/tmp/claude-501/-Users-adrien-workspace/43b900d7-caa1-497c-b610-f89470f02e6e/scratchpad/pr2728-wt` | `(detached 70eb7630)` | unfinished-salvaged | YES | 2 commits in PR; detached HEAD; commits captured in PR |
| `/private/tmp/t3-review-2759-WNVI` | `(detached a94862f8)` | unfinished-salvaged | YES | 1 commits in PR; detached HEAD; commits captured in PR |
| `/private/tmp/teatree-fix-session-1782506979` | `fix/get-active-session-new` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/2346-behavioural-drift-under-load-eval-lane-d/teatree` | `2346-behavioural-drift-under-load-eval-lane-d` | unfinished-salvaged | YES | 2 commits in PR |
| `~/workspace/2545-dream-acceptance-gates/teatree` | `2545-fix-deferred-import` | unfinished-salvaged | YES | 2 commits in PR |
| `~/workspace/2625-fix-statusline-freshness/teatree` | `2625-fix-statusline-freshness` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/a-teatree-e2e-playwright-args/teatree` | `a-e2e-playwright-args-hook` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/ac-2232-spec-coverage-gate/teatree` | `ac/2232-spec-coverage-gate` | unfinished-salvaged | YES | 99 commits oversized→branch ref (keep branch); uncommitted diff in PR |
| `~/workspace/fix-task-lifecycle/teatree` | `fix/loop-task-lifecycle` | unfinished-salvaged | YES | 83 commits oversized→branch ref (keep branch); uncommitted diff in PR |
| `~/workspace/identity-gate-host-aware/teatree` | `fix/identity-host-aware-gate` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/souliane/1038-redis-shared-namespace` | `1038-redis-shared-namespace` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/2577-fix-redis-recreate-guard` | `2577-fix-redis-recreate-guard` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/comment-density-restate-gate/teatree` | `comment-density-restate-gate` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/souliane/eval-tier-redesign/teatree` | `eval-tier-redesign` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/fix-seed-cadence-parity/teatree` | `fix-seed-cadence-parity` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/fix-under-load-watchdog-fairness` | `2700-fix-scanner-ordering-flake` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/task-reconcile-rename` | `task-reconcile-rename` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/teatree-config-guard` | `feat/block-destructive-config-overwrite` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/teatree-wt-1780` | `ac/1780-1937-note-gate-overblock` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/souliane/teatree-wt-codex-sdk-backend` | `codex-sdk-backend` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/souliane/teatree-wt-skill-preamble` | `fix/orchestrator-subagent-skill-preamble` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/souliane/teatree-wt-teammate-opus` | `ac/teammate-opus-floor` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/souliane/wt-e2e-remote-no-provision` | `feat-e2e-remote-no-provision` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/souliane/wt-statusline-switcher-guard` | `feat-statusline-roster-no-switcher-guard` | unfinished-salvaged | YES | 1 commits in PR |
| `~/workspace/teatree-wt-get-active-session` | `fix/get-active-session` | unfinished-salvaged | YES | uncommitted diff in PR |
| `~/workspace/teatree-wt/1640-planning` | `1640-fsm-planning-phase` | unfinished-salvaged | YES | 83 commits oversized→branch ref (keep branch); uncommitted diff in PR |
| `~/workspace/teatree-wt/orch-investigation` | `1442-orchestrator-investigation-gate` | unfinished-salvaged | YES | 83 commits oversized→branch ref (keep branch); uncommitted diff in PR |
| `/private/tmp/claude-501/-Users-adrien-workspace/43b900d7-caa1-497c-b610-f89470f02e6e/scratchpad/wt-2717` | `(detached 03c13392)` | unfinished-salvaged (partial-withheld) | NO | banned-terms: withheld commits.patch. detached commit NOT in PR/branch ref — at risk; restore from worktree before wipe |
| `~/workspace/312-feat-pr-description-template-mechanism/teatree` | `312-feat-pr-description-template-mechanism` | unfinished-salvaged (partial-withheld) | NO | banned-terms: withheld worktree.diff. uncommitted diff NOT in PR — at risk; restore from worktree before wipe |
| `~/workspace/souliane/teatree-wt-e2e-review-record` | `docs-e2e-review-record-howto` | unfinished-salvaged (partial-withheld) | YES | banned-terms: withheld commits.patch. commit in branch ref (keep branch); patch withheld from PR |
| `~/workspace/souliane/teatree-wt-scrub` | `1933-dream-scrub-gate` | unfinished-salvaged (partial-withheld) | YES | banned-terms: withheld commits.patch. commit in branch ref (keep branch); patch withheld from PR |
| `~/workspace/teatree-1773` | `feat/author-trust-merge-gate` | unfinished-WITHHELD | NO | restricted; commits in branch ref (keep branch), uncommitted diff NOT in PR — restore locally |
| `~/workspace/t3-workspaces/t3-teatree/feat-teatree-autoload-default-off` | `feat-teatree-autoload-default-off` | redundant | YES | content-equivalent on origin/main or merged PR; clean tree |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a02243824087be065` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a0cb28456afb8a605` | `worktree-agent-a0cb28456afb8a605` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a24c51c1ddf65b215` | `worktree-agent-a24c51c1ddf65b215` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a2f519ef604bb599e` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a3260068ddee7b5d4` | `worktree-agent-a3260068ddee7b5d4` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a397b84c804057794` | `ac/258-fix-round2` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a3a2be6d989a46454` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a3d1f3693f78e7d1f` | `ac/review-fixes-core-models` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a440dbd1eb1b05ec9` | `ac/review-fixes-edges` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a4f60b690998cd122` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a52d6eb71787625c2` | `pr-1585` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a5d81a76eac0aaf1b` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a64bf1ef5e116ba27` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a6cebd3e96f4bde92` | `ac/review-fixes-backends` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a6d01d8eb13f9006a` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a6de2ffcdf5bce800` | `ac/review-fixes-all` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a6e0529aa87975398` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a81c2c4e35ddd4160` | `worktree-agent-a81c2c4e35ddd4160` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a862eadf6fb09374f` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a88a319b4c55c173d` | `worktree-agent-a88a319b4c55c173d` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a91de5ffcd6cc7fdf` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-a986d8746f8c22636` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-aa4a107758c7fb448` | `worktree-agent-aa4a107758c7fb448` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-ab9df8c82e52373e6` | `worktree-agent-ab9df8c82e52373e6` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-ac24cc0a138924cfa` | `pr1589new` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-ad9331cf2ccdc7b90` | `s-pubgate-rework` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-ae1c027434c34958c` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-af37dab7878ac0f93` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-af41177679772f33a` | `ac/dream-memory-to-fix-fix` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-af96314075abca6de` | `(detached)` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/agent-afd6a83bbb61989df` | `pr1583` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/banned-fail-unset` | `fix-banned-terms-fail-when-unset` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/cleanup-redesign` | `redesign-cleanup-fsm-done-no-snapshots` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/credit-classify` | `fix-credit-vs-subscription-limit-classification` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/docs-review-how` | `docs-review-how-blocks` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/recovered-work` | `recovered-snapshot-work` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/slack-bot-autoresolve-appid` | `ac/slack-bot-autoresolve-appid` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_563e54f6-4b6-5` | `regression-evals-pollution-and-premise-gates` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_72fef446-43e-2` | `worktree-wf_72fef446-43e-2` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_72fef446-43e-4` | `worktree-wf_72fef446-43e-4` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_7f61a596-1b0-2` | `worktree-wf_7f61a596-1b0-2` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_a81ae8c9-264-1` | `resolve-2517-conflict` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_a81ae8c9-264-4` | `ac/dream-memory-to-fix-merge` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_c45c32bf-966-1` | `worktree-wf_c45c32bf-966-1` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_c45c32bf-966-2` | `pr2505-fix` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/wf_ce97aede-335-5` | `worktree-wf_ce97aede-335-5` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree/.claude/worktrees/workspace-regroup` | `feat-per-overlay-workspace-dir-and-relocate` | active-protected | NO | under .claude/worktrees or locked — live |
| `~/workspace/souliane/teatree` | `main` | main-clone (skipped) | NO | teatree main clone — never wiped |
