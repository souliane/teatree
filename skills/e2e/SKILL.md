---
name: e2e
description: End-to-end testing with Playwright — writing tests, running them, visual snapshots, evidence posting, and the pre-push visual QA gate. Use when user says "e2e", "playwright", "write e2e", "run e2e", "visual qa", "screenshot", "post evidence", or is working with Playwright-based tests.
compatibility: macOS/Linux, Playwright, Node.js, t3 CLI.
requires:
  - test
  - workspace
  - platforms
metadata:
  version: 0.0.1
  subagent_safe: false
---

# E2E Testing

Playwright-based end-to-end testing for overlay target applications. Covers writing tests, running them, visual snapshots, evidence posting, and the pre-push visual QA gate.

## Dependencies

- **t3:test** (required) — general testing patterns and CI interaction.
- **t3:workspace** (required) — worktree and dev server management.

## Setup & Prerequisites

**Full worktree per PR (Non-Negotiable):** Each PR under test MUST have its own full worktree setup (backend + frontend via `t3 <overlay> worktree provision` + `t3 <overlay> worktree start`). Never mix backends from one worktree with frontends from another. Never patch an incomplete worktree by hand — if it's missing repos, env files, or DB, delete it and start over with `t3 <overlay> workspace ticket`.

Always start dev servers via `t3 <overlay> worktree start` before running tests. Never start services manually. Before running E2E tests, verify that **translations are loaded** — the frontend i18n directory is gitignored and only populated at startup. If the frontend was started manually, translations will be missing. Quick check: open any page and confirm labels show human-readable text, not raw keys like `app.feature.xxx.label`.

## Claude in Chrome connectivity

**Automated account-switch recovery (#1916).** A `/login` switch is now detected without manual checks: the `SessionStart` hook compares the active `~/.claude.json` `oauthAccount.accountUuid` against the last-recovered one and, on a change, prepends a directive telling you to run `t3 doctor check` (or `t3 setup recover-account-switch`). That command invalidates the teatree backend token cache and live-re-probes each Slack/Notion connector's `auth.test`, recording the new account so the notice clears. It exits non-zero when a connector is still unreachable. The Slack/Notion legs are automatic; the Claude-in-Chrome extension re-pairing below is still manual (item (a)).

Browser-driven E2E and visual checks run through the Claude-in-Chrome MCP server. Two failure modes are easy to misread as "the browser is broken" when the fix is mechanical.

**Diagnosis one-liner: MCP server connected ≠ extension connected.** `/mcp` showing the `claude-in-chrome` server green only proves the MCP server reachable — it does NOT prove the extension is paired with the active account.

**(a) Logged into claude.ai ≠ extension connected.** Being signed into claude.ai in the browser does not connect the extension — the extension popup carries its **own** connection / sign-in state. After any account switch, every browser tool can return "extension not connected" while `/mcp` shows the server reconnected.

- **Verify:** call `list_connected_browsers`. An **empty array** (`[]`) means the extension is not paired with THIS account — zero instances are connected.
- **Fix:** open the extension popup → sign out → sign in with the active account → Connect. Do a **full browser restart** if it still reports empty, then re-run `list_connected_browsers` to confirm a non-empty result before proceeding.

This is the empirical fallout item (c) from the account-switch checklist in [souliane/teatree#1916](https://github.com/souliane/teatree/issues/1916).

**(b) Navigation can silently block on per-origin permission prompts.** A `navigate` call can stall on a per-origin permission prompt the user has to grant. In an interactive session this surfaces as an `AskUserQuestion` fallback you answer once per origin. For an automated/unattended run, pre-authorize the browser MCP tools in `~/.claude/settings.json` so the tool itself never prompts:

```jsonc
{
  "permissions": {
    "allow": [
      "mcp__claude-in-chrome__navigate",
      "mcp__claude-in-chrome__*"
    ]
  }
}
```

**Research finding — MCP allow-rules cannot constrain by domain, and wildcard subdomains are NOT supported.** Per the [Claude Code permission rule syntax](https://docs.claude.com/en/docs/claude-code/permissions#mcp), an MCP specifier matches only by server and tool name — `mcp__server`, `mcp__server__*`, or `mcp__server__tool_name`. There is **no** argument/domain form: you cannot write `mcp__claude-in-chrome__navigate(domain:*.example.com)` the way you can write `WebFetch(domain:example.com)`. So a browser-navigation allow-rule is all-or-nothing per tool — it auto-approves the `navigate` tool for **every** origin, not a wildcard-subdomain subset. (`WebFetch(domain:...)` and the bash sandbox's `allowedDomains` do support `*.example.com`, but those govern `WebFetch` and Bash, not the Claude-in-Chrome MCP tools.) The per-origin grant the browser itself enforces is upstream of permission rules; auto-approving the tool removes the Claude Code prompt, not the browser's own origin gate. Upstream feature gap: there is no per-origin allow-list for MCP browser tools today — track it against the account-switch automation in [souliane/teatree#1916](https://github.com/souliane/teatree/issues/1916).

## Running E2E Tests

- Run headless with `CI=1`.
- `t3 <overlay> e2e` — run E2E tests locally.
- `t3 ci trigger-e2e` — trigger E2E tests on CI.

**E2E for backend/API changes:** When backend or microservice changes affect data visible in the frontend (e.g., webhook payload fields, API serializer fields, new model fields exposed via API), E2E tests are still required even if there is no frontend PR. The frontend form already has the fields — E2E proves the end-to-end data flow. Do NOT skip E2E just because the change is "backend-only."

## Dual-Env Testing (one spec, DEV or local)

A single spec should run against either the deployed **dev** environment or the **local** stack, selected by one CLI argument. Determinism comes from code, never from a parsed file.

**Target selection.** `t3 <overlay> e2e [run|external|project] --target dev|local`:

- `dev` — keep the pre-set `BASE_URL` (deployed env); no local port scan.
- `local` — always discover the local frontend, even if a stray `BASE_URL` is exported (so `--target local` can never silently hit a deployed env).
- omitted — back-compat: infer `dev` if `BASE_URL` is set, else `local`.

The resolved value is exported as **`T3_E2E_TARGET`**. The spec branches on it — `const IS_DEV = process.env.T3_E2E_TARGET === 'dev'` — and must not re-derive the target from a `BASE_URL` host regex. Prefer testing a deployed/merged change against `dev`; an unmerged change must still pass on `local`.

**Recording DEV-vs-local discrepancies (typed sidecar, not prose).** When a spec must behave differently per target (different field labels in a regulated vs internal document, a DEV-only cross-check, a feature whose data only exists on one side), encode it in a **typed TypeScript sidecar the spec imports** (e.g. `<spec>.dualenv.ts` exporting a typed `DualEnvSpec`). `tsc` type-checks it; nothing parses Markdown/YAML to drive behavior. The sidecar is the durable, machine-enforced record of every known divergence and of any fixture provenance.

**Replicating a DEV object to local.** To test a not-yet-deployed feature locally, anchor on a real reproducible DEV object (read it read-only — authorized) and ensure it exists in the local DB. The local DB must be a DEV dump (use DSLR; if the object is missing, ask the user for a fresh dump — agents must never set `T3_ALLOW_REMOTE_DUMP`). Provisioning is at most two `t3` CLI invocations (provision/refresh, then run); fold password reset and access seeding into the provision step (`t3 <overlay> db refresh` already resets passwords).

**Documented limitation — some features are DEV-only on local.** DSLR snapshots legitimately lack certain data catalogs (e.g. the Excel-priced bandwidth product catalog). A feature that depends on such a catalog **cannot be reproduced on the local stack from DSLR**, regardless of snapshot age. This is not a bug to fix — record it in the spec's typed sidecar as a DEV-only divergence so the spec runs that feature against `dev` only and the limitation stays visible and enforced. Pin the run to the intended worktree's stack; cross-worktree container/DB drift causes silent mis-targeting.

**The reproducible dual-env recipe (five reusable sub-patterns).** Getting one spec to run reliably on both `dev` and a restored-dump `local` stack converges on the same five moves every time. Apply them as a checklist rather than rediscovering each serially (data-completeness walls surface one at a time — each found only after clearing the previous, which is what turns a small fix into a multi-day detour):

1. **Permission-scaffolding-as-sanctioned-setup.** A restored dump often lacks the relational links (user↔org/role rows the queryset filter traverses) that let any user *see* the target object — so the API returns empty for every user. Synthesizing the **minimal, idempotent, local-only visibility scaffolding** to make the test user reach the real object is sanctioned fixture setup, not faking: the data path stays real, the assertion is unchanged, permissions are not what's under test. Requires an explicit user ruling the first time; once ruled, encode it as an idempotent fixture auto-run by global-setup. **Hard boundary:** never synthesize or touch the thing under test (the asserted value, the priced data, the rendered output) — those must be produced by the real fixed code path.
2. **Reuse, don't create.** Creating the domain object via its write API often hits a setup-only dependency the dump lacks (a required system user the create path looks up → HTTP 500). **Reuse a real pre-existing object from the dump** instead — it sidesteps the entire write path and its missing dependencies. Keep the `dev` target's create path unchanged; only `local` reuses.
3. **Settle via the real flow.** A reused draft/object may carry **persisted stale values** even when the API recompute is correct — a downstream renderer reads the persisted items, not the live recompute, so it shows the pre-fix value despite a correct fix. Settle the object through the **real recompute-and-persist flow** (sanctioned setup) so the renderer sees fixed values; never inject the asserted value to "fix" the divergence.
4. **Deterministic endpoint.** When the local stack fronts multiple backend processes (e.g. an nginx round-robin across two backends), API calls land non-deterministically and flake. Resolve the backend port deterministically and use the auth scheme the restored-dump stack expects (e.g. token auth), so every call hits a predictable target. For the **`local` target the runner exports `COMPOSE_PROJECT_NAME`** = the resolved worktree's teatree compose project, so a spec that resolves the backend / fetches an artifact via a bare `docker compose port web 8000` / `docker compose exec -T web` (run from the backend repo dir, no `-p`) deterministically hits the teatree-provisioned stack whose `web` container has the restored-Postgres `DATABASE_URL` injected — instead of defaulting to the directory basename and missing it. No spec change is needed: `docker compose` honours `COMPOSE_PROJECT_NAME` natively.
5. **Target-aware assertions.** The same feature legitimately presents differently per environment (a DEV object may exhibit a single-variability case while the reused local object is a real combined-variability case; a regulated vs internal document uses different labels). The assertion must branch on `T3_E2E_TARGET` via the typed sidecar — never assume the two environments yield identical output.

**Branch-currency precheck (make it prominent).** Before any local-FULL verdict, assert the fix is actually present: `git merge-base --is-ancestor <fix-sha> HEAD`. A worktree silently behind the default branch renders the *pre-fix* value, manufacturing a "fix incomplete" false alarm. This is a precondition of the verdict, not optional discipline — see workspace `references/troubleshooting.md` § "Verify-Before-Relay".

## Writing Tests

**Test depth:** Don't just verify "page loads with 200". Read the source code to understand what the feature does, then test specific behaviors: form fields, filters, CRUD operations, access control, edge cases.

**Component placement:** Before writing E2E tests for a UI component, check the **routing module** to find which page/route renders it. Components may only appear at specific wizard steps or behind navigation — not on the page you'd naively navigate to. Grep for the component selector in templates to find its host, then check the routing module for the URL path.

**Mocking — stub with the error status the failure-path expects, not `200`.** When stubbing an API call in an Angular/NgRx app to exercise the empty/failure path (e.g. a "no results" alert, a retry gate, a fall-through navigation), return the status the failure effect listens for — typically `404`, sometimes `500` — rather than `200 []`. A `200` dispatches the success Action and short-circuits the path under test (the success effect navigates away or stores the empty list as a successful result). Match the status to the effect: inspect the relevant `createEffect(...)` block, find which HTTP error the `catchError` branch maps to the failure Action, and stub that status.

**`storageState` in Playwright:** `test.use({ storageState: undefined })` means "use default" (inherits global setup state). For truly unauthenticated tests, use `test.use({ storageState: { cookies: [], origins: [] } })`.

**Establish baseline before attributing failures (Non-Negotiable):** When running E2E tests to validate a change, first run the same test on the **default branch** (or the unmodified code) to confirm it passes without your changes. If the test already fails on the default branch, it is a pre-existing failure — do not waste time debugging it as if your changes caused it. Report it as pre-existing and move on.

**Test integrity (Non-Negotiable):** Never weaken, simplify, or remove test cases to work around failures. If a test fails, fix the underlying issue (environment, selectors, timing) — don't dilute the test.

**Clean baseline against stateful infra (Non-Negotiable):** When debugging a test against a stateful database (a restored dump, a shared dev DB, anything not freshly provisioned), establish **one clean baseline first**, then change exactly one thing per run. Never interleave fixture re-runs, password/credential resets, or data re-seeding with test runs while diagnosing — re-running a fixture that fires model signals can mutate *other* rows and manufacture failures that look like product bugs. If a fixture must be idempotent to be safe to re-run, make it idempotent before re-running it. One disciplined pass, observe, then diagnose — re-seeding mid-investigation invalidates every observation that follows.

## Pixel-Stable Visual Snapshots

When using visual snapshot plugins (`pytest-playwright-visual`, `assert_snapshot`), snapshot tests are only reproducible when every source of visual drift is pinned. Eliminate in this order before regenerating baselines:

- **Dynamic data in seeded fixtures.** Freeze timestamps, pin any `now()` values. Signal handlers that run on model creation add fresh timestamps — update them after the signal fires.
- **Animations and caret blink.** Playwright's `animations="disabled"` only handles CSS animations it knows about. Add a session-scoped `page.add_init_script` that injects `*{animation-duration:0s!important;transition-duration:0s!important;caret-color:transparent!important}`. Combine with `reduced_motion: "reduce"` in `browser_context_args`.
- **Font antialiasing across architectures.** Apple Silicon Docker (arm64) and x86_64 CI render fonts at different heights. Force `platform: linux/amd64` on the e2e compose service so locally-regenerated baselines match CI.

**Regenerate baselines inside the same Docker image CI uses.** Never regenerate on the host with `uv run pytest --update-snapshots` — macOS Chromium renders differently. Use `t3 <overlay> e2e --update-snapshots` (which runs in the pinned Docker image).

**Recovering a baseline that was never committed.** Playwright fails with `A snapshot doesn't exist at ...`. Pull the `{name}-actual.png` from the failing job's artifacts and commit it as the baseline. Inspect the extracted PNG before committing — confirm it captures the intended deterministic state rather than a transient error page.

## Pre-Push Browser Sanity Gate (Visual QA)

`t3 <overlay> pr create` runs a pre-push browser sanity gate as a side effect of the shipping flow. It loads the page(s) the branch diff touches, captures silent-render regressions (crashes, console errors, raw `app.*` keys, blocking 404s), and records the summary on `Ticket.extra['visual_qa']`. See `t3:ship` § "4c. Visual QA Gate" for the blocking behavior and bypass flags.

This gate is **not a replacement for E2E evidence** — it only catches silent-render regressions before push.

## DoD Local-E2E Gate (UI-visible tickets)

`Ticket.ship()` refuses to ship a **UI-visible** ticket — one whose scope includes a repo in the active overlay's `frontend_repos` — until a **green local-stack E2E artifact** exists. The durable `Ticket.extra['e2e_recipe'].last_run` must be `result == "green"` AND `env == "local"`; a `dev` run records provenance but does NOT satisfy the gate. A dev-after-merge run is deliberately not enough — the whole point is to catch missing scope *before* the merge, not after. A green local run is recorded automatically by `t3 <overlay> e2e run <work-item>` (which resolves an on-disk workspace, so `env` defaults to `local`).

The gate raises `DodLocalE2EError` (a transition refusal, like the dirty-worktree preflight) and the FSM stays put. Escape hatch for a genuinely non-UI or exempt ticket the heuristic mis-flags:

```bash
t3 <overlay> ticket dod-override <ticket-id> --reason "<why this is exempt>"
```

The override is recorded on `Ticket.extra['dod_e2e_override']` (audited; a blank reason is refused) so the bypass is explicit, not silent.

## Private Test Suite

Sometimes a **separate test repo** reduces friction — no conflicts with the QA team's tests, no build pipeline overhead, freedom to use different tooling or test data.

- Set `T3_PRIVATE_TESTS` in `~/.teatree.toml` to the path of your private test repo.
- Structure tests by app and feature: `tests/<app>/<feature-area>/<test-file>`
- Store artifacts (screenshots, recordings) in a git-tracked `artifacts/<TICKET>/` directory.

## Post Testing Evidence on the Ticket

**Use `t3 <overlay> e2e post-evidence` first.** It posts ONE structured comment on the **ticket** (work item / bug) — never on the MR, even when MRs are open. The deployed-environment proof belongs to the issue the work closes and stays attached after the MR merges.

The command refuses bad evidence before any upload or post:

- missing `--before` / `--after` artifact
- identical before == after (same path, or two paths with byte-identical contents)
- `--env` outside `{dev, local}`
- an unknown commit, or a dirty working tree (uncommitted changes ⇒ not reproducible)

Flags (all keyword-only):

| Flag | Required | Auto-detection |
|---|---|---|
| `--ticket` | no | pk / issue number / issue URL; falls back to the resolved worktree's ticket |
| `--env` | yes | none — must be `dev` or `local` |
| `--commit` | no | `git HEAD` of the resolved worktree |
| `--before` | yes | none — path to the before artifact |
| `--after` | yes | none — path to the after artifact |
| `--video` | no | none — optional video artifact |
| `--assertion` | yes | none — the feature claim the evidence proves |

After confirming a feature works, embed screenshot(s) and video(s) **directly in the evidence comment** — not as a separate comment.

### Visual Comparison Format

Evidence screenshots must use a **side-by-side comparison table**:

```markdown
| Before (current UI) | After (this PR) |
|---|---|
| ![before](url) | ![after](url) |
```

With a design mockup available, use 3 columns:

```markdown
| Before (current UI) | Design mockup | After (this PR) |
|---|---|---|
| ![before](url) | ![mockup](url) | ![after](url) |
```

### Rules

- **One screenshot per verified section** — not per checkbox.
- **Cover ALL sections on first post.**
- Screenshots go below the section they verify, not at the bottom.
- Write in first person, casual tone ("Tested on local...", "Works as expected").
- Evidence must read as manual testing — no mentions of automation, E2E, Playwright, or scripts.
- **Idempotent on `(ticket, env)` — one comment per environment.** Each comment carries a hidden marker `<!-- t3-e2e-evidence env=<env> -->` (renders invisibly). Re-running on the same env — for any commit — edits that comment in place and opens it with a terse `old -> new` commit delta; a different env posts a new comment. You never have to hand-dedup, and a moving HEAD never spams the ticket with one comment per commit — the marker does it.
- **Match evidence type to PR type.** UI screenshots for frontend PRs; backend evidence (test output, API diffs) for backend PRs.

### Evidence Source Integrity (Non-Negotiable)

Evidence posted on tickets or MRs MUST come from the **deployed environment** (dev/staging), never from local builds. Violation is grounds for termination — it exposes the team to compliance and trust failures.

The `dev`/`local` gate is now **machine-enforced** by `t3 <overlay> e2e post-evidence`: an `--env` outside `{dev, local}`, a missing/identical before-after pair, or an unknown/dirty commit is refused before any upload or post — so a fake or mislabelled evidence pair never reaches the ticket.

**Prohibited evidence sources:**

- Golden test PDFs from `build/test-results/` or `src/test/resources/`
- `pdftotext` output from locally-rendered documents
- Screenshots of locally-served pages that aren't deployed
- Side-by-side comparisons using git-extracted PDFs from different commits

**Required evidence sources:**

- Browser screenshots of the actual deployed application (dev/staging URL)
- API responses from the deployed environment
- Documents regenerated on the deployed environment after merge + deploy

**Before posting evidence, verify:**

1. The MR is merged and deployed to the target environment
2. Screenshots show a real environment URL in the browser bar (not `localhost`)
3. The document was rendered by the deployed code, not a local build

Golden test PDFs serve ONE purpose: CI regression testing. They prove the XSL transform is internally consistent. They do NOT prove the deployed system works correctly — the data, config, and rendering pipeline in the real environment can differ.

## Debugging E2E Failures

### Browser Console First (Non-Negotiable)

When an E2E test shows missing UI elements (empty form, blank section, component not rendering), **capture browser console errors before investigating component code.** Add `page.on('console', ...)` and `page.on('pageerror', ...)` listeners. Runtime errors like `"Undefined form configuration!"` reveal the root cause in seconds.

### Screenshot Sanity Check (Non-Negotiable)

Before claiming E2E success or posting screenshots as evidence, **visually inspect every screenshot** for environment issues. Reject and fix if any of these are present:

- **Missing translations:** Labels show raw keys instead of human-readable text.
- **Missing static files:** Broken images, unstyled pages, 404s for assets.
- **Console errors:** Check for blocking JS errors.
- **Feature element not visible:** The screenshot must show the specific UI element being tested. Use `element.scrollIntoViewIfNeeded()` before screenshots.

### Store Contamination Check

E2E tests for features that load data via a state management store must verify the data is loaded **from the tested page**, not from prior navigation. Each test must start from a clean state — navigate directly to the page under test. Empty dropdowns/lists are a red flag.

## Test Tracking Files

Each test file can have a sibling `.md` with the same basename — a single source of truth for what has been tested and posted per ticket.

| Ticket | PR | Description | Comment |
|--------|-----|-------------|---------|
| [PROJ-1234](url) | [#5678](url) | Initial: feature X | [Plan + images](url) |

## Re-Read Before Debugging

When an E2E test fails or the environment misbehaves, **re-read this skill** before spending more than 2 minutes on ad-hoc debugging. Skill guidance loaded early in a session gets compressed out of context.
