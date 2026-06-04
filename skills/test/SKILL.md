---
name: test
description: Testing, QA, and CI — running tests, analyzing failures, quality checks, CI interaction, test plans, and posting testing evidence. Use when user says "run tests", "pytest", "lint", "CI failed", "pipeline", "test plan", "QA", or any test/CI task.
compatibility: macOS/Linux, pytest, linting tools, CI CLI (glab/gh).
requires:
  - workspace
  - rules
  - platforms
  - teatree
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Testing, QA & CI

Running tests, analyzing failures, quality checks, CI interaction, test plans, and testing evidence.

## Dependencies

- **t3:workspace** (required) — provides server and environment context. **Load `/t3:workspace` now** if not already loaded.

## Writing New Tests

When adding a new test, default to an integration or E2E test — not a unit test. Concretely:

- **Django test client** (`client.get(...)`, `client.post(...)`) for views and URL endpoints.
- **`call_command("name", ...)`** for management commands (Typer + Django glue).
- **Real overlay against `tmp_path`** with real `git init` for provisioning, worktrees, env files.
- **Playwright** (in `e2e/` or the project's E2E repo) for browser-visible behavior.
- **`subprocess.run(["t3", ...])`** (mark `@pytest.mark.integration`) when only the real CLI entry point reproduces the bug.

**Mock only unstoppable externals:** network calls (GitHub / GitLab / Slack / Sentry), the clock (`time_machine`), third-party subprocesses. Don't mock teatree code, Django models, filesystem under `tmp_path`, or `git` — run the real thing. If setup is painful, that usually points at a design problem, not a need for mocks.

**Unit tests** are reserved for pure logic: parsers, formatters, branch-name / slug builders, regex validators, anything with no I/O. A unit test for glue code that's already covered by an integration test is duplicate coverage — delete it.

The repo's `AGENTS.md` § "Test-Writing Doctrine" carries the authoritative rule and the review gate. `t3:review` enforces it per-PR (§ "New-Test Shape Check"). When rebalancing existing tests, the coverage gate must not drop.

## Workflows

### Backend Tests

**Prerequisites:** Docker services (Postgres, Redis) must be running. Start them via `t3 <overlay> worktree start` (see `/t3:workspace`) rather than raw `docker compose`. Read the project's test reference (e.g. `references/running-tests-and-lint.md`) for the full setup steps.

- `t3 <overlay> run tests` — run the project test suite.
- Flags: `--reuse-db`, `--failed-first`, optional `--parallel`.
- Always run with `--reuse-db` for speed unless schema changed.
- Use `--failed-first` to quickly re-verify fixes.
- **`t3 <overlay> run tests` and a raw `uv run pytest` can report different total counts** (the CLI wrapper may apply a narrower collection scope than a bare pytest invocation). A passed-count delta between the two runners is a collection difference, **not** a regression — confirm by checking the delta exists on the untouched base commit too, and don't burn a cycle hunting "missing" tests when your diff touches no test files. When a brief cites an expected count, match it with the **same runner** that produced it.

### Frontend Lint

- Run the project's frontend lint command (extension point: `wt_lint_frontend`).
- Fix lint errors before pushing.

### E2E Testing

See [`../e2e/SKILL.md`](../e2e/SKILL.md) (`/t3:e2e`) for the full E2E workflow: writing tests, running them, visual snapshots, evidence posting, and the pre-push visual QA gate.

### Quality Check

- `t3 ci quality-check` — quality analysis.
- Run before finalizing to catch quality issues early.

### CI Interaction

- `t3 ci fetch-failed-tests` — extract failed test node IDs from CI.
- `t3 ci fetch-errors` — extract error logs from CI.
- Run failed tests locally to reproduce before fixing.

### CI Pipeline Monitoring

- Background polling for pipeline status.
- Costs no tokens while waiting.

**Not-green == red (Non-Negotiable).** A pipeline is only OK when every required job is `success`. Treat any non-`success` job as a failure to fix, re-trigger, and confirm green: `failed`/`error`, `canceled`, `skipped`, `manual` (not run), `blocked`, a failing `allow_failure: true` job, or any gray/unknown state. `allow_failure: true` keeps the *pipeline* green but the *job* still failed — investigate it, do not skip it. Never declare CI passing, and never end a monitoring loop, while any job is non-green; a still-running/pending job is not yet terminal — wait, then re-apply. Canonical statement: `/t3:ship` § 6 "Not-green == red"; enforced in code by `teatree.loop.scanners.my_prs._needs_attention`.

### Docker Coverage Before Push

When the repo's pre-push hook uses `--no-cov` but CI enforces a coverage threshold, run the exact CI command locally before pushing:

```bash
docker run --rm -v "$PWD":/app:ro -e UV_PROJECT_ENVIRONMENT=/tmp/.venv -e COVERAGE_FILE=/tmp/.coverage teatree-test uv run -p 3.13 pytest --no-header -q -o cache_dir=/tmp/.pytest_cache
```

If the total is below the CI threshold, add tests before pushing.

### Green Means Root Cause

"Make the pipeline green" means fix the root cause — not skip, xfail, or `pragma: no cover` the test. Urgency means being faster at diagnosing, not cutting corners. A test that fails on the default branch is pre-existing and reported as such, not silenced.

### Fix-Push-Monitor Loop

When CI fails:

1. Fetch failures (`t3 ci fetch-failed-tests`)
2. **Check if the failure is pre-existing** (file never touched by branch) → if so, delegate to `/t3:ship` § "Isolate Unrelated Fixes"
3. Run failed tests locally to reproduce
4. Fix the issue
5. Push a regular commit (no squash/rebase)
6. Monitor pipeline again
7. Loop until green

## Generate Test Plan for PR

Analyze PR changes and produce a manual test plan. Use when the user says "test plan", "QA", or wants to document what to verify before merging.

### 1. Gather Context

- Read PR description via the issue tracker CLI (e.g., `glab mr view`, `gh pr view`)
- Read the diff (`git diff main...HEAD` or via the CLI)
- Read any linked ticket/specs for intended behavior

### 2. Structure the Test Plan

```markdown
## Test Plan

### Prerequisites
- [ ] Local environment running (backend + frontend)
- [ ] Logged in as {user_type} (admin/advisor/customer)
- [ ] Test data: {what data is needed}

### Test Cases

#### TC1: {Scenario name}
**Steps:**
1. Navigate to {page/URL}
2. {Action}

**Expected:** {What should happen}

### Edge Cases
- [ ] {Edge case}

### Regression Checks
- [ ] {Existing functionality that should still work}
```

**Tailor to change type:**

| Change type | Test focus |
|---|---|
| New UI field/component | Visibility, data binding, responsive layout, translations |
| Tooltip/label change | Text content, hover behavior, all affected languages |
| Form validation | Valid input, invalid input, boundary values, error messages |
| API/serializer change | Frontend displays new data, existing data still works |
| Acceptance rule | Rule triggers correctly, edge cases, existing rules unaffected |
| Customer-specific config | Correct customer sees it, other customers don't |

### 3. Post Test Plan to PR

Post the test plan as a comment on the PR. If a test plan comment already exists, skip posting (don't duplicate).

See your [issue tracker platform reference](../platforms/references/) § "PR Notes" for the posting recipe.

## Evidence, Visual QA, and E2E Debugging

See [`../e2e/SKILL.md`](../e2e/SKILL.md) (`/t3:e2e`) for the full E2E workflow including evidence posting, visual QA gate, browser console debugging, screenshot sanity checks, and store contamination checks.

## Verification Before Claims

**Iron law:** No completion claims without fresh verification evidence.

| Claim | Required evidence |
|---|---|
| "Tests pass" | Test runner output showing green |
| "Lint is clean" | Linter output with zero errors |
| "No regressions" | Diff review + relevant test output |
| "Services are running" | HTTP checks returning expected status codes (2xx/3xx) |
| "Evidence posted" | HTTP 200 from API + note/comment ID in output |
| "PR updated" | Confirmed via API response, not just "script ran" |
