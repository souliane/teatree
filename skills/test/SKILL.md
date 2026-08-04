---
name: test
description: Testing, QA, and CI — running tests, analyzing failures, quality checks, CI interaction, test plans, and posting testing evidence. Use when user says "run tests", "pytest", "lint", "CI failed", "pipeline", "test plan", "QA", or any test/CI task.
compatibility: macOS/Linux, pytest, linting tools, CI CLI (glab/gh).
requires:
  - workspace
  - rules
  - platforms
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

**Run tests ONLY via `t3` — never raw `docker compose` (Absolute Rule).** The `t3` wrapper sets the correct env, project environment, DB name, and collection scope; a raw `docker compose up` + bare `pytest` reproduces neither and silently drifts from CI. Do this, in order — never the right-hand alternative: <!-- local-verification: cited-not-prescribed -->

1. Bring services up with the overlay command — **never** `docker compose up` / `docker-compose up`:

   ```bash
   t3 <overlay> worktree start
   ```

2. Run the suite with the overlay command — **never** a bare `pytest` / `uv run pytest` against the raw containers: <!-- local-verification: cited-not-prescribed -->

   ```bash
   t3 <overlay> run tests --reuse-db
   ```

3. In CI, the single canonical entry point is:

   ```bash
   t3 <overlay> ci
   ```

**Prerequisites:** Docker services (Postgres, Redis) must be running. Start them with `t3 <overlay> worktree start` (see `/t3:workspace`) — never raw `docker compose`. Read the project's test reference (e.g. `references/running-tests-and-lint.md`) for the full setup steps.

- `t3 <overlay> run tests` — run the project test suite.
- Flags: `--reuse-db`, `--failed-first`, optional `--parallel`.
- Always run with `--reuse-db` for speed unless schema changed.
- Use `--failed-first` to quickly re-verify fixes.
- To run only the tests for a specific file or directory, append the path after `--`: `t3 <overlay> run tests -- path/to/test_file.py` (extra args after `--` are forwarded to pytest). This scopes verification to the changed module instead of firing the whole suite locally.
- **`t3 <overlay> run tests` and a raw `uv run pytest` can report different total counts** (the CLI wrapper may apply a narrower collection scope than a bare pytest invocation). A passed-count delta between the two runners is a collection difference, **not** a regression — confirm by checking the delta exists on the untouched base commit too, and don't burn a cycle hunting "missing" tests when your diff touches no test files. When a brief cites an expected count, match it with the **same runner** that produced it. <!-- local-verification: cited-not-prescribed -->

### Local Test Selection — the testing phase's default lane (#113, [#3994](https://github.com/souliane/teatree/issues/3994))

`t3 tool affected-tests` selects only the pytest tests a diff affects. On the teatree repo it is **what the testing phase runs**, not an optional accelerator: a local whole-tree sweep re-pays for the run CI's required sharded lane is about to make anyway, and measured across 71 FSM transitions that duplication was most of a 3.5h ticket against a 30m target. It is **safety-biased**: it over-selects (never under-selects) and degrades to the whole-tree run on anything it cannot prove local. What is unchanged by making it the default: the whole-tree 12-shard CI run stays the merge/coverage gate, and the selector is **never** wired into the pre-push gate.

```bash
t3 tool affected-tests                 # human report: SCOPED (tach plugin + force-keep) or FULL + reason
t3 tool affected-tests --pytest-args   # emit the pytest invocation (the --tach flags + force-keep plugin)
t3 tool affected-tests --json          # machine-readable selection
t3 tool affected-tests --explain all   # trace why each test is force-kept over the plugin
t3 tool affected-tests --explain tests/teatree_core/test_x.py   # trace one test

bash dev/test-affected.sh              # select + run the fast lane (--full to force whole suite)
```

How it selects (#3672): the **tach pytest plugin** is the impact engine — `--tach --tach-base origin/main` walks the reverse-import graph natively and deselects the tests a diff cannot reach, in one session. `t3 tool affected-tests` decides FULL-vs-scoped from the escalation policy and, on a scoped run, layers our **force-keep** plugin (`-p teatree.quality.force_keep_plugin`) over the deselection: the always-run floor (`tests/quality`, `tests/integration`, `tests/conformance`), the reference-reader tests for a changed non-imported path (a doc, or a `dev/` lane runner — #3817), the mirror-convention test path, and the changed test files themselves are kept regardless of what the graph says. A change to the lane's OWN selection machinery (`SELECTION_DEFINING_PATHS`: the `quality` classifier modules, `cli/affected_tests_tools.py`, `dev/test-affected.sh`) forces FULL — a selection cannot validate its own change; bound `PYTEST_XDIST_AUTO_NUM_WORKERS` on a memory-tight host rather than skip the run (a dispatched agent sets that env var in its own brief — `/t3:rules` § "Sub-Agent Limitations"). The changed src modules run under `--doctest-modules` to match the CI shard flags. Zero test runs twice.

Degrades to a whole-tree FULL run with the plugin OFF (deterministically — over-run, never under-run) on any of: a changed `conftest.py` / `factories.py`; test settings (`tests/django_settings*`, `tests/config/**`); a migration (adds `--create-db`); a non-`.py` data file under `src/`/`tests/`; any file outside the modelled roots (`scripts/`, `hooks/`, `e2e/`, docs/skills `.md`); any deletion/rename; or a dirty merge-base. When the report says FULL, run the whole suite.

**Not a gate.** This is fast feedback only; a subset run cannot prove the 93% whole-tree coverage floor. Before pushing, run `bash dev/ci-parity-fast.sh` — the necessary-and-sufficient local check (scoped prek, `makemigrations --check`, this lane, the incremental push gate, no coverage floor). `bash dev/ci-parity.sh` is the opt-in deep lane for a red CI or a coverage-sensitive PR; the whole-tree floor is owned by it and by CI's sharded `test (3.13)` lane, never by a push hook.

**A hand-picked directory list is not a substitute.** `pytest tests/teatree_core tests/teatree_loop …` skips the always-run floor above, so a `tests/conformance` break — a new `ScanSignal` kind with no dispatch/statusline route — survives it. That reached CI twice ([#3787](https://github.com/souliane/teatree/pull/3787), [#3788](https://github.com/souliane/teatree/pull/3788)). Use `bash dev/test-affected.sh`, which force-keeps the floor; `tests/conformance` also runs unconditionally in `dev/push-gate.sh`, so it cannot be pushed red either way.

### Order-Dependence (Shuffle) Lane

```bash
bash dev/test-shuffle.sh              # seed 7 — the recorded #2359 Class B reproducer
bash dev/test-shuffle.sh 7 1 13 100   # CI's full matrix, serially
```

The local twin of CI's `test-shuffle` job: the curated order-safe directory set run serially (`-n0`) under shuffled collection, so a test that leaks process-global state and the victim it breaks land in a failing relative order.

**Never hand-roll it.** `pytest-randomly` is deliberately out of the default `dev` group, so `uv run pytest -p randomly … | tail -5` on a plain env raises `ImportError: Error importing plugin "randomly"` **and still exits 0** — a shell pipeline reports its LAST stage's status, so a lane that collected nothing reads green to anything gating on the exit code. The runner installs `--group shuffle`, preflights `import pytest_randomly` and fails loud, passes `-o required_plugins=pytest-randomly`, and never pipes pytest. `tests/test_ci_shuffle_lane_scope.py` pins each of those guards plus directory parity with the CI job.

### Frontend Lint

- Run the project's frontend lint command (extension point: `wt_lint_frontend`).
- Fix lint errors before pushing.

### E2E Testing

See [`../e2e/SKILL.md`](../e2e/SKILL.md) (`/t3:e2e`) for the full E2E workflow: writing tests, running them, visual snapshots, evidence posting, and the pre-push visual QA gate.

### Quality Check

- `t3 ci coverage` — print current coverage vs the configured floor; exits non-zero if any floor is missed. **Use this to confirm coverage still meets the gate after adding new code.**
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

#### Responding to Failed Jobs

A job shows `failed` (or any non-`success` state). Do NOT echo "green"/"passing" — act on it. The canonical next command is:

```bash
t3 ci fetch-failed-tests
```

That extracts the failing node IDs so you can reproduce locally; pair it with `t3 ci fetch-errors` for the logs. Then reproduce (`t3 <overlay> run tests --failed-first -- <node_id>`), fix the root cause, push, and re-monitor until every job is `success`. Never run a command that asserts the pipeline is green (e.g. `echo "CI passing"`) while any job is non-green.

### Docker Coverage — the opt-in deep lane, not a pre-push mandate

The 93% floor is a whole-tree property, so proving it locally is **not** what you do before every push — `bash dev/ci-parity-fast.sh` is (above), and CI's required sharded `test (3.13)` lane owns the floor. Reach for the deep lane when a PR is coverage-sensitive (deleting tests, adding a low-coverage module) or when reproducing a red CI:

```bash
bash dev/ci-parity.sh     # the exact blocking CI predicate, incl. dev/test-cov.sh + t3 ci coverage
bash dev/test-matrix.sh   # the same suite in Docker across Python 3.13 + 3.14
```

Never hand-roll the `docker run`: the runner pins the image, project environment, cache mounts and version set, so a hand-written invocation drifts from CI in exactly the way the Docker lane exists to catch. If the total is below the CI threshold, add tests before pushing.

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

- Read the PR/MR state and description via the `mcp__teatree__github_pr_get` / `mcp__teatree__gitlab_pr_get` MCP tool (structured JSON; fall back to the issue tracker CLI — `glab mr view`, `gh pr view` — when the MCP server isn't connected)
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
