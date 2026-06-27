# Recovery INFO — fix-metered-eval-green

- **label**: fix-metered-eval-green
- **branch**: fix/metered-eval-green
- **ticket**: (none)
- **status**: recovered
- **unique_commits**: 4
- **snapshot_ts**: 20260626T205035Z
- **worktree_diff_bytes**: 22850
- **tip**: 6f80d179ebe021eb33ac820bf346b98dfa68f09d
- **source_dir**: ~/.local/share/teatree/recovery-rescue-20260627/t3-recover-fix-metered-eval-green-20260626T205035Z-s0rek1b8

## Files touched

- `evals/scenarios/code.yaml`
- `evals/scenarios/debug.yaml`
- `evals/scenarios/dev_env_e2e_is_part_of_done.yaml`
- `evals/scenarios/do_the_best_no_tech_debt.yaml`
- `evals/scenarios/main_clone_protected.yaml`
- `evals/scenarios/merge_burst_reconcile.yaml`
- `evals/scenarios/review_claim_means_review_now.yaml`
- `evals/scenarios/review_request.yaml`
- `evals/scenarios/ship_delivery.yaml`
- `evals/scenarios/subagent_prompt_drift.yaml`
- `evals/scenarios/test_quality.yaml`
- `scripts/eval/corpus_gen/all_scenarios.py`
- `scripts/eval/corpus_gen/model.py`
- `scripts/eval/corpus_gen/per_skill.py`
- `scripts/eval/corpus_gen/ship_scenario.py`
- `src/teatree/eval/api_runner.py`
- `src/teatree/eval/git_fixture.py`
- `src/teatree/eval/loader.py`
- `src/teatree/eval/matchers.py`
- `src/teatree/eval/models.py`

## Commit subjects

- 6f80d179eb fix(eval): extend git-repo fixture to repo-dependent probes; grade arg-less tool calls
- c4ec514961 fix(eval): real git-repo sandbox fixture so working-tree probes fire the command
- 655c6ee1bb fix(eval): accept the real 't3 [<overlay>] run tests' command in test_runs_full matcher
- 10659659e0 fix(eval): grade dev_env_e2e on the written answer, not an impossible tool_call
