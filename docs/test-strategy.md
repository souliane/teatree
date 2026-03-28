# Test Strategy

## Test Pyramid

1. **Integration tests (bulk)**: Django `TestCase` + test client to exercise views, htmx partials, management commands, model transitions, and selectors end-to-end through the Django stack. Fast and catch most regressions.

2. **Unit tests (edge cases)**: Isolate individual functions (selectors, utility helpers, FSM transition conditions) for edge cases that are hard to trigger through integration tests. Fill coverage gaps.

3. **E2E tests (critical paths)**: Playwright for flows that depend on JavaScript behavior — htmx interactions, SSE updates, modal popups, the sync button. Keep small and stable.

## Rules

- **100% branch coverage** on all code under `src/`. No exceptions.
- Integration tests for happy paths, unit tests for edge cases.
- `@pytest.mark.parametrize` when 3+ tests differ only by input/output.
- Mocks only for external boundaries (subprocess, network, clock).
- Target < 30s for the full suite locally.
