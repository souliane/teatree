# Worktree salvage — 1640-fsm-planning-phase

- **path**: ~/workspace/teatree-wt/1640-planning
- **branch**: 1640-fsm-planning-phase
- **ticket**: #1640
- **head**: 1505ab23c7bbc0f416f57bec9d205d77aa12c786
- **patch-id-unique unpushed commits**: 83
- **merged_pr**: False  open_pr: False
- **commits.patch commits**: 83 (13712861B)
- **worktree.diff bytes**: 91695

## Uncommitted tracked files

- `BLUEPRINT.md`
- `MAP.md`
- `agents/orchestrator.md`
- `agents/planner.md`
- `docs/blueprint/loop-topology.md`
- `hooks/scripts/hook_router.py`
- `src/teatree/core/management/commands/lifecycle.py`
- `src/teatree/core/management/commands/loop_dispatch.py`
- `src/teatree/core/migrations/0045_alter_ticket_state.py`
- `src/teatree/core/models/task.py`
- `src/teatree/core/models/ticket.py`
- `src/teatree/core/models/types.py`
- `src/teatree/core/phases.py`
- `src/teatree/core/plan_artifact_marker.py`
- `src/teatree/core/tasks.py`
- `src/teatree/eval/README.md`
- `src/teatree/eval/transcript_conformance.py`
- `src/teatree/skill_map.py`
- `tests/fixtures/transcripts/all_pass.session.jsonl`
- `tests/teatree_core/models/_shared.py`
- `tests/teatree_core/models/test_phase_dispatch.py`
- `tests/teatree_core/models/test_ticket_dirty_worktree_preflight.py`
- `tests/teatree_core/test_management_commands.py`
- `tests/teatree_core/test_managers.py`
- `tests/teatree_core/test_phases.py`
- `tests/teatree_core/test_plan_artifact.py`
- `tests/teatree_core/test_planning_phase.py`
- `tests/teatree_core/test_tasks.py`
- `tests/teatree_loop/test_tick.py`
- `tests/test_gate_liveness_corpus.py`
- `tests/test_hook_router_agent_plan_gate.py`
- `tests/test_hook_router_plan_artifact_gate.py`
- `tests/test_transcript_replay_conformance.py`

## NOTE — commits NOT inlined (oversized)

The 83 patch-id-unique commits total ~13MB and are intentionally
NOT committed here. They persist in the branch ref `1640-fsm-planning-phase` in the repo's
.git, so they survive `git worktree remove`. Restore locally with:

```bash
git format-patch origin/main...1640-fsm-planning-phase --cherry-pick --right-only --no-merges --stdout > 1640-fsm-planning-phase.patch
```

Only the uncommitted working-tree diff (at risk on worktree removal) is salvaged in this PR.
