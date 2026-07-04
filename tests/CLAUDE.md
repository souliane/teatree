# tests — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `tests/`.

- **Run:** `uv run pytest` for a fast inner loop — the default is parallel (`-n auto`) with no coverage. Add `-x -q` to stop on first failure. The full coverage gate (`bash dev/test-cov.sh`, and the CI `test (3.13)` lane) is **93% branch, non-negotiable** (`fail_under = 93, branch = true`; migrations omitted). New code ships with its tests in the same commit.
- **The CI `test (3.13)` gate is sharded, the floor is not weakened.** CI runs a 4-way `test-shard` matrix (`pytest-split`) that measures coverage with no floor, then a `test` COMBINER aggregates the shards, proves an exact partition (`scripts/ci/check_shard_completeness.py`), and enforces the 93% floor once over the combined data — identical semantics to the single-process `dev/test-cov.sh`. `tests/test_coverage_floor_guard.py::TestShardedCoverageLane` locks that the floor stays load-bearing (needs-edge to the shards, ≥2 distinct groups, partition check, shard-pass guard).
- **Tests mirror `src/`.** Test path mirrors the module under test; classes/methods describe behaviour, not implementation.
- **Lean integration / functional.** Prefer the Django test client, `call_command`, and real `git` under `tmp_path`. Reserve unit tests for pure logic (parsers, formatters, branch-name builders). Mock only unstoppable externals (network, clock, third-party subprocesses). Full rule + review gate: `AGENTS.md` § "Test-Writing Doctrine".
- **A regression test must be observed RED before the fix.** A test that passes on the buggy code guards nothing — see `/t3:code` § TDD Discipline.
