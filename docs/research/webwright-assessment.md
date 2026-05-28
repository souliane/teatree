# Webwright assessment for teatree's E2E mechanism

A personal note on whether Webwright (microsoft.github.io/Webwright/) would
help with the friction I keep running into in the `t3 <overlay> e2e` path.

## 1. What Webwright is

Webwright is a small harness (around 1k lines) that lets a language model
drive a disposable Playwright browser from a terminal, capture the resulting
actions as a Playwright-flavoured script, and persist that script as a
parameterisable CLI tool. The browser session is throwaway; the generated
code is what sticks. It is not a test framework, not a fixture system, not
an orchestrator. It sits one layer above Playwright and one layer below
whatever runs the produced scripts.

## 2. Where the current E2E mechanism hurts

- **Visual-regression brittleness.** A canary snapshot under
  `__snapshots__/tests/<feature-area>/np/<landing-state>.png` diffed
  ~16k px on a recent run with no functional change. The config at
  `playwright.config.ts:21-27` sets `maxDiffPixelRatio: 0.01`, which is
  strict given font-rendering noise between local macOS Chromium and the
  Linux runner. Re-baselining is a manual loop.
- **Bug reproducers needing custom backend fixtures.** A recent regression
  spec needs an unclamped-draft precondition that can only be arranged
  inside the Django ORM. A sibling fixture script
  (`fixtures/<feature>-loan-request.py`) shows the shape: a Python script
  piped into `docker compose exec -T web python` to seed catalog rows
  before the browser portion runs. Every such test pulls in
  `django.setup()` and the full factory tree.
- **API-only flows.** Some catalog tickets have frontend pieces, but
  related catalog tickets land before any UI exists. The current shape
  forces a Playwright spec anyway, which then spends most of its time in
  `request.newContext()` and almost none in the browser.
- **Triple-hop SSO on a customer DEV environment.** `helpers/login.ts:70-102`
  walks Angular's `.sso-sign-button` to a Keycloak `#social-<idp>-dev`
  button to a third-party auth server, with a `networkidle` wait at the
  tail. Each hop is a separate failure mode and a separate timing knob.

## 3. What Webwright would change

- *Visual regression:* no change. Webwright records actions, not pixel
  baselines, and would still hand off to the same Playwright comparator.
- *Bug reproducers:* no change. The arranging step is server-side Python;
  a browser harness cannot reach it.
- *API-only flows:* small win. Webwright's emphasis on scripts-as-CLI-tools
  matches the pattern of "I want a reusable command that hits these
  endpoints"; though for pure API work, a Playwright `APIRequestContext`
  helper plus `t3 <overlay> e2e external` already covers it.
- *Triple-hop SSO:* possibly worse. An LLM-driven recorder would re-derive
  the selectors each run, which is exactly the non-determinism the explicit
  `#social-<idp>-dev` selector in `helpers/login.ts` was put in to
  remove. The current code is hand-pinned for a reason.

## 4. What Webwright would NOT change

The pieces in `src/teatree/core/management/commands/e2e.py` —
`_resolve_target` for dual-env switching
(`e2e.py:388-402`), `_build_e2e_env` for overlay env extras
(`e2e.py:99-150`), `_resolve_target_env` for compose-project routing
(`e2e.py:333-368`) — are workspace and lifecycle orchestration. So is the
customer-scoping gate that `OverlayBase.get_e2e_preflight` injects, and the
`pass`-stored credential layer in `helpers/login.ts:30-46`. Webwright does
none of this. Adopting it would leave every line of that file untouched.

## 5. Recommendation

Not now. The hard problems in the current setup are server-side fixture
arrangement, customer-scoped env composition, and SSO chain timing — none
of which a browser-side recording harness addresses. Webwright is
well-shaped for "make me a reusable script to do a thing in a browser",
but the friction in `t3 <overlay> e2e` is upstream of the browser. A small
follow-up worth testing: borrow Webwright's "scripts-as-parameterisable-
CLI-tools" idea inside the existing `helpers/` layer for one-shot
exploratory flows, without replacing the runner.
