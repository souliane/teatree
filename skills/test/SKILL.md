---
name: test
description: Testing, QA, and CI — running tests, analyzing failures, quality checks, CI interaction, test plans, and posting testing evidence. Use when user says "run tests", "pytest", "lint", "e2e", "CI failed", "pipeline", "test plan", "QA", or any test/CI task.
compatibility: macOS/Linux, pytest, linting tools, CI CLI (glab/gh).
requires:
  - workspace
  - rules
  - platforms
triggers:
  priority: 20
  keywords:
    - '\b(run.*tests?|pytest|lint|sonar|e2e|ci fail|pipeline fail|what tests|tests? broke|test runner)\b'
    - '\bpipeline\b.*(fail|red|broke)'
search_hints:
  - test
  - pytest
  - e2e
  - lint
  - ci
  - pipeline
  - qa
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

- **Django test client** (`client.get(...)`, `client.post(...)`) for views, URL, HTMX endpoints.
- **`call_command("name", ...)`** for management commands (Typer + Django glue).
- **Real overlay against `tmp_path`** with real `git init` for provisioning, worktrees, env files.
- **Playwright** (in `e2e/` or the project's E2E repo) for dashboard or browser-visible behavior.
- **`subprocess.run(["t3", ...])`** (mark `@pytest.mark.integration`) when only the real CLI entry point reproduces the bug.

**Mock only unstoppable externals:** network calls (GitHub / GitLab / Slack / Sentry), the clock (`time_machine`), third-party subprocesses. Don't mock teatree code, Django models, filesystem under `tmp_path`, or `git` — run the real thing. If setup is painful, that usually points at a design problem, not a need for mocks.

**Unit tests** are reserved for pure logic: parsers, formatters, branch-name / slug builders, regex validators, anything with no I/O. A unit test for glue code that's already covered by an integration test is duplicate coverage — delete it.

The repo's `AGENTS.md` § "Test-Writing Doctrine" carries the authoritative rule and the review gate. `t3:review` enforces it per-MR (§ "New-Test Shape Check"). When rebalancing existing tests, the coverage gate must not drop.

## Workflows

### Backend Tests

**Prerequisites:** Docker services (Postgres, Redis) must be running. Start them via `t3 <overlay> lifecycle start` (see `/t3:workspace`) rather than raw `docker compose`. Read the project's test reference (e.g. `references/running-tests-and-lint.md`) for the full setup steps.

- `t3 <overlay> run tests` — run the project test suite.
- Flags: `--reuse-db`, `--failed-first`, optional `--parallel`.
- Always run with `--reuse-db` for speed unless schema changed.
- Use `--failed-first` to quickly re-verify fixes.

### Frontend Lint

- Run the project's frontend lint command (extension point: `wt_lint_frontend`).
- Fix lint errors before pushing.

### E2E Testing

- Playwright-based E2E tests.
- **Always run headless** with `CI=1`.
- `t3 ci trigger-e2e` — trigger E2E tests on CI.

**Full worktree per MR (Non-Negotiable):** Each MR under test MUST have its own full worktree setup (backend + frontend via `t3 <overlay> lifecycle setup` + `t3 <overlay> lifecycle start`). Never mix backends from one worktree with frontends from another. Never patch an incomplete worktree by hand — if it's missing repos, env files, or DB, delete it and start over with `t3 <overlay> workspace ticket`. Anti-pattern: manually adding repos with `git worktree add`, copying env files, editing `.env.worktree` by hand.

**E2E for backend/API changes:** When backend or microservice changes affect data visible in the frontend (e.g., webhook payload fields, API serializer fields, new model fields exposed via API), E2E tests are still required even if there is no frontend MR. The frontend form already has the fields — E2E proves the end-to-end data flow. Do NOT skip E2E just because the change is "backend-only."

**`storageState` in Playwright:** `test.use({ storageState: undefined })` means "use default" (inherits global setup state). For truly unauthenticated tests, use `test.use({ storageState: { cookies: [], origins: [] } })`.

**Establish baseline before attributing failures (Non-Negotiable):** When running E2E tests to validate a change, first run the same test on the **default branch** (or the unmodified code) to confirm it passes without your changes. If the test already fails on the default branch, it is a pre-existing failure — do not waste time debugging it as if your changes caused it. Report it as pre-existing and move on.

**Pixel-stable visual snapshots** (`pytest-playwright-visual`, `assert_snapshot`): the plugin hard-fails on any single-pixel mismatch, so snapshot tests are only reproducible when every source of visual drift is pinned. Eliminate in this order before regenerating baselines:

- **Dynamic data in seeded fixtures.** Freeze timestamps (`TaskAttempt.objects.update(started_at=frozen, ended_at=frozen)`), pin any `now()` values. Signal handlers that run on model creation (e.g. immediate-backend) add fresh timestamps — update them after the signal fires.
- **Git metadata in headers.** The dashboard header prints `git rev-parse --short HEAD` + branch; these change on every commit. Override via env vars (`TEATREE_E2E_GIT_SHA`, `TEATREE_E2E_GIT_BRANCH`) read by the view. Set them at **module level** in `conftest.py` so `subprocess.Popen(env=os.environ)` propagates them to the uvicorn subprocess — patching the test process alone is not enough.
- **Animations and caret blink.** Playwright's `animations="disabled"` only handles CSS animations it knows about. Add a session-scoped `page.add_init_script` that injects `*{animation-duration:0s!important;transition-duration:0s!important;caret-color:transparent!important}`. Combine with `reduced_motion: "reduce"` in `browser_context_args`.
- **Font antialiasing across architectures.** Apple Silicon Docker (arm64) and x86_64 CI render fonts at different heights. Force `platform: linux/amd64` on the e2e compose service so locally-regenerated baselines match CI. Even then, leave ~0.5% pixel tolerance for residual antialiasing noise.
- **Plugin pixel tolerance.** `pytest-playwright-visual`'s strict `if mismatch == 0:` check can be relaxed with `patchy`, but the fixture is decorated with `@pytest.fixture` — patchy's re-exec re-applies the decorator, producing a `FixtureFunctionDefinition` with no `__code__`. Patch `.__wrapped__` (the underlying function) while temporarily rebinding `pytest.fixture` to identity; the outer `FixtureFunctionDefinition` keeps wrapping the patched function. See `e2e/conftest.py` for the working pattern.

**Regenerate baselines inside the same Docker image CI uses.** Never regenerate on the host with `uv run pytest --update-snapshots` — macOS Chromium renders differently. Use `t3 teatree e2e project --update-snapshots` (which runs in the pinned Docker image).

### Private Test Suite

E2E and integration tests ideally live in the project repo they test (e.g., the frontend repo's `e2e/` directory). But sometimes a **separate test repo** reduces friction — no conflicts with the QA team's tests, no build pipeline overhead, freedom to use different tooling or test data. This is especially useful for personal verification tests that complement (not replace) the project's official suite.

**Setup:**

- Set `T3_PRIVATE_TESTS` in `~/.teatree` to the path of your private test repo (e.g., `$HOME/workspace/my-e2e`). Skills use this to locate and run your tests.
- Structure tests by app and feature: `tests/<app>/<feature-area>/<test-file>`
- Store artifacts (screenshots, recordings) in a git-tracked `artifacts/<TICKET>/` directory for proof.

**Prerequisites:** Always start dev servers via `t3 <overlay> lifecycle start` (see `/t3:workspace`) before running tests. Never start services manually. Before running E2E tests, verify that **translations are loaded** — the frontend i18n directory is gitignored and only populated at startup (by `t3 <overlay> lifecycle start`). If the frontend was started manually, translations will be missing. Quick check: open any page and confirm labels show human-readable text, not raw keys like `app.feature.xxx.label`.

**Test depth:** Don't just verify "page loads with 200". Read the source code to understand what the feature does, then test specific behaviors: form fields, filters, CRUD operations, access control, edge cases.

**Component placement:** Before writing E2E tests for a UI component, check the **routing module** to find which page/route renders it. Components may only appear at specific wizard steps or behind navigation — not on the page you'd naively navigate to. Grep for the component selector in `.html` templates to find its host, then check the routing module for the URL path.

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

### Fix-Push-Monitor Loop

When CI fails:

1. Fetch failures (`t3 ci fetch-failed-tests`)
2. **Check if the failure is pre-existing** (file never touched by branch) → if so, delegate to `/t3:ship` § "Isolate Unrelated Fixes"
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

See your [issue tracker platform reference](../platforms/references/) § "MR Notes" for the posting recipe.

## Pre-Push Browser Sanity Gate (Visual QA)

`t3 <overlay> pr create` runs a pre-push browser sanity gate as a side effect of the shipping flow. It loads the page(s) the branch diff touches, captures silent-render regressions (crashes, console errors, raw `app.*` keys, blocking 404s), and records the summary on `Ticket.extra['visual_qa']`. See [`../ship/SKILL.md`](../ship/SKILL.md) § "4c. Visual QA Gate" for the blocking behavior and bypass flags.

This gate is **not a replacement for manual E2E evidence** — it only catches silent-render regressions before push. Feature verification still requires the workflows below.

## Post Testing Evidence on MR

**Use `t3 <overlay> pr post-evidence` first.** If the CLI command handles uploading and posting, use it instead of manual API calls. Only fall back to manual posting if the CLI doesn't support the required operation.

After confirming a feature works (via E2E or manual verification), embed screenshot(s) and video(s) **directly in the test plan comment** — not as a separate comment. Each verified checkbox gets checked, and a screenshot goes below the relevant section.

### 1. Upload Screenshot(s) and Video(s)

Upload via the platform's file upload API and get the markdown embed syntax. See your [issue tracker platform reference](../platforms/references/) § "File Uploads" for the recipe.

**Video embedding:** Use the same `![alt](url)` markdown syntax as images. GitLab auto-detects video formats (.webm, .mov, .mp4) and renders an inline player. Do NOT use `<video>` HTML tags — they don't work in GitLab markdown.

### 2. Post or Update Comment — Always Use Python

Shell variable interpolation and `jq --arg` both escape `!` to `\!`, breaking image syntax `![alt](url)`. **Always** use inline Python with the note body as a heredoc string — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Temp File Safety" for the full rules. See your [issue tracker platform reference](../platforms/references/) § "MR Notes (Comments)" for the complete Python recipe.

When editing: check the verified boxes (`- [ ]` → `- [x]`), insert screenshot markdown below the relevant section, leave unchecked items as-is.

### Visual Comparison Format

Evidence screenshots must always use a **side-by-side comparison table**. Minimum 2 columns, up to 3 when a design mockup is available.

**2-column format** (no design mockup available):

```markdown
| Before (current UI) | After (this MR) |
|---|---|
| ![before](url) | ![after](url) |
```

**3-column format** (design mockup available -- from Figma, ticket description, etc.):

```markdown
| Before (current UI) | Design mockup | After (this MR) |
|---|---|---|
| ![before](url) | ![mockup](url) | ![after](url) |
```

- **Always label columns explicitly** -- "Before", "After", "Design mockup" (or "Figma design").
- **"Before" = default branch UI** for the same screen. Capture it before switching to the feature branch.
- **"After" = feature branch UI** after the change.
- **"Design mockup"** = Figma screenshot (via direct API), image from ticket description, or any reference design. Fetch Figma frames with the project's `figma_screenshot.py` script if available, or download from the ticket.
- One comparison table per verified section, placed below the section heading.

### Rules

- **One screenshot per verified section** — not per checkbox, not a flood. One image that proves the section works.
- **Cover ALL sections on first post** — don't post a partial set and wait for the user to ask for more. Every verified section should have its screenshot from the start.
- **Screenshots go below the section they verify**, not at the bottom of the comment
- **Clickable references:** Every MR, ticket, or note reference must be a markdown link — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Clickable References".
- Write in first person, casual tone ("Tested on local...", "Works as expected")
- **Never mention automation, E2E, Playwright, or scripts** — must read as manual testing
- **Update the existing test plan** — never post a second evidence comment. Always GET existing notes first, find the test plan, and PUT to update it.
- If a separate evidence comment already exists, delete it after merging evidence into the test plan
- **Don't post code findings as bugs without asking the user first.** Behavior that looks like a bug may be intentional. Ask "Is this expected?" before posting a finding on the MR.
- **Match evidence type to MR type.** UI screenshots are evidence for frontend MRs. Backend MRs need backend evidence: unit test output, API response diffs, or logs. Don't post frontend screenshots on a backend-only MR — they prove the frontend works, not the backend fix. When both MRs exist, put screenshots on the frontend MR and reference it from the backend MR.

## Re-Read Before Debugging

When an E2E test fails or the environment misbehaves, **re-read this skill's verification sections** (Screenshot Sanity Check, Store Contamination Check, Establish Baseline) before spending more than 2 minutes on ad-hoc debugging. Skill guidance loaded at the start of a long session gets compressed out of active context — re-reading takes 10 seconds and prevents 30-minute debugging detours.

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

### Browser Console First (Non-Negotiable)

When an E2E test shows missing UI elements (empty form, blank section, component not rendering), **capture browser console errors before investigating component code.** Add `page.on('console', ...)` and `page.on('pageerror', ...)` listeners to your test. Runtime errors like `"Undefined form configuration!"` or `"Cannot read property of undefined"` reveal the root cause in seconds — investigating framework internals (change detection, signal timing, template rendering) without this context wastes hours.

### Screenshot Sanity Check (Non-Negotiable)

Before claiming E2E success or posting screenshots as evidence, **visually inspect every screenshot** for environment issues. Reject and fix if any of these are present:

- **Missing translations:** Labels show raw keys like `app.feature.xxx.label` or `app.question.xxx` instead of human-readable text. Cause: frontend started without the translation sync step (handled by `t3 <overlay> run frontend` / `t3 <overlay> lifecycle start`). Fix: restart via `t3 <overlay> lifecycle start`.
- **Missing static files:** Broken image icons, unstyled pages, or 404s for assets. Cause: static asset build/collection not run. Fix: restart via `t3 <overlay> lifecycle start` (which handles asset preparation).
- **Console errors:** Open browser devtools and check for blocking JS errors before taking screenshots.
- **Feature element not visible:** The screenshot must show the specific UI element being tested (badge, field, status indicator), not just the top of the page. Use `element.scrollIntoViewIfNeeded()` before taking screenshots. A screenshot of "Personal Data" doesn't prove the "ID" section badge is correct.

A screenshot with raw translation keys is **not valid evidence** — it proves the environment was broken, not that the feature works. A screenshot that doesn't show the tested element is **not valid evidence** either — it proves nothing.

### Store Contamination Check

E2E tests for features that load data via a state management store must verify the data is loaded **from the tested page**, not from a prior navigation. If you visit page A (which dispatches a data load into the store) and then navigate to page B (which reads from the store but never dispatches the load itself), page B will appear to work — but only because page A pre-populated the store.

- **Each test must start from a clean state** — navigate directly to the page under test without visiting other pages first.
- **Verify the page dispatches its own data load** — check the component source for the relevant dispatch/action call. If missing, the feature has a bug (empty dropdown, missing data), regardless of what the screenshot shows.
- **Empty dropdowns/lists are a red flag** — if a modal or form shows "No items found", do NOT mark the test as passing. Investigate whether the data load is missing.
