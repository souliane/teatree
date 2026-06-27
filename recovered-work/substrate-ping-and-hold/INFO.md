# Recovery INFO — substrate-ping-and-hold

- **label**: substrate-ping-and-hold
- **branch**: substrate-ping-and-hold
- **ticket**: (none)
- **status**: recovered
- **unique_commits**: 4
- **snapshot_ts**: 20260625T151029Z
- **worktree_diff_bytes**: 0
- **tip**: 0f0da5862683632b8641c0855f7ddc5842d1fef9
- **source_dir**: ~/.local/share/teatree/recovery-rescue-20260627/t3-recover-substrate-ping-and-hold-20260625T151029Z-we43dzio

## Files touched

- `src/teatree/backends/forge_merge_rpc.py`
- `src/teatree/backends/github/client.py`
- `src/teatree/backends/gitlab/client.py`
- `src/teatree/core/backend_protocols.py`
- `src/teatree/core/gates/owned_repo_guard.py`
- `src/teatree/core/merge/authorization.py`
- `src/teatree/core/merge/ci_rollup.py`
- `src/teatree/core/merge/execution.py`
- `src/teatree/core/models/merge_clear.py`
- `src/teatree/eval/regression_corpus.py`
- `src/teatree/eval/regression_corpus_predicates.py`
- `src/teatree/loop/scanner_factories.py`
- `src/teatree/loop/scanners/pr_sweep.py`
- `src/teatree/loop/scanners/pr_sweep_adapters.py`
- `src/teatree/loop/scanners/pr_sweep_substrate.py`
- `src/teatree/loop/substrate_pinger.py`
- `tests/teatree_backends/test_code_host_protocol_coverage.py`
- `tests/teatree_backends/test_protocols.py`
- `tests/teatree_core/models/test_merge_clear_substrate_paths.py`
- `tests/teatree_core/test_clear_issuance_and_human_substrate.py`
- `tests/teatree_eval/test_regression_corpus_predicates.py`
- `tests/teatree_loop/test_pr_sweep_scanner.py`
- `tests/teatree_loop/test_scanner_error_signals.py`

## Commit subjects

- 0f0da58626 chore: merge origin/main into substrate-ping-and-hold
- 84405c29aa test(eval): regression corpus pins substrate ping-and-hold under autonomy=full
- c1150d2598 test(backends): keep CodeHostBackend coverage in sync with fetch_pr_changed_paths
- 53a19bbbdb fix(merge): substrate pings the owner and holds under autonomy=full
