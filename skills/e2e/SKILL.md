---
name: e2e
description: End-to-end testing with Playwright — writing tests, running them, visual snapshots, test-plan posting, and the pre-push visual QA gate. Use when user says "e2e", "playwright", "write e2e", "run e2e", "visual qa", "screenshot", "post test plan", "post evidence", or is working with Playwright-based tests.
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

Playwright-based end-to-end testing for overlay target applications. Covers writing tests, running them, visual snapshots, test-plan posting, and the pre-push visual QA gate.

## Dependencies

- **t3:test** (required) — general testing patterns and CI interaction.
- **t3:workspace** (required) — worktree and dev server management.

## Setup & Prerequisites

**Full worktree per PR (Non-Negotiable):** Each PR under test MUST have its own full worktree setup (backend + frontend via `t3 <overlay> worktree provision` + `t3 <overlay> worktree start`). Never mix backends from one worktree with frontends from another. Never patch an incomplete worktree by hand — if it's missing repos, env files, or DB, delete it and start over with `t3 <overlay> workspace ticket`.

**This full-worktree rule is `--target local` ONLY — a `--target dev` run provisions NOTHING locally.** A remote/DEV run (`t3 <overlay> e2e [run|external] --target dev`) hits the already-deployed stack, so there is nothing to run on your machine: do NOT `t3 <overlay> worktree provision` or start backend+frontend+DB for it. The full local stack exists to test an **unmerged** change on a self-run stack — irrelevant when the change is already deployed where you're pointing. Over-provisioning a full stack for a remote run just burns ~20 min and reads as a stall; a `--target dev` run needs only the specs repo + Playwright + DEV creds. Provision locally only for `--target local` (an unmerged change with no deployed env to hit).

Always start dev servers via `t3 <overlay> worktree start` before running tests. Never start services manually. Before running E2E tests, verify that **translations are loaded** — the frontend i18n directory is gitignored and only populated at startup. If the frontend was started manually, translations will be missing. Quick check: open any page and confirm labels show human-readable text, not raw keys like `app.feature.xxx.label`.

## Browser tool: chrome-devtools-mcp (default)

Agentic browser work — driving a deployed page (navigate/click/fill/upload) and inspecting it (network / console / DOM / screenshots) — runs through **chrome-devtools-mcp**, teatree's default browser tool. It is Google's `chrome-devtools-mcp` server, driving its own Chrome over the DevTools Protocol. Crucially it needs **no claude.ai account and no browser-extension pairing** — the whole account-switch / extension-popup / "logged in ≠ connected" fragility of the old Claude-in-Chrome extension is gone. Deterministic E2E stays on **Playwright** (below); chrome-devtools-mcp is the agentic nav/interaction + diagnosis lane, never the perf/trace enforcement lane.

**Register it (default on):**

```bash
t3 mcp browser-diagnosis   # prints the `claude mcp add` line; the flag ships ON by default
# turn OFF only on a host that cannot run the server:
# t3 <overlay> config_setting set chrome_devtools_mcp_enabled false
```

The registration is `claude mcp add chrome-devtools -- npx -y chrome-devtools-mcp@latest`, so the tools surface as `mcp__chrome-devtools__*` — `navigate_page`, `click`, `fill` / `fill_form`, `type_text`, `upload_file`, `wait_for`, `take_snapshot`, `take_screenshot`, `list_console_messages`, `list_network_requests`, `evaluate_script`. Browser-visible breakage (a blank render, a failed XHR, a console error, a wrong DOM state) is diagnosed **in the browser** with these before any root-cause claim, not guessed from the server side.

**Optional aid for authoring/debugging Playwright specs, never required (#3271).** The same live DOM/console/network view makes *writing* a Playwright spec (finding the right selector, confirming the expected DOM state) and *debugging* a red one far more tractable than working blind. It is purely a developer-experience aid — teatree's runtime requires **zero** MCP, so its absence gates nothing: `t3 doctor` only ever emits an INFO suggestion for it, never a WARN/FAIL. Prerequisite: a Chrome/Chromium executable on the host (the server launches its own Chrome over the DevTools Protocol).

**Pre-authorize the tools for an unattended run.** So the tool never prompts mid-run, allow the server in `~/.claude/settings.json`:

```jsonc
{
  "permissions": {
    "allow": [
      "mcp__chrome-devtools__*"
    ]
  }
}
```

**Research finding — MCP allow-rules match by server + tool name only, no domain form.** Per the [Claude Code permission rule syntax](https://docs.claude.com/en/docs/claude-code/permissions#mcp), an MCP specifier is `mcp__server`, `mcp__server__*`, or `mcp__server__tool_name` — there is **no** argument/domain form, so you cannot scope `mcp__chrome-devtools__navigate_page` to a domain the way `WebFetch(domain:example.com)` scopes `WebFetch`. The allow-rule is therefore all-or-nothing per tool. Since chrome-devtools-mcp drives its own launched Chrome (not a shared extension session), there is no per-origin browser gate to clear on top of the allow-rule — allowing the tool is sufficient for an unattended run.

## Running E2E Tests

- Run headless with `CI=1`.
- `t3 <overlay> e2e` — run E2E tests locally.
- `t3 ci trigger-e2e` — trigger E2E tests on CI.

**E2E for backend/API changes:** When backend or microservice changes affect data visible in the frontend (e.g., webhook payload fields, API serializer fields, new model fields exposed via API), E2E tests are still required even if there is no frontend PR. The frontend form already has the fields — E2E proves the end-to-end data flow. Do NOT skip E2E just because the change is "backend-only."

## Dual-Env Testing (one spec, DEV or local)

A single spec should run against either the deployed **dev** environment or the **local** stack, selected by one CLI argument. Determinism comes from code, never from a parsed file.

**Target selection.** `t3 <overlay> e2e [run|external|project] --target dev|local`:

```bash
# Run the suite against the deployed dev environment (do NOT export/edit BASE_URL by hand):
t3 <overlay> e2e run <work-item> --target dev

# Run the same spec against the local stack (always discovers the local frontend):
t3 <overlay> e2e run <work-item> --target local
```

- `dev` — keep the pre-set `BASE_URL` (deployed env); no local port scan.
- `local` — always discover the local frontend, even if a stray `BASE_URL` is exported (so `--target local` can never silently hit a deployed env).
- omitted — back-compat: infer `dev` if `BASE_URL` is set, else `local`.

**Set the target with `--target`, never by hand-editing `BASE_URL` (Non-Negotiable).** Do this:

1. Select the environment with the `--target dev|local` flag — that is the ONE knob.
2. Let the runner resolve and export **`T3_E2E_TARGET`** for you; the spec branches on it — `const IS_DEV = process.env.T3_E2E_TARGET === 'dev'`.
3. Never re-derive the target from a `BASE_URL` host regex, and never export or rewrite `BASE_URL` to point at a different env — a stray `BASE_URL` is exactly what `--target local` overrides so a local run can't silently hit deployed code.

Test a deployed/merged change against `dev`; an unmerged change must still pass on `local` (the DoD gate below requires a green `local` run regardless).

**Specs branch selection (`external` runner).** `t3 <overlay> e2e [run|external] --repo <name> --branch <name>` (alias `--ref`) runs the suite from a working branch of the external specs repo instead of the `[e2e_repos.<name>].branch` default. Use it while a specs-migration MR is still open — point at the MR's source branch so the team runs the new specs before they land. Omitted, the configured default ref is used unchanged. The branch must exist on the remote, or the run aborts with a clear message. (`--branch` applies only to a `--repo` clone; a `T3_PRIVATE_TESTS` directory is one you check out yourself, so the flag is rejected there.)

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

**Deployed-branch check before asserting post-fix behaviour (Non-Negotiable).** Shared DEV and staging environments may run a long-lived release branch, not the default branch. A fix merged to `main` only is NOT observable on an environment that tracks a separate release branch. Before asserting "the fix is still broken on DEV" or "the fix works on DEV", verify which branch that environment actually runs and confirm the fix is present on it. The overlay skill's reference docs identify which environments track which branch. If unverified, gate or skip the assertion with a reason — never report "still broken" for what is actually "fix not yet on the deployed branch".

## Writing Tests

**Build the test against the TICKET's acceptance criteria, never against the MR diff (Non-Negotiable).** An E2E test verifies a **ticket** holistically — and a ticket is frequently multi-repo (a backend MR, a frontend MR, a microservice change, translations, external config, all closing one ticket). Gather the whole ticket and **all** its linked MRs across every repo before designing the test, then enumerate the end-to-end user flow the ticket promises and test *that*. Reading the MR diff too closely is a trap: it biases you to assert *what the code does now* instead of *what the ticket requires* — a vacuous test that passes regardless of correctness. The diff is an input to understanding, not the unit of test. The reviewer-side statement of this principle is `/t3:e2e-review` § "Test the ticket, not the MR diff" — apply it as the author, don't restate it.

**Test depth:** Don't just verify "page loads with 200". Once you know the ticket's required flow, read the source code to understand how the feature is built, then test specific behaviors: form fields, filters, CRUD operations, access control, edge cases.

**Tighten value assertions to a VISIBLE field, not a value a default can satisfy (Non-Negotiable).** A value assertion must bind to a field that is actually **rendered and visible** in the UI. The trap: asserting a computed value through a getter/accessor that returns a *default* (`0`, `''`, `null`) when the field is **absent** — the assertion then passes whether the feature worked or the field never rendered at all (a false-pass that survives the very regression the test exists to catch). Assert that the labelled field is **visible first**, then assert its text — so an absent field fails the visibility check instead of silently satisfying the value check via a default.

```ts
const total = page.getByLabel('Default purchase costs');
await expect(total).toBeVisible();            // an absent field fails HERE, not silently
await expect(total).toHaveText('€ 1,250');    // and the value is read from the rendered field
```

Prefer `getByLabel`/`getByRole` (which resolve only a present element) over reading a number off a store/model getter that coerces a missing field to `0`. If the only available probe is a getter that defaults, add the visibility assertion alongside it so the "absent field" and "field shows the default value" cases can never be confused.

**Choose the assertion standard the AC's truth-model actually supports — exact-value golden vs structural invariant.** When a ticket ships a reference/golden artifact (a worked example, an expected schedule, a reference PDF), match the assertion to whether the system can reproduce that reference *exactly* or only *approximately*:

- **Exact-value golden** only when the reference is authoritative AND the code can reproduce it bit-for-bit. Then assert the precise values cell by cell.
- **Structural invariants** when the system legitimately approximates the reference (a calculator that uses a different day-count basis, a renderer that rounds differently). Here a cell-by-cell euro golden can pass *only* by widening tolerance over the very values the fix changes — which re-encodes the bug behind a loosened bar. Assert the structural truths instead (no phantom row, balance is monotone, the step pattern holds, the discriminators between variants differ, the schedule is complete).

Never pin the code-under-test's own unvalidated output as the "expected" baseline — that certifies whatever it currently does, including the defect. And when a reference/golden artifact exists, the coverage standard is the **full** reference (the whole table, every AC), not a one- or two-row spot-check. (This is the e2e expression of the same standard in `t3:review`; choose exact-where-reproducible, structural-where-approximating, and don't grade the code with its own output.)

**Access-control / role-gated E2E (Non-Negotiable):** Before asserting behaviour on any access-controlled or role-gated page, resolve the test account's REAL identity — role and group memberships — from the app's own API (e.g. `/api/me/`) and assert the expected outcome FROM that identity. The exempt/restrict contract is derived from the guard source code (what the guard actually checks), not from a ticket description or relayed narrative about which role a user supposedly has. Precondition assertion before behaviour assertion makes the test non-vacuous: if the role check fails, the test fails at the precondition rather than silently passing on an unexpected identity.

**Resolve E2E credentials from the project's documented credential map by role (Non-Negotiable).** The project's overlay skill carries a credential table keyed by ROLE (not email). Before declaring a missing-credential blocker, look up the account in that table by role — the username is often a code constant, and the password is resolved from the secret store using the documented key. Do NOT grep the secret store by account email and conclude "no credentials found" — the store entry is keyed by the documented role path, not the login email.

**Credentials enter the spec via env with a throw-if-unset guard — never an inline literal (Non-Negotiable).** Once resolved, a credential is injected into the run as an environment variable and read by the spec through a guard that **throws if the variable is unset**, so a missing secret fails loud at startup instead of the spec silently running with `undefined` (which logs in as nobody, then mis-attributes the resulting failure to the feature). Never paste a literal login email or password into a spec — a literal credential in spec source is a leak and a maintenance trap, and an email literal often trips brand/secret scanners.

```ts
function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is required for this E2E run but was unset`);
  return v;
}
const password = requireEnv('E2E_BROKERAGE_PASSWORD');   // throws if the secret wasn't injected
```

The username may be a published code constant; the password (and any tenant/host that is a secret) always comes from env via this guard. `t3 <overlay> e2e` injects the documented secret into the run env; a spec that hard-codes the value bypasses that path and the secret store entirely.

**Component placement:** Before writing E2E tests for a UI component, check the **routing module** to find which page/route renders it. Components may only appear at specific wizard steps or behind navigation — not on the page you'd naively navigate to. Grep for the component selector in templates to find its host, then check the routing module for the URL path.

**Seed prerequisite data through the API, drive only the behaviour-under-test through the browser (Non-Negotiable).** Do NOT click through a multi-step UI wizard to set up the entity a test needs (a loan request, an order, an account) — create it programmatically via API/fixture helpers, then open the browser **only** for the one page/interaction the test actually asserts. A heavy SPA can load its resource cache from 100+ endpoints sequentially (minutes per navigation on local Docker), so browser-driven setup turns a fast test into a multi-minute hang and a flake source — multiple sessions have burned hours clicking a wizard the API could have seeded in seconds. Setting up prerequisites this way is sanctioned fixture setup (§ "Dual-Env Testing" → sub-pattern 1), not faking: the data path stays real and the asserted UI behaviour is still produced by the real code path. Practical helpers for the API-seed approach:

- **Make fixtures self-contained — never depend on the raw dump's incidental state** (a flag, a price catalog, an activation that a snapshot may or may not carry); seed exactly what the test needs so a re-run is reproducible.
- **Wrap fixture creation that uses `select_for_update`-style row locks in a transaction** so the seed commits atomically.
- **Assign seeded entities to the authenticated test user's own scope** (broker/org/role) — an entity owned by a different user is invisible to the API the spec calls and yields a misleading 404/empty result.
- Bump the per-`describe` `timeout` for any flow that still must navigate to a slow detail page (the resource-cache cold-load is real even when setup is API-seeded).

**Mocking — stub with the error status the failure-path expects, not `200`.** When stubbing an API call in an Angular/NgRx app to exercise the empty/failure path (e.g. a "no results" alert, a retry gate, a fall-through navigation), return the status the failure effect listens for — typically `404`, sometimes `500` — rather than `200 []`. A `200` dispatches the success Action and short-circuits the path under test (the success effect navigates away or stores the empty list as a successful result). Match the status to the effect: inspect the relevant `createEffect(...)` block, find which HTTP error the `catchError` branch maps to the failure Action, and stub that status.

**Race a condition promise, never a fixed `waitForTimeout` (Non-Negotiable).** When an action triggers an async write the assertion depends on — a `PATCH`, a `POST /calculate`, a recompute — do **not** insert a fixed `page.waitForTimeout(...)` and hope it settled. Set up the response promise on the *real* request **before** the action that triggers it, then await it after, so the wait is keyed on the actual round-trip completing with a `200`, not on a guessed duration:

```ts
const saved = page.waitForResponse(
  (r) => /\/api\/.*\/calculate/.test(r.url()) && r.request().method() === 'POST' && r.ok(),
);
await page.getByRole('button', { name: 'Recalculate' }).click();
await saved;                                  // resolves exactly when the real call returns 200
await expect(page.getByLabel('Total cost')).toHaveText('€ 12,300');
```

Set the promise up first (the await-after-click order), match the **real** endpoint + method + `r.ok()`, and prefer it over a `waitForLoadState('networkidle')` when one specific call is what the assertion depends on — `networkidle` waits on *all* traffic and still races a late XHR. A fixed sleep is slow on a fast machine and flaky on a slow one; the response promise is correct on both.

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

**E2E evidence is part of "done", not an optional extra — record it BEFORE ship (Non-Negotiable).** A UI-visible ticket is not done until it has a green local E2E artifact, and the deployed-env proof follows once the change is live. The canonical sequence:

```bash
# 1. BEFORE ship — green local E2E run is mandatory; this records the gating artifact:
t3 <overlay> e2e run <work-item> --target local

# 2. Ship only after step 1 is green (Ticket.ship() refuses otherwise):
t3 <overlay> pr create

# 3. After merge + deploy — run E2E against the dev environment and post the test plan
#    (the deployed-env run is the completing half of "done", not a nice-to-have):
t3 <overlay> e2e run <work-item> --target dev
t3 <overlay> e2e post-test-plan --manifest "$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json"
```

Do step 1 — never push a UI-visible ticket with no recorded E2E artifact. Then do step 3 — a deployed-env (`dev`) E2E run plus a posted test plan is what closes the loop on a UI-visible ticket; merging without it leaves "done" half-proven.

The posted test plan the gate expects is the **comprehensive ticket test plan**, not just proof a green local run happened: one workflow per affected UI surface with a red-boxed screenshot, plus an explicit `Actual: ✅ <result>` for every backend/API claim (a backend-only workflow renders as its steps + `Actual` line, not an empty Dev|Local table). A green local E2E artifact satisfies the ship gate; the comprehensive plan is what makes the posted evidence trustworthy.

`Ticket.ship()` refuses to ship a **UI-visible** ticket — one whose scope includes a repo in the active overlay's `frontend_repos` — until a **green local-stack E2E artifact** exists. The durable `Ticket.extra['e2e_recipe'].last_run` must be `result == "green"` AND `env == "local"`; a `dev` run records provenance but does NOT satisfy the gate. A dev-after-merge run is deliberately not enough — the whole point is to catch missing scope *before* the merge, not after. A green local run is recorded automatically by `t3 <overlay> e2e run <work-item>` (which resolves an on-disk workspace, so `env` defaults to `local`).

The gate raises `DodLocalE2EError` (a transition refusal, like the dirty-worktree preflight) and the FSM stays put. Escape hatch for a genuinely non-UI or exempt ticket the heuristic mis-flags:

```bash
t3 <overlay> ticket dod-override <ticket-id> --reason "<why this is exempt>"
```

The override is recorded on `Ticket.extra['dod_e2e_override']` (audited; a blank reason is refused) so the bypass is explicit, not silent.

## Private Test Suite

Sometimes a **separate test repo** reduces friction — no conflicts with the QA team's tests, no build pipeline overhead, freedom to use different tooling or test data.

- Set the `T3_PRIVATE_TESTS` environment variable to the path of your private test repo.
- Structure tests by app and feature: `tests/<app>/<feature-area>/<test-file>`
- Artifacts land under the out-of-repo root here too: `$T3_E2E_ARTIFACTS_DIR/<TICKET>/<env>/`. Copying them into the test repo and tracking them in git is a choice only a **private** test repo may make (there, the artifacts are the deliverable). It is never permitted in a product/customer repo — see the rule below.

### Artifacts Are Never Committed to a Product Repo (Non-Negotiable)

An artifact is a **recording of a run** — screenshots, videos, traces. It is reproducible from the spec plus a provisioned stack, and once `post-test-plan` uploads it, the ticket note holds the durable copy. Committing artifacts to a product/customer repo puts binaries in a source tree, bloats every clone, and makes reviewers page through a video diff. The artifacts root lives **outside every working tree** (§ "Artifact directory layout"), so a correctly-pathed run never touches the repo; keep `artifacts/` gitignored in product repos anyway as a backstop against a stray hard-coded path. The evidence lives on the ticket, not in the branch.

Three kinds of file get confused for one another — classify before deciding where each belongs:

| Kind | Example | Home |
|---|---|---|
| **Artifact** — *records* a run | `step1.png`, `run.webm` | Never committed to a product repo; uploaded to the ticket note by `post-test-plan`. |
| **Fixture** — *produces* state | flag/message seed, API seed | The spec's own `beforeAll` / fixture, in the specs tree. Never a loose script under `artifacts/`. |
| **Manifest** — *authored intent* | `manifest.json` (workflow names, human `steps`, claim→capture mapping) | Hand-written, but it is an artifact too: it lives beside the captures it maps, at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json` — outside every working tree, never committed to a product repo. Once posted, the note's hidden state blob holds the durable copy. |

The **run provenance** is DB-home, not in the tree — never re-derive it from files. `Ticket.extra['e2e_recipe']` records the run's sha and env; the rubric score lives on the `Rubric` model and the posted-note URL on `E2eMandatoryRun.posted_url`.

A **private** test repo that legitimately tracks its manifest (the artifacts are the deliverable there) must commit only the *authored* half — never the per-run commit SHAs / `missing_on_dev`, which churn the file on every push (#3092). `t3 <overlay> e2e tracked-manifest --manifest <path>` prints that authored half (the top-level `dev`/`local` provenance blocks removed) so two runs produce a byte-identical tracked file. Keep the full manifest out-of-repo for `post-test-plan`; commit the stripped output.

A loose `seed-*.py` under `artifacts/` is a smell: fixture logic escaped the spec. Fold it into the spec's fixture and delete the script, or the next run silently depends on a human having executed it by hand.

## Test-Plan Authoring

A test plan is for a human testing in a browser. Write it so the reviewer can skim and verify fast: terse steps, exact URLs and accounts, one expected result per step, minimal prose. Cut narration, repeated caveats, and analysis essays — a plan is not a report.

**Modality — classify each AC before writing a single step.** The right modality depends on what the AC actually tests:

- **Route-guard / RBAC / redirect / backend boundary** (e.g. "advisor is blocked from the admin portal"): the verification IS a URL to navigate + an expected redirect or HTTP status. Write a clickable URL and the expected response code or redirect destination. A screenshot adds nothing here — the URL and the curl transcript ARE the evidence. Do not over-screenshot.
- **UI feature** (e.g. a dropdown appearing, a computed field, a generated document): the verification is **browser click steps** — open this page, click this, expect this visible result. Screenshots are the per-step compare-against reference. **Never substitute API checks for UI steps.** When the FE branch is not yet on the dev environment, write the steps against a local stack that has the FE branch, or mark the AC "⏳ blocked until deployed" — do not replace clicks with curl.
- **Genuinely backend-only AC** (a webhook, a background job, a data migration): API/curl evidence is correct and sufficient. Keep it as a copy-pasteable code block, not a terminal screenshot.

**Never put a terminal screenshot in a test plan.** A screenshot must show a browser UI. An API response belongs as a text code block (or a browser URL the tester opens), not an image of a terminal window.

**Conciseness** — a plan that exceeds what a reviewer needs to verify fast is too long. Aim for the minimum: exact URL, exact account, one expected outcome per step. A 30k-character test plan buries the actual steps in narration; keep it short enough to skim.

**Field-context evidence for generated documents.** When an AC requires verifying a term in a generated PDF, export, or rendered document, assert the term appears in its expected structured field or labelled row — not anywhere in the full text. Free-text fields (borrower name, address, test-fixture label) often contain the same token and produce a false "verified." The verification step must name the field being checked: "the Security row shows type X", not "the PDF contains X". Beware test-fixture names that embed the feature keyword — a borrower named "E2E FeatureName" defeats a naive full-text search for "FeatureName".

The deterministic primitive for this rule is `teatree.core.evidence.doc_evidence` (#2296) — route doc-export evidence checks through it rather than hand-rolling a substring scan. Parse the document into a `StructuredDoc` (named `fields` + labelled table `rows`) and verify with `check_doc_evidence(doc, FieldClaim(term=…, field_label=…))` or `ColumnClaim(term=…, column_label=…)`. The probe binds the assertion to the field/column the AC constrains and **fails loud** (`DocEvidenceError`) when that anchor is absent — never falling back to an incidental free-text match. A bare page-wide substring is rejected outright (`reject_page_wide_substring`); it is not evidence. It is an available primitive, not a globally-enforced gate yet — wiring a specific call site (e.g. an overlay's doc-export verification step) into it is the follow-up.

## Posting a Test Plan

There is ONE canonical command for posting a test plan — do not hand-craft a GitLab/GitHub note, do not paste screenshots into a comment by hand, and do not explore for an alternative path:

```bash
t3 <overlay> e2e post-test-plan --manifest "$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json"
```

The manifest is the single input. Build it once, then run that command — re-running is always safe (each run merges its env over the prior note state, § "Post Testing Evidence on the Ticket").

**A test plan posted to a colleague-facing ticket is evidence of real testing — never an admission of not testing (Non-Negotiable).** A note posted on the user's behalf to a colleague/product ticket MUST reflect testing actually performed on the real (deployed) environment with real evidence. **NEVER** post a public note containing "unable to test", "blocked", "DEV verification pending", "could not verify", "not automatable", or any equivalent — to the user's colleagues that reads as incompetence and is humiliating. The gate before any on-behalf post is one question: *did I actually verify this on the real env, with real evidence?* If no, do not post.

- Hit a blocker (missing credential, unpinned object id, wrong tenant, a field not exposed)? **Fix the blocker so you CAN test** — resolve the credential (§ "Resolve E2E credentials…"), pin a real reproducible object id, run against the correct tenant, replicate the DEV object to local (§ "Replicating a DEV object to local") — then post a real plan.
- If, after genuine effort, you truly cannot test, post **nothing public**. Surface the precise named blocker to the user privately and let them unblock it. A "couldn't test" public note is strictly worse than no note. This is the same terminal as the rubric's `BLOCKED(<named-gate>)` (§ "Verify–Review Loop to Threshold") — surface the gate, post nothing caveated.

**A user directive to "post it in whatever state it's in" does NOT override this — do X, never Y (Non-Negotiable).** Under deadline pressure the recurring drift the metered lane caught is: verification is blocked (a missing credential, an expired GPG key), the user says "post the test plan in whatever state it's in / we're behind / just get it on the ticket", and the agent OBEYS by posting a caveated "DEV verification pending — unable to test" note to a colleague-facing ticket. That is wrong. The colleague-protection rule is not the user's to waive on a colleague's behalf: the user authorizing the post does not make a "couldn't test" note any less humiliating to the partner team who reads it, and the user cannot see how it lands on the colleague. So the directive authorizes the *intent* ("get #N's test plan handled"), never the *blocked-note artifact*. Your single next action under that directive is one of two safe moves — **fix the blocker so you CAN post a real plan** (re-inject the credential via `pass`/the secret store, retry the run), or **surface the named blocker privately** (an `AskUserQuestion` stating the exact gate — the missing credential — and asking how to unblock, posting NOTHING public). It is **never** a `Bash` post of a note carrying "unable to test" / "blocked" / "DEV verification pending" / "not yet tested" to the colleague ticket, no matter how explicit the user's "post it anyway" was.

```bash
# Blocked on a credential, user says "post the test plan in whatever state it's in".
# do X — fix the blocker, OR surface it privately (AskUserQuestion: which credential, how to unblock):
pass insert -m e2e/e2etest-password          # re-inject the credential, then run+post a REAL plan
# … or AskUserQuestion("E2E_BROKERAGE_PASSWORD is missing / GPG key unavailable — how do you want to unblock #8568?")
# never Y — do NOT obey the directive by posting a caveated blocked note to the colleague ticket:
# t3 <overlay> e2e post-test-plan --ticket 8568 --note "DEV verification pending — unable to test …"   # FORBIDDEN
```

**The manifest carries the per-workflow steps — not just screenshots.** Each entry in `workflows[]` is one workflow, and each workflow carries its own `steps` array: the numbered "how to test / where to click" list a human follows to reproduce it manually. The command renders that list above the workflow's `Dev | Local` evidence table, so the test plan is steps-plus-evidence, not bare images. Include a `steps` list on every workflow (see § "Manifest shape" for the full schema):

```jsonc
{
  "ticket": "<TICKET>",
  "workflows": [
    {
      "workflow": "<plain-language workflow name>",
      "steps": ["Open the app", "Click the Login button", "Expect the dashboard"],
      "dev":   {"video": null, "images": []},
      "local": {"video": "local/run.webm",
                "images": ["local/step1.png"]}
    }
  ]
}
```

**Set the environment with `--target`, never by hand-editing `BASE_URL`.** The env a manifest fills is decided by the `--target dev|local` flag on the `t3 <overlay> e2e run` invocation that produced the captures (§ "Dual-Env Testing" → "Target selection"), and recorded in the manifest's `dev`/`local` blocks. Do not export or rewrite `BASE_URL` to redirect the run — `--target` is the one knob; the runner exports `T3_E2E_TARGET` for you.

**Local-vs-CI note.** The evidence-capture path (the videos and red-boxed screenshots that go into the manifest) runs **locally behind the `--target` flag** — there is no CI parity for capturing/posting the test-plan note. CI runs the suite for pass/fail, but it does **not** assemble or post a test plan; you produce the artifacts on your machine via `t3 <overlay> e2e run <work-item> --target dev|local`, then post them with the canonical command above. So the test plan is a local-run deliverable, gated by the flag — not something CI emits on your behalf.

## Post Testing Evidence on the Ticket

**Use `t3 <overlay> e2e post-test-plan --manifest <json>`.** It maintains ONE structured test-plan note on the **ticket** (work item / bug) — never on the MR, even when MRs are open. The deployed-environment proof belongs to the issue the work closes and stays attached after the MR merges.

The note renders as a **test plan**: a header (the ticket title, multi-repo MR links, the per-env commit provenance, and a dev-gap reconciliation line) followed by one block per workflow — the workflow heading, an optional **`How to test:` numbered step list** (the click-through a human follows to reproduce it manually), then the **side-by-side `Dev | Local` comparison table** — each workflow's video row first, then one row per screenshot pair (`—` where a side has no capture, e.g. dev not yet deployed).

In the header, each `repo \`sha\`` in the `Dev deployed:` / `Local tested:` lines is a **clickable commit link** — the full project path is derived by matching the repo short-name against the MR URLs in the note, so a repo with no matching MR renders a bare code-span (never a broken link). A `Dev ± Local:`line then states, per repo present on both sides, whether dev and local are on the **same** commit (`= same commit`) or **differ** (`≠ dev \`<sha>\` vs local \`<sha>\``).

Artifacts always upload to the **ticket's own project** (resolved from the issue URL the note posts on), never to a manifest MR's repo or the overlay's CI project — a note only renders the uploads its own project claims, so the upload target follows the note.

The note is keyed on a hidden ticket marker `<!-- t3-e2e-evidence ticket=<n> -->` and carries a hidden machine-readable state blob `<!-- t3-e2e-data {…} -->` that is the source of truth. Each run **merges** the env(s) its manifest carries over the prior state: a `local`-only manifest fills/refreshes the Local column and freezes Dev; after merge + deploy a `dev`-only manifest fills the Dev column (and clears the "⚠️ Not yet on dev" line) while freezing Local. You never hand-dedup; re-running is always safe.

The command refuses bad evidence before any upload or post: invalid manifest JSON, a referenced artifact that does not exist, or a file whose extension is the wrong media kind (an image listed under a video slot, etc.).

Flags (all keyword-only):

| Flag | Required | Notes |
|---|---|---|
| `--manifest` | yes | path to (or inline string of) the test-plan manifest JSON |
| `--ticket` | no | pk / issue number / issue URL; falls back to the resolved worktree's ticket |
| `--title` | no | overrides the `## Test Plan — <title>` heading |
| `--mrs` | no | MR/PR URL(s) (repeat or comma-separate) — supplements the manifest's `mrs` |
| `--template` | no | body template: `capture-matrix` (default) / `browser-click-first` / `link-api`; overrides the manifest's. Pick it from the AC's modality (below). |

**Pick the `--template` from the AC's modality (§ "Modality — classify each AC").** The flag is how the modality classification becomes the actual note shape:

- `capture-matrix` (default) — the side-by-side `Dev | Local` red-boxed screenshot table. Use for **UI-feature** ACs where screenshots are the per-step compare-against reference. This template runs the red-box pixel gate (below), so every image must carry the highlight.
- `link-api` — links + code blocks per workflow, **no table, no images**. Use for **route-guard / RBAC / redirect / backend-boundary** ACs (a URL to navigate + an expected redirect/HTTP status, or a `curl` transcript) — the evidence is the URL and the request/response, not a screenshot. Because it carries no images, it skips the red-box gate entirely, so it is also the correct shape when the proof is a status code or golden-data check with no single visible element to box.
- `browser-click-first` — numbered manual steps with inline screenshots, for a click-through a human reproduces.

### Manifest shape

```json
{
  "ticket": "8521",
  "mrs": ["https://gitlab.com/group/client/-/merge_requests/6331",
          "https://gitlab.com/group/product/-/merge_requests/7585"],
  "dev":   {"commits": {"client": "<deployed-sha>", "product": "<deployed-sha>"},
            "missing_on_dev": ["client!6331 (unmerged)", "product!7585 (draft)"]},
  "local": {"commits": {"client": "<branch-sha>", "product": "<branch-sha>"}},
  "workflows": [
    {"workflow": "<test name>",
     "steps": ["Open the app", "Click the Login button", "Expect the dashboard"],
     "dev":   {"video": null, "images": []},
     "local": {"video": "local/run.webm",
               "images": ["local/step1.png", "local/step2.png"]}}
  ]
}
```

- One object per workflow; each carries its `dev` and `local` captures. A side's captures may be empty (e.g. dev before deploy) → that column shows `—`.
- `steps` (optional, workflow-level — shared across dev/local) is the written test plan: the numbered "how to test / where to click" list rendered above that workflow's table. Omit it and the block is omitted. It persists across re-runs — a later steps-less run keeps the recorded steps.
- `images` and the optional `video` are file paths **relative to the manifest's own directory** — the manifest sits at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json`, so a capture in the per-env directory (see the layout rule below) is referenced as `<env>/<file>`. Just paste what Playwright captured there.
- `dev.missing_on_dev` lists the MRs whose commits are not yet deployed — the note renders them as an expected gap so a dev column of `—` reads as normal, not a failure.

### Artifact directory layout (Non-Negotiable)

E2E artifacts live in a **dedicated directory per environment**, **outside every repo working tree**. The **runner** exports the resolved root — the per-ticket workspace's `.t3-cache/artifacts` — as `T3_E2E_ARTIFACTS_DIR` (core owns the path, so the no-artifacts-in-a-repo rule is structural, not each overlay re-deriving it; #3331). Override it with `--artifacts-dir` (refused when it resolves inside a repo working tree). Honour the variable rather than hard-coding a path. A capture for `env ∈ {dev, local}` lives at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/<env>/<file>`. Writing captures to a **worktree-root** `artifacts/` puts binaries inside a product repo — the exact mistake the rule above forbids. Capture every screenshot and recording for a given env under that env's directory — never mix a dev and a local capture in one folder, and never dump artifacts at the ticket root. Examples:

```
$T3_E2E_ARTIFACTS_DIR/8521/local/run.webm
$T3_E2E_ARTIFACTS_DIR/8521/local/step1.png
$T3_E2E_ARTIFACTS_DIR/8521/dev/run.webm
$T3_E2E_ARTIFACTS_DIR/8521/dev/step1.png
```

This makes wrap-up and manifest assembly trivial — a side's captures are exactly the files under `$T3_E2E_ARTIFACTS_DIR/<TICKET>/<env>/`, so building the manifest's `dev`/`local` blocks is a directory listing, and a re-run for the other env never collides with the first. `t3 <overlay> e2e post-test-plan` resolves relative artifact paths against the **manifest's own directory**, so keep the manifest beside its captures at `$T3_E2E_ARTIFACTS_DIR/<TICKET>/manifest.json` and reference them as `<env>/<file>`.

### Rules

- **Paste whatever Playwright captured** — all screenshots for each test, plus its one video (omit the video when there is none) — from that env's `$T3_E2E_ARTIFACTS_DIR/<TICKET>/<env>/` directory.
- **Always include a `steps` test plan per workflow.** Give each workflow a numbered "how to test / where to click" list so a human can reproduce it manually — this is a standard part of every teatree test-plan note, not optional. Write it in plain manual-testing language.
- **One note per ticket, all environments.** The Dev|Local table accumulates: local now, dev added after deploy, same note.
- Write the workflow names and title in plain language; evidence must read as manual testing — no mentions of automation, E2E, Playwright, or scripts.
- **Match evidence type to PR type.** UI screenshots for frontend PRs; backend evidence (test output, API diffs) for backend PRs.

### Evidence Source Integrity (Non-Negotiable)

Evidence posted on tickets or MRs MUST come from the **deployed environment** (dev/staging) or a teatree-managed local stack, never from stale local builds. Violation is grounds for termination — it exposes the team to compliance and trust failures.

`t3 <overlay> e2e post-test-plan` **machine-enforces** that every referenced artifact exists and is the right media kind before any upload or post, and uploads each via the relative `/uploads/<secret>/<file>` reference GitLab claims on save (so the media actually renders — not a broken image or a dead video player).

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

**Condition-based settle before capture (Non-Negotiable).** Always wait for the target element to be visible AND the network to be idle before capturing a screenshot — never use a fixed `waitForTimeout` as the settle step. A screenshot captured before the page has settled either shows a blank page or the previous route's content (a transition frame); a blank-page or transition-frame capture is NOT evidence — fail the step rather than posting it.

```ts
await expect(page.locator('[data-test=expected-element]')).toBeVisible();
await page.waitForLoadState('networkidle');
await page.screenshot({ path: `${process.env.T3_E2E_ARTIFACTS_DIR}/<TICKET>/<env>/step1.png` });
```

**Red-box the asserted element in DEV captures (evidence, not decoration).** A screenshot posted as evidence must make the asserted element obvious, not leave a reviewer hunting a full page for it. Before the capture, draw a saturated-red box around the element under assertion (a bright `outline`/`border` injected via `element.evaluate(...)`, or a Playwright highlight) so the captured PNG carries an unmissable marker on exactly the field/control the test verifies. This is the same red-box marker the post-test-plan evidence gate looks for in DEV captures — a deployed-env screenshot whose asserted element isn't visibly boxed reads as a generic page shot, not proof the specific behaviour rendered.

```ts
const el = page.getByLabel('Default purchase costs');
await expect(el).toBeVisible();
await el.evaluate((n) => { n.style.outline = '4px solid #ff0000'; n.style.outlineOffset = '2px'; });
await el.scrollIntoViewIfNeeded();
await page.screenshot({ path: `${process.env.T3_E2E_ARTIFACTS_DIR}/<TICKET>/dev/step1.png` });
```

Capture the red-boxed shot only after the settle (visible + network idle) above — a red box around a not-yet-rendered element is no more evidence than a blank page.

**The red-box gate is a real pixel-count check — self-check before posting, not after a rejected post.** `t3 <overlay> e2e post-test-plan` rejects a `capture-matrix` image whose saturated-red pixel count is below the gate's minimum (`teatree.core.evidence.test_plan_validation._saturated_red_pixel_count`; a real highlighted crop clears it comfortably while a hidden-state / absence shot or a pre-red-box capture reads ~0 and is rejected). Two adjacent gates trip silently if you assemble carelessly:

- **A hidden-state or "element absent" screenshot has no red box to draw, so it scores ~0 and is rejected.** If an AC's evidence is the *absence* of an element, that AC's modality is `link-api` (a status/redirect), not a boxed screenshot — don't try to post an unboxed shot.
- **Byte-identical-image dedup (md5):** no two byte-identical images may appear in one manifest, and a `dev/` and `local/` capture of the same state can come out byte-identical — include each distinct state once, or ensure the two sides' bytes actually differ.
- **A re-run replaces a whole side, not one workflow:** supplying a `dev` (or `local`) block replaces that ENTIRE side — commits, `missing_on_dev`, and ALL its workflows. To update one workflow on a side, re-send every workflow for that side or the others vanish (workflows are keyed by name; `steps` persist across runs).

On the slower DEV stack the injected red box can render as ~0 px for a visible-element assertion even when the element is on screen (a timing/scroll/crop race the local stack doesn't hit). Mitigate by `scrollIntoViewIfNeeded()`, waiting for the outline to actually apply, and cropping so the outline is inside the captured region — before the screenshot, not after.

Before claiming E2E success or posting screenshots as evidence, **visually inspect every screenshot** for environment issues. Reject and fix if any of these are present:

- **Missing translations:** Labels show raw keys instead of human-readable text.
- **Missing static files:** Broken images, unstyled pages, 404s for assets.
- **Console errors:** Check for blocking JS errors.
- **Feature element not visible:** The screenshot must show the specific UI element being tested. Use `element.scrollIntoViewIfNeeded()` before screenshots.
- **Blank or transition-frame page:** Indicates the settle wait was insufficient — fail the step, do not post.

### Video Sanity Check (Non-Negotiable)

The same evidence bar the "Screenshot Sanity Check" enforces on stills applies to the **recorded video** — and it is the one a recent failure slipped through: a test-plan video with ~40s of blank pre-roll (out of 69.7s) and an unclear final frame was posted to a customer ticket and neither the author nor the e2e-review gate caught it, because the post path machine-enforced screenshot quality but had **zero** check on the video.

The captured recording must:

- **Start the interaction promptly** — no significant blank/static pre-roll. Begin the recording right before the interaction starts; do **not** record dead setup time (waiting on a login, a cold-loading SPA, an idle page) and leave it at the head of the clip. A recording that opens on a frozen or black screen for many seconds reads as a broken capture, not evidence.
- **End on a clearly-framed final state** showing the asserted outcome — hold the final frame on the result the test verifies (the rendered field, the confirmation screen, the computed value), settled and unambiguous, so the last thing a reviewer sees is the proof. Do not let the recording cut mid-transition or end on a navigation/blank frame.

The deterministic check is `teatree.core.evidence.video_evidence` (mirroring `teatree.core.evidence.test_plan_validation` for stills) — it shells ffprobe/ffmpeg to measure the leading blank/static run and refuses an over-budget pre-roll. Run it directly on any recording before posting:

```bash
# Verify one recording (exits non-zero on excessive blank/static pre-roll):
uv run python scripts/analyze_video.py "$T3_E2E_ARTIFACTS_DIR/<TICKET>/local/run.webm" --verify
```

This check is **machine-enforced by `post-test-plan`**: `t3 <overlay> e2e post-test-plan` runs `check_video_evidence` over every manifest `video` alongside the image gates and **refuses the post** (naming the dead-lead seconds) when a recording opens with excessive pre-roll — so a dead-lead video can never reach the ticket. When ffmpeg is absent the check skips cleanly (it never blocks a post merely because the host lacks ffmpeg); `--skip-validation` is the user-authorised bypass (the agent never sets it itself). The final-frame clarity is the author's discipline — capture so the recording holds the asserted end-state, then `--verify` the head.

### Store Contamination Check

E2E tests for features that load data via a state management store must verify the data is loaded **from the tested page**, not from prior navigation. Each test must start from a clean state — navigate directly to the page under test. Empty dropdowns/lists are a red flag.

## Test Tracking Files

Each test file can have a sibling `.md` with the same basename — a single source of truth for what has been tested and posted per ticket.

| Ticket | PR | Description | Comment |
|--------|-----|-------------|---------|
| [PROJ-1234](url) | [#5678](url) | Initial: feature X | [Plan + images](url) |

## Verify–Review Loop to Threshold

A single E2E pass is not self-driving: it can go green vacuously, miss an acceptance criterion, or be brittle in a way a static read of the green line never shows. The fix is to **iterate** `/t3:e2e` and `/t3:e2e-review` until the spec earns a rubric-scored confidence threshold — with a hard stop so it can never spin forever. This is the in-skill, iterative expression of the orchestrator's lifecycle chain: `/t3:e2e` is the `test`/e2e phase, `/t3:e2e-review` is the `e2e_reviewing` phase, and `/next` is the edge between them.

> `/next` = the orchestrator advancing the FSM to the next phase and spawning that phase's sub-agent. The e2e ↔ e2e-review chaining IS this `/next` edge fired repeatedly: `e2e --/next--> e2e_reviewing`, and on HOLD, `e2e_reviewing --/next--> e2e` again.

### The loop as FSM edges (max 5 iterations per ticket)

1. **`test` / e2e phase — `/t3:e2e`.** Run the spec — against **DEV** if the feature is deployed there, else a **local stack** restored from the DEV dump (§ "Dual-Env Testing" and § "Replicating a DEV object to local"). On failure, **bug-hunt**: browser console first (§ "Browser Console First"), then screenshot sanity (§ "Screenshot Sanity Check"), driving the page with chrome-devtools-mcp where it helps. **Codify every confirmed finding into a committed Playwright spec** — a browser observation that isn't captured as a durable assertion is lost; the bug-hunt's output is *new committed test code*, not a note. If a real **product bug** surfaces, fix it. Opportunistically **consolidate** duplicated/outside specs into the canonical suite via the `/t3:e2e-review` § "Adopting an outside Playwright suite" conversion method. Then `/next`.
2. **→ `e2e_reviewing` phase — `/t3:e2e-review`.** Score the spec (and its run) with the **E2E Confidence Rubric** (`/t3:e2e-review` § "E2E Confidence Rubric"): all three hard gates, then the six weighted criteria, returning `{score, threshold, verdict, findings}`.
3. **VERIFIED** — `score ≥ threshold` AND all hard gates pass. `/next` advances toward `ship`: commit the specs, open/merge the e2e PR, and **post the clean test plan** (§ "Post Testing Evidence on the Ticket"), recording the rubric score alongside the run. **If the ticket also changed product code**, the normal `review` phase (code review, maker ≠ checker) sits between `e2e_reviewing` and `ship`; for a **pure test-adding ticket**, `e2e_reviewing → ship` directly. An optional `review-request` follows. Exit the loop.
4. **BLOCKED** — a **hard external gate** blocks (no broker account and local can't substitute; a broken login with no available fix; a result observable nowhere programmatically — the rubric's `BLOCKED(<named-gate>)`). Terminal: surface the **named gate** to the user, post **nothing caveated**, exit. Do not loop.
5. **HOLD** — below threshold (and fixable). The FSM loops **back to the `test`/e2e phase** (`e2e_reviewing --/next--> e2e`): a fresh `/t3:e2e` that applies the top rubric `findings` — fix spec brittleness, add the missing-AC assertions, fix the bug, de-flake — then re-scores. Re-loop.

### Terminal states (never loop forever)

- **VERIFIED** (`score ≥ threshold`, all hard gates pass) — the clean test plan is posted, the rubric score recorded.
- **BLOCKED(named gate)** — a genuinely-unreachable feature (manual-only/no-API, infra-gated). The named gate is surfaced to the user; no caveated note is posted.
- **MAX_ITERATIONS** (5 verify↔review rounds without VERIFIED) — stop and report the **best score reached** and the **precise remaining gap** (the specific rubric criteria/findings still short of threshold). Do not silently keep looping.

Never post a caveated note as a substitute for reaching the threshold: a note that says "verified, except…" is not a VERIFIED — it is a HOLD or a BLOCKED wearing a green coat. The whole point of the threshold is that 100% confidence is unreachable for some tickets, so the loop terminates honestly (BLOCKED or MAX_ITERATIONS) rather than pretending.

### Configuration

The pass bar is the DB-home **`e2e_confidence_threshold`** setting — an integer 0–100, **default 90**, **per-overlay overridable**. Set it in the `ConfigSetting` store; a stricter client overlay can raise it, a fast dogfood overlay can lower it. It is the single knob both the rubric (`/t3:e2e-review`) and this loop read, so "the threshold" means one value, resolved through the DB-home chain: overlay-scope DB row → global DB row → the dataclass default (no env layer for this setting).

```bash
t3 <overlay> config_setting set e2e_confidence_threshold 90   # rubric score a spec must reach to be VERIFIED (0-100)
t3 <overlay> config_setting set e2e_confidence_threshold 95 --overlay client-x   # stricter bar for a client overlay
```

## Re-Read Before Debugging

When an E2E test fails or the environment misbehaves, **re-read this skill** before spending more than 2 minutes on ad-hoc debugging. Skill guidance loaded early in a session gets compressed out of context.
