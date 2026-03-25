---
name: t3-test
description: Testing, QA, and CI — running tests, analyzing failures, quality checks, CI interaction, test plans, and posting testing evidence. Use when user says "run tests", "pytest", "lint", "e2e", "CI failed", "pipeline", "test plan", "QA", or any test/CI task.
compatibility: macOS/Linux, pytest, linting tools, CI CLI (glab/gh).
requires:
  - t3-workspace
  - t3-rules
  - t3-platforms
triggers:
  priority: 20
  keywords:
    - '\b(run.*tests?|pytest|lint|sonar|e2e|ci fail|pipeline fail|what tests|tests? broke|test runner)\b'
    - '\bpipeline\b.*(fail|red|broke)'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Testing, QA & CI

Running tests, analyzing failures, quality checks, CI interaction, test plans, and testing evidence.

## Dependencies

- **t3-workspace** (required) — provides server and environment context. **Load `/t3-workspace` now** if not already loaded.

## Workflows

### Backend Tests

**Prerequisites:** Docker services (Postgres, Redis) must be running. Start them via `t3 lifecycle start` (see `/t3-workspace`) rather than raw `docker compose`. Read the project's test reference (e.g. `references/running-tests-and-lint.md`) for the full setup steps.

- `t3 run tests` — run the project test suite (extension point: `wt_run_tests`).
- Flags: `--reuse-db`, `--failed-first`, optional `--parallel`.
- Always run with `--reuse-db` for speed unless schema changed.
- Use `--failed-first` to quickly re-verify fixes.

### Frontend Lint

- `nx run-many --target=lint` — lint all frontend projects.
- Fix lint errors before pushing.

### E2E Testing

- Playwright-based E2E tests.
- **Always run headless** with `CI=1`.
- `t3 ci trigger-e2e` — trigger E2E tests on CI (extension point: `wt_trigger_e2e`).

**Full worktree per MR (Non-Negotiable):** Each MR under test MUST have its own full worktree setup (backend + frontend via `t3 lifecycle setup` + `t3 lifecycle start`). Never mix backends from one worktree with frontends from another. Never patch an incomplete worktree by hand — if it's missing repos, env files, or DB, delete it and start over with `t3 workspace ticket`. Anti-pattern: manually adding repos with `git worktree add`, copying env files, editing `.env.worktree` by hand.

**E2E for backend/API changes (Non-Negotiable):** When backend or microservice changes affect data visible in the frontend (e.g., webhook payload fields, API serializer fields, new model fields exposed via API), E2E tests are still required even if there is no frontend MR. The frontend form already has the fields — E2E proves the end-to-end data flow. Do NOT skip E2E just because the change is "backend-only."

**`storageState` in Playwright:** `test.use({ storageState: undefined })` means "use default" (inherits global setup state). For truly unauthenticated tests, use `test.use({ storageState: { cookies: [], origins: [] } })`.

**Establish baseline before attributing failures (Non-Negotiable):** When running E2E tests to validate a change, first run the same test on the **default branch** (or the unmodified code) to confirm it passes without your changes. If the test already fails on the default branch, it is a pre-existing failure — do not waste time debugging it as if your changes caused it. Report it as pre-existing and move on.

### Private Test Suite

E2E and integration tests ideally live in the project repo they test (e.g., the frontend repo's `e2e/` directory). But sometimes a **separate test repo** reduces friction — no conflicts with the QA team's tests, no build pipeline overhead, freedom to use different tooling or test data. This is especially useful for personal verification tests that complement (not replace) the project's official suite.

**Setup:**

- Set `T3_PRIVATE_TESTS` in `~/.teatree` to the path of your private test repo (e.g., `$HOME/workspace/my-e2e`). Skills use this to locate and run your tests.
- Structure tests by app and feature: `tests/<app>/<feature-area>/<test-file>`
- Store artifacts (screenshots, recordings) in a git-tracked `artifacts/<TICKET>/` directory for proof.

**Prerequisites:** Always start dev servers via `t3 lifecycle start` (see `/t3-workspace`) before running tests. Never start services manually. Before running E2E tests, verify that **translations are loaded** — the frontend i18n directory is gitignored and only populated at startup (by `t3 lifecycle start`). If the frontend was started manually, translations will be missing. Quick check: open any page and confirm labels show human-readable text, not raw keys like `app.feature.xxx.label`.

**Test depth (Non-Negotiable):** Don't just verify "page loads with 200". Read the source code to understand what the feature does, then test specific behaviors: form fields, filters, CRUD operations, access control, edge cases.

**Test integrity (Non-Negotiable):** Never weaken, simplify, or remove test cases to work around failures. If a test fails, fix the underlying issue (environment, selectors, timing) — don't dilute the test. Tests must cover both happy paths and edge cases.

**Evidence posting:** After a green run, embed key screenshots into the test plan comment on the MR (see § "Post Testing Evidence on MR"). Evidence should read as manual testing — never mention automation tools.

### Test Tracking Files

Each test file can have a sibling `.md` with the same basename — a single source of truth for what has been tested and posted per ticket.

**Format:**

```markdown
# <test-name>

One-line description.

## Tickets

| Ticket | MR | Description | Comment |
|--------|-----|-------------|---------|
| [PROJ-1234](https://example.com/tickets/PROJ-1234) | [!5678](https://example.com/mrs/5678) | Initial: feature X | [Plan + images](https://example.com/mrs/5678#note_123) |
| [PROJ-5678](https://example.com/tickets/PROJ-5678) | [!5700](https://example.com/mrs/5700) | Modified: behavior Y | Draft |

## Key Screenshots

![screenshot](https://example.com/artifacts/PROJ-1234/screenshot.png)
```

**Rules:**

- One row per ticket that created or modified the test.
- Comment column: `—` (not written), `Draft` (local only), `[Plan only](https://example.com/mrs/5678#note_123)`, or `[Plan + images](https://example.com/mrs/5678#note_123)`.
- Draft sections: `## Draft: <TICKET>` with the full comment body. When posting: upload images, replace relative paths, post, update the table, delete the draft section.
- Before posting: check the `.md` file for existing evidence to avoid duplicates.

### Batch Testing for Open MRs

When reviewing open MRs, test all MRs that change visible behavior — not just the current one. Unit tests alone are not enough to ship.

1. List all open non-draft MRs across repos in scope.
2. For each MR that modifies UI, forms, or user-facing logic: create a worktree and write/run tests.
3. Skip MRs that only change CI config, linting, or non-visible code.
4. Post test evidence on each MR after a green run.

### SonarQube Quality Check

- `t3 ci quality-check` — quality analysis (extension point: `wt_quality_check`).
- Run before finalizing to catch quality issues early.

### CI Interaction

- `t3 ci fetch-failed-tests` — extract failed test node IDs from CI (extension point: `wt_fetch_failed_tests`).
- `t3 ci fetch-errors` — extract error logs from CI (extension point: `wt_fetch_ci_errors`).
- Run failed tests locally to reproduce before fixing.

### CI Pipeline Monitoring

- Background polling for pipeline status.
- Costs no tokens while waiting.

### Fix-Push-Monitor Loop

When CI fails:

1. Fetch failures (`t3 ci fetch-failed-tests`)
2. **Check if the failure is pre-existing** (file never touched by branch) → if so, delegate to `/t3-ship` § "Isolate Unrelated Fixes"
3. Run failed tests locally to reproduce
4. Fix the issue
5. Push a regular commit (no squash/rebase)
6. Monitor pipeline again
7. Loop until green

## Generate Test Plan for MR

Analyze MR changes and produce a manual test plan. Use when the user says "test plan", "QA", or wants to document what to verify before merging.

### 1. Gather Context

- Read MR description via the issue tracker CLI (e.g., `glab mr view`, `gh pr view`)
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

### 3. Post Test Plan to MR

Post the test plan as a comment on the MR. If a test plan comment already exists, skip posting (don't duplicate).

See your [issue tracker platform reference](../t3-platforms/references/) § "MR Notes" for the posting recipe.

## Post Testing Evidence on MR

After confirming a feature works (via E2E or manual verification), embed screenshot(s) **directly in the test plan comment** — not as a separate comment. Each verified checkbox gets checked, and a screenshot goes below the relevant section.

### 1. Upload Screenshot(s)

Upload the screenshot via the platform's file upload API and get the markdown embed syntax. See your [issue tracker platform reference](../t3-platforms/references/) § "File Uploads" for the recipe.

### 2. Post or Update Comment — Always Use Python (Non-Negotiable)

Shell variable interpolation and `jq --arg` both escape `!` to `\!`, breaking image syntax `![alt](url)`. **Always** use inline Python with the note body as a heredoc string — see [`../t3-rules/SKILL.md`](../t3-rules/SKILL.md) § "Temp File Safety" for the full rules. See your [issue tracker platform reference](../t3-platforms/references/) § "MR Notes (Comments)" for the complete Python recipe.

When editing: check the verified boxes (`- [ ]` → `- [x]`), insert screenshot markdown below the relevant section, leave unchecked items as-is.

### Rules

- **One screenshot per verified section** — not per checkbox, not a flood. One image that proves the section works.
- **Cover ALL sections on first post** — don't post a partial set and wait for the user to ask for more. Every verified section should have its screenshot from the start.
- **Screenshots go below the section they verify**, not at the bottom of the comment
- **Clickable references:** Every MR, ticket, or note reference must be a markdown link — see [`../t3-rules/SKILL.md`](../t3-rules/SKILL.md) § "Clickable References".
- Write in first person, casual tone ("Tested on local...", "Works as expected")
- **Never mention automation, E2E, Playwright, or scripts** — must read as manual testing
- Prefer editing the existing test plan over posting a second comment
- If a separate evidence comment already exists, delete it after merging evidence into the test plan

## Command Reference

| Command | Purpose |
|---------|---------|
| `t3 <overlay> run tests` | Run project tests |
| `t3 ci fetch-failed-tests` | Extract failed test IDs from CI |
| `t3 ci fetch-errors` | Extract error logs from CI |
| `t3 ci quality-check` | Quality analysis |
| `t3 ci trigger-e2e` | Trigger E2E tests on CI |

## Verification Before Claims

**Iron law:** No completion claims without fresh verification evidence.

| Claim | Required evidence |
|---|---|
| "Tests pass" | Test runner output showing green |
| "Lint is clean" | Linter output with zero errors |
| "No regressions" | Diff review + relevant test output |
| "Services are running" | HTTP checks returning expected status codes (2xx/3xx) |
| "Evidence posted" | HTTP 200 from API + note/comment ID in output |
| "MR updated" | Confirmed via API response, not just "script ran" |

### Screenshot Sanity Check (Non-Negotiable)

Before claiming E2E success or posting screenshots as evidence, **visually inspect every screenshot** for environment issues. Reject and fix if any of these are present:

- **Missing translations:** Labels show raw keys like `app.feature.xxx.label` or `app.question.xxx` instead of human-readable text. Cause: frontend started without the translation sync step (handled by `t3 run frontend` / `t3 lifecycle start`). Fix: restart via `t3 lifecycle start`.
- **Missing static files:** Broken image icons, unstyled pages, or 404s for assets. Cause: static asset build/collection not run. Fix: restart via `t3 lifecycle start` (which handles asset preparation).
- **Console errors:** Open browser devtools and check for blocking JS errors before taking screenshots.

A screenshot with raw translation keys is **not valid evidence** — it proves the environment was broken, not that the feature works.

### Store Contamination Check (Non-Negotiable)

E2E tests for features that load data via store dispatches (e.g., NgRx `loadResources`) must verify the data is loaded **from the tested page**, not from a prior navigation. If you visit page A (which loads resources into the store) and then navigate to page B (which reads from the store but never dispatches the load), page B will appear to work — but only because page A pre-populated the store.

- **Each test must start from a clean state** — navigate directly to the page under test without visiting other pages first.
- **Verify the page dispatches its own data load** — check the component source for the relevant dispatch call. If missing, the feature has a bug (empty dropdown, missing data), regardless of what the screenshot shows.
- **Empty dropdowns/lists are a red flag** — if a modal or form shows "No items found", do NOT mark the test as passing. Investigate whether the data load is missing.
