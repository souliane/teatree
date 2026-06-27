# Recovery INFO — 2746-feat-dream-cold-tier-memory-recall-surfa

- **label**: 2746-feat-dream-cold-tier-memory-recall-surfa
- **branch**: 2746-feat-dream-cold-tier-memory-recall-surfa
- **ticket**: #2746
- **status**: recovered
- **unique_commits**: 2
- **snapshot_ts**: 20260626T162700Z
- **worktree_diff_bytes**: 0
- **tip**: b77c96a028ea9633898c4a643c60e62cb1fd6a3d
- **source_dir**: ~/.local/share/teatree/recovery-rescue-20260627/t3-recover-2746-feat-dream-cold-tier-memory-recall-surfa-20260626T162700Z-h_167l3c

## Files touched

- `BLUEPRINT.md`
- `docs/generated/cli-reference.md`
- `docs/generated/management-commands.json`
- `docs/generated/management-commands.md`
- `hooks/scripts/hook_router.py`
- `hooks/scripts/memory_recall.py`
- `hooks/scripts/teatree_settings.py`
- `src/teatree/cli/django_groups.py`
- `src/teatree/cli/teatree_gate.py`
- `src/teatree/core/management/commands/memory.py`
- `src/teatree/loops/dream/gates.py`
- `src/teatree/loops/dream/recall.py`
- `src/teatree/loops/dream/reindex.py`
- `tests/teatree_cli/test_teatree_gate.py`
- `tests/teatree_core/management/commands/test_memory_command.py`
- `tests/teatree_hooks/test_memory_recall_hook.py`
- `tests/teatree_loops/dream/test_decay.py`
- `tests/teatree_loops/dream/test_gates.py`
- `tests/teatree_loops/dream/test_recall.py`
- `tests/teatree_loops/dream/test_reindex.py`

## Commit subjects

- b77c96a02 fix(dream): count distinct tokens for the cold-recall relevance floor (#2746)
- ea197aed6 feat(dream): cold-tier memory recall surfaces archived rules on relevant prompts (#2746)
