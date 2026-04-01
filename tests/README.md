# Test Strategy

## Pyramid

1. **Integration tests** (bulk): exercise Django views, management commands, and selectors with the test client and real DB. Cover happy paths.
2. **Unit tests** (edge cases): isolated functions with complex branching, error handling, boundary values.
3. **E2E tests** (`e2e/`): browser-level tests with Playwright for dashboard panels and critical user flows.

## Conventions

- Test files mirror `src/` module paths: `src/teatree/core/sync.py` -> `tests/teatree_core/test_sync.py`
- Test classes and methods describe behavior: `test_returns_error_when_token_missing`
- `@pytest.mark.parametrize` when 3+ tests differ only by input/output
- Mock only external boundaries: subprocess, network, clock
- Use `setUpTestData()` for DB fixtures (class-level, faster)

## Running

```bash
uv run pytest --no-cov -x -q          # fast: no coverage, stop on first failure
uv run pytest                          # full: with coverage, all tests
uv run pytest tests/teatree_core/      # specific module
uv run pytest --tach                   # skip tests unaffected by changes
```

## Coverage

Target: 100% (`fail_under = 100` in pyproject.toml). Use `pragma: no cover` sparingly and only for genuinely unreachable code (e.g., `if __name__ == "__main__"`).
