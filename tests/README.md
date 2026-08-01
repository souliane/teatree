# Test Strategy

## Pyramid

1. **Integration tests** (bulk): exercise Django management commands and selectors with `call_command` and real DB. Cover happy paths.
2. **Unit tests** (edge cases): isolated functions with complex branching, error handling, boundary values.

## Conventions

- Test files mirror `src/` module paths: `src/teatree/core/resolve.py` -> `tests/teatree_core/test_resolve.py` (an oversized module splits into a mirror package, e.g. `tests/teatree_core/sync/`)
- Test classes and methods describe behavior: `test_returns_error_when_token_missing`
- `@pytest.mark.parametrize` when 3+ tests differ only by input/output
- Mock only external boundaries: subprocess, network, clock
- Use `setUpTestData()` for DB fixtures (class-level, faster)

## Running

```bash
bash dev/test-affected.sh              # the diff-scoped lane — the local default (`--full` for the whole suite)
uv run pytest tests/teatree_core/      # specific module
uv run pytest tests/teatree_core/test_resolve.py -x -q   # single file; -x stops on first failure
bash dev/ci-parity-fast.sh             # pre-push inner loop: scoped prek + makemigrations + affected tests + push gate
bash dev/test-cov.sh                   # coverage lane: --cov --doctest-modules, 93% floor (CI parity)
```

The local default is **diff-scoped**, not the whole tree ([#3994](https://github.com/souliane/teatree/issues/3994)):
a local whole-tree sweep pays again for the run CI's required sharded lane is
about to make, and it sits on the critical path. `dev/test-affected.sh` is
fail-safe TO FULL — a migration, a conftest / `factories.py` / test-settings
edit, an unclassifiable path, or a missing merge-base each escalate to the
whole suite on their own — so scoping never under-runs.

The default `addopts` is lean and parallel (`-n auto`, pytest-xdist): no
coverage, no doctests, no `--exitfirst`. Coverage + doctests + the 93% floor
live in the dedicated CI `test (3.13)` lane and `dev/test-cov.sh`, so the inner
loop stays fast and the gate stays honest.

## Coverage

Floor: `fail_under = 93` (branch coverage) in `pyproject.toml`. Use `pragma: no
cover` sparingly and only for genuinely unreachable code (e.g., `if __name__ ==
"__main__"`).
