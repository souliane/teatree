---
name: e2e-review
description: Reviewer-side quality gate for Playwright end-to-end specs. Load when reviewing a new or changed E2E test, deciding whether a spec is ready to land, or adopting an outside Playwright suite. Judges specs against Playwright's published best practices — user-visible behaviour over implementation, resilient role/label/test-id locators, web-first auto-retrying assertions instead of hard waits, per-test isolation, page-object structure, and runnable evidence — and tells the implementer what to fix before approval.
compatibility: macOS/Linux, Playwright, Node.js, git.
requires:
  - review
  - e2e
companions:
  - receiving-code-review
metadata:
  version: 0.0.1
  subagent_safe: true
---

# Reviewing E2E Specs

A focused reviewer's lens for Playwright end-to-end tests. It does **not** replace `/t3:review` — the maker's lifecycle, ticket-context retrieval, and posting mechanics all live there. This skill adds the one thing a generic code review misses: whether an E2E spec is a **trustworthy** end-to-end test or a brittle one that will flake, lie, or rot.

A spec earns approval when a green run actually proves the user-facing behaviour the ticket asked for, and a red run points at a real regression rather than a timing artefact. Everything below is a way to decide whether that is true.

## The one question behind every check

> If this test goes green, has a real user's path through the app actually been proven? If it goes red, will the failure name a real defect?

Most E2E review findings reduce to that test. A spec that asserts on a CSS class proves nothing about the user. A spec padded with `waitForTimeout` goes green by luck and red by load. A spec that depends on yesterday's leftover data is green until it isn't. Read each spec asking the question, then group what you find under the principles below.

## Test the ticket, not the MR diff

An E2E test verifies a **ticket's** acceptance criteria, not a single MR's diff. A ticket is frequently multi-repo — the same change often lands as a backend MR, a frontend MR, a microservice change, translations, and external config, all closing one ticket. Before designing or judging the test, gather the **whole** ticket and **all** its linked MRs across every repo it touches; the diff in front of you is one input, not the whole story.

Reading the MR diff too closely is a known trap: it biases the test toward asserting *what the code does now* instead of *what the ticket requires* — a vacuous test that passes regardless of whether the feature is correct. Build the test against the **spec / acceptance criteria** and the business-domain behaviour, never against the current behaviour of the MR under review.

- **Start from the ticket, not the diff.** Read the ticket and its acceptance criteria first; enumerate the end-to-end user flow the ticket promises, then check the test covers *that* flow.
- **Gather every linked MR across every repo** before judging coverage — a ticket whose test only exercises the FE diff misses the BE/microservice/translation/config halves of the same flow.
- **A test written from the diff is the failure mode to catch.** If an assertion mirrors the MR's current output rather than the ticket's stated requirement, it certifies the implementation instead of verifying it — flag it the same way you'd flag a golden master captured from the code-under-test's own output (§ 6).
- **The MR diff is an input to understanding, not the unit of test.** Test the holistic user flow the ticket promises, across every repo it touches.

This is why the rubric scores **AC coverage** (§ "E2E Confidence Rubric"): "every acceptance criterion has a backing assertion" only means something if the criteria came from the ticket, not from the diff.

## 1. Behaviour over implementation

Playwright's first best practice is to test what the end user sees, not how the app is built internally ([best-practices](https://playwright.dev/docs/best-practices)). The reviewer's job is to catch specs that have quietly bound themselves to implementation.

- **Assert on user-visible outcomes** — rendered text, a visible element, the URL, an enabled/disabled control — not on internal state, a redux/ngrx store, a class name, or a DOM-structure detail that a refactor would silently change.
- **The scenario name should read like a requirement.** A reviewer (or the next maintainer) should understand what broke from the test title alone, without reading the body. `submits the application and shows the confirmation screen` is reviewable; `test flow 3` or `clicks button and checks div` is not. A title that describes mechanics instead of behaviour is a finding.
- **Reject "test the framework" assertions.** A test that only checks Angular/React rendered *something* — or re-asserts a library's own guarantees — adds maintenance cost without protecting a user path.
- **Reject a value assertion that a default can satisfy.** A `toHaveText`/`toEqual` against a value read through a getter that returns a default (`0`, `''`, `null`) when the field is **absent** passes whether the feature worked or the field never rendered — a false-pass that survives the regression the test exists to catch. The reviewable form asserts the labelled field is **visible first**, then asserts its text (the `/t3:e2e` § "Writing Tests" tighten-to-a-visible-field rule), so an absent field fails the visibility check rather than silently satisfying the value check. A number read off a store/model getter with no accompanying visibility assertion is a finding.
- **Don't test third parties you don't control.** Playwright explicitly warns against asserting on external sites or third-party servers; mock those responses via the network API instead ([best-practices §3](https://playwright.dev/docs/best-practices)). A spec that navigates to a real payment provider or a live external URL is a flake source, not coverage.

## 2. Locator strategy — resilient by accessibility, stable by intent

Playwright ranks locators by how closely they mirror how a user (and assistive technology) perceives the page ([locators](https://playwright.dev/docs/locators), [best-practices §5](https://playwright.dev/docs/best-practices)). Review every locator against that ordering and reject the brittle tail of it.

- **Prefer, in order:** `getByRole` (most resilient, accessibility-aligned) → `getByLabel` / `getByPlaceholder` for form controls → `getByText` for non-interactive content → `getByAltText` / `getByTitle` → `getByTestId` as an explicit, stable handle when no user-facing one fits.
- **CSS and XPath selectors are a finding by default.** Playwright calls them out as tied to DOM structure and prone to breaking on refactors. Accept them only when there is genuinely no role/label/text/test-id handle and the spec says why; otherwise ask for a `data-testid` to be added to the component instead.
- **A `data-testid` is a deliberate, stable handle the component owns, not a workaround.** A test-id is fine — Playwright calls it the most resilient handle — but it must be a deliberate attribute on the component, not a scrape of a generated id, an auto-numbered suffix, or a class that doubles as a style hook.
- **Strictness, not `.nth()` roulette.** A locator should resolve to exactly one element. Reaching for `.first()`, `.last()`, or `.nth(2)` to dodge a strict-mode violation usually means the locator is too broad — ask for one that uniquely identifies the target, or a `.filter()` chain that scopes to the right region. An index-based locator silently re-targets when the list reorders.
- **No hardcoded credentials, URLs, tenant ids, or secrets** baked into a locator or a `goto`. Base URL and login come from config/fixtures/env, so the same spec runs against local, CI, and review environments unchanged. A literal `https://app.staging…` or an inline password is a blocker. An inline **login email** literal is equally a blocker — it leaks the account and trips brand/secret scanners.
- **Credentials must be read from env through a throw-if-unset guard.** A secret entering the spec as `process.env.X` without a guard that throws when `X` is unset is a finding: the spec then runs with `undefined`, logs in as nobody, and mis-attributes the failure to the feature. The reviewable form is a `requireEnv('…')`-style helper that fails loud at startup (the `/t3:e2e` § "Writing Tests" pattern). A bare `process.env.PASSWORD ?? ''` fallback is a hidden silent-skip.

## 3. Waiting on conditions, never on the clock

This is the single highest-value thing an E2E reviewer enforces. Playwright already waits: before every action it runs actionability checks — visible, stable, enabled, receives-events, editable — and web-first assertions retry until the condition holds or the timeout expires ([auto-waiting](https://playwright.dev/docs/actionability), [assertions](https://playwright.dev/docs/test-assertions)). A spec that adds its own fixed sleeps is fighting the framework.

- **`page.waitForTimeout(...)` / any fixed `sleep` is a blocker.** It is slow on fast machines and flaky on slow ones. The fix is to wait on the *condition that the sleep was approximating* — the element appearing, the text changing, the URL updating, the network call settling.
- **When the assertion depends on an async write, the prescribed fix is `page.waitForResponse` on the real call.** A sleep inserted after a save/recompute trigger should become a response promise set up **before** the triggering action and awaited after it, matched on the real endpoint + method + `r.ok()` (the `/t3:e2e` § "Writing Tests" race-a-condition-promise pattern). Flag a `waitForLoadState('networkidle')` standing in for one specific call too — it races a late XHR; the keyed `waitForResponse` is the resilient form.
- **Web-first assertions over manual polling.** `await expect(locator).toBeVisible()` / `toHaveText(...)` / `toHaveURL(...)` auto-retry. Flag the non-retrying anti-pattern of a snapshot `isVisible()` read fed into a plain `expect(...)` — it captures a single moment and re-introduces the flakiness the framework removes.
- **For a multi-step or eventual condition, use the retrying block forms** — `expect.poll(...)` for a value that converges, or `expect(...).toPass()` to retry a small block of assertions — rather than a sleep-then-check. Suggest these when you see a hand-rolled retry loop.
- **Tune the timeout, don't pad with sleeps.** If a step legitimately takes long (a heavy report, a slow backend), the right move is a per-assertion `{ timeout: … }`, not a `waitForTimeout` before it.

## 4. Isolation and deterministic data

Playwright expects each test to run independently, with its own storage, cookies, and data, so one failure never cascades and order never matters ([best-practices §2](https://playwright.dev/docs/best-practices)). Review the spec as if it will run alone, in parallel, and in a random order — because it will.

- **No cross-test state.** A spec must not depend on another spec having run first (a logged-in session, a record created by an earlier test, a shared module-level variable). If test B only passes after test A, that is an ordering dependency to flag.
- **Set up and tear down your own data.** Each spec creates the entities it needs (via fixture, API seed, or `beforeEach`) and cleans them up (`afterEach` / fixture teardown), so a re-run on a dirty database is green, not "already exists". A spec that assumes a specific seeded record exists by name is brittle unless that seed is owned by the suite.
- **Fixtures over copy-pasted setup.** Repeated login/navigation boilerplate across specs should be a shared fixture or POM method. Duplication here is both a maintenance and an isolation risk — one place to get setup right.
- **Parallel-safe by construction.** Unique names (timestamp/uuid suffixes) instead of fixed literals, no reliance on a global singleton, no two specs racing on the same row. If two parallel workers would collide, it's a finding.

## 5. Structure — one behaviour per test, page objects for reuse

- **One behaviour per test.** A spec that drives five unrelated flows in one `test(...)` is hard to name, hard to diagnose (the first failure masks the rest), and slow to re-run. Ask to split it; each test should map to one scenario the title can name. (Where you genuinely want partial results within one scenario, Playwright's soft assertions collect multiple failures instead of stopping at the first — suggest those rather than cramming unrelated behaviours together.)
- **Page Object Model for anything reused.** Playwright documents POM as the way to centralise selectors and encapsulate page interactions, so tests read declaratively and a UI change updates one file instead of twenty ([pom](https://playwright.dev/docs/pom)). When a selector or a multi-step interaction appears in more than one spec, the reviewer's ask is "move this into a page object." A test body that is a long ladder of raw `page.getByRole(...).click()` calls is a candidate for a POM method named after the intent.
- **Locators live in the page object, assertions live in the test.** A healthy split: the POM exposes intent-level methods and locators; the spec orchestrates them and owns the `expect` assertions. A POM that hides assertions inside its methods makes failures hard to attribute — flag it.
- **Readable, intent-named helpers.** Page-object methods should be named for what the user does (`submitApplication()`), not for the mechanics (`clickButton3()`). The same readability bar as scenario titles.

## 6. Evidence — it actually runs

A static read of a spec cannot tell you it passes; a green local read of a spec that was never executed is not review. Before approving:

- **Confirm the spec runs and goes green** in the suite, not just in isolation — and that it goes green for the *right* reason (the assertions actually fire, not skipped or short-circuited).
- **Manual-QA parity is the coverage floor.** If two competent QA reviewers would manually exercise more states, branches, or workflows than the Playwright spec asserts, coverage is incomplete. Green must mean "no known bug remains in this covered workflow", not merely "the page opens".
- **Anti-vacuity on a regression spec.** If the E2E was written to lock a bug fix, the same anti-vacuity proof from `/t3:review` applies: revert the production fix and confirm the spec goes **red**. A spec that stays green with the fix reverted guards nothing. The full rule is the source of truth in `../review/SKILL.md` — apply it, don't restate it.
- **A golden-master assertion must not re-encode the bug.** When a spec pins exact values against a reference (a schedule, a reference PDF, a worked example), check two things: the expected values were **not** captured from the code-under-test's own output (that certifies the defect), and the tolerance was **not** widened to make an approximating calculator pass over the very values the fix changes. If the system only approximates the reference (different day-count basis, different rounding), the reviewable form asserts **structural invariants** (no phantom row, monotone balance, the step pattern, variant discriminators, schedule completeness) — not a cell-by-cell euro golden behind a loosened bar. Where a reference artifact exists, coverage is the **whole** reference, not a one-or-two-row spot-check. (The `/t3:e2e` § "Writing Tests" exact-vs-structural rule.)
- **Watch for a quietly-skipped or no-op spec** — a `test.skip`, a `test.fixme`, a `test.only` left in (which silently drops the rest of the file from the run), an empty body, or an assertion that can never fail. Any of these is a blocker; a green CI line over a skipped test is a false signal. Removing a skip/fixme is valid only when the same user workflow is covered by another running assertion; otherwise it deletes evidence instead of improving it.
- **Visual / media evidence follows the project's E2E evidence rules** (real screenshots/video from an actual run, never a text stub). Those mechanics — capture, where to post, refuse-on-zero-media — live in `/t3:e2e`; this skill only checks that the evidence exists and matches the asserted behaviour.
- **Decompose any video-backed plan and view its frames (Non-Negotiable).** For a plan whose evidence includes a recording, do not approve on the existence of a video file alone — **decompose the video into frames** (`uv run python scripts/analyze_video.py <recording>`) and view them. Confirm the deterministic check passes (no excessive blank/static pre-roll — `analyze_video.py <recording> --verify`, the same `teatree.core.video_evidence` gate `post-test-plan` enforces) **AND** that the **final frame unambiguously shows the asserted end-state** (the rendered field, the confirmation, the computed value — not a mid-transition, navigation, or blank frame). A recording that opens on a long blank pre-roll or ends on an unclear frame is not legible evidence — you cannot VERIFY it. This is the reviewer half of `/t3:e2e` § "Video Sanity Check"; it caught a 69.7s recording with ~40s of blank head and an unclear final frame that the existence-only check let through.

## Reviewer verdict

Translate findings into the team's severity language, scaled to impact:

- **Blocker** — anything that makes a green run untrustworthy or a red run misleading: a fixed `waitForTimeout`/sleep, a non-retrying `isVisible()` assertion, hardcoded creds/URL, a cross-test ordering dependency, a `test.only`/skipped-but-counted spec, or a vacuous regression spec that stays green when the fix is reverted.
- **Should-fix** — a brittle CSS/XPath locator with an available user-facing alternative, an `.nth()` dodge around strict mode, duplicated setup that belongs in a fixture/POM, a multi-behaviour mega-test, or a title that describes mechanics instead of the requirement.
- **Nit** — naming polish, a marginally better locator, a small readability split. Non-blocking; the maker curates.

Post findings through the normal review path in `/t3:review` (draft-by-default, one terse anchored note on a colleague's MR, the on-behalf/autonomy gates) — this skill does not introduce its own posting mechanics.

## E2E Confidence Rubric

The severity verdict above is the human-review language; the **rubric** is the machine-scoreable form of the same judgement, so the verify↔review loop in `/t3:e2e` § "Verify–Review Loop to Threshold" has a number to gate on. The reviewer scores a spec (or a spec + its run) 0–100 and returns a structured verdict. The score exists for one purpose: to decide whether the spec has earned **VERIFIED** or must loop back for another verify pass — never to dress up a brittle spec as "90% good enough".

### Three hard gates (any one failing caps the score to HOLD)

A hard gate is not a weighted criterion — it is a precondition. If any fails, the spec **cannot** be VERIFIED no matter how high the weighted criteria score; the verdict is **HOLD** (loop back) or, when the gate is an external impossibility, **BLOCKED** (see below). Check all three before scoring the weighted criteria.

- **HARD GATE H1 — Non-vacuous green.** The spec actually **ran** — not `test.skip`/`test.fixme`/`test.only`-dropped, not flag-gated off, not an empty body. Every **precondition** assertion passed for the *right reason*: the role-gated/RBAC precheck (e.g. the resolved `/api/me` role and group memberships from § "Writing Tests" in `/t3:e2e`) asserted the test account's real identity before the behaviour assertion, so a green can't come from an unexpected identity. Real assertions fired (web-first `expect(...)`, not a swallowed `isVisible()` read). For a **regression** spec, the anti-vacuity proof holds: revert the production fix → the spec goes **red** (the full rule is `../review/SKILL.md`; apply it, don't restate it). A spec that goes green with the fix reverted, or whose precondition never fired, fails H1.
- **HARD GATE H2 — Evidence integrity.** The green came from the **deployed environment** (dev/staging) **or** a teatree-managed local stack — never a stale local build, golden test PDFs (`build/test-results/`, `src/test/resources/`), `pdftotext` of a locally-rendered document, or a `localhost` screenshot. This is the `/t3:e2e` § "Evidence Source Integrity" rule in gate form: if the evidence backing the green violates that rule, H2 fails regardless of how clean the spec reads.
- **HARD GATE H3 — Evidence legibility.** For any **video-backed** plan, the reviewer **decomposes the recording** (`scripts/analyze_video.py`) and views the frames, confirming BOTH: the deterministic check passes (**no excessive blank/static pre-roll** — `analyze_video.py <recording> --verify`, the `teatree.core.video_evidence` gate `post-test-plan` machine-enforces) AND the **final frame unambiguously shows the asserted end-state** (not a mid-transition, navigation, or blank frame). A recording that opens on a long blank pre-roll or ends on an unclear frame is illegible evidence — the reviewer **cannot VERIFY** it, so H3 fails. This is the gate form of `/t3:e2e` § "Video Sanity Check"; it exists because a 69.7s recording with ~40s of blank head and an unclear final frame slipped past an existence-only check.

### Weighted criteria (sum = 100)

Scored only once all hard gates pass. Each is the rubric form of one of the six principles above:

| Criterion | Weight | What full marks looks like |
|---|---:|---|
| **Behaviour over implementation** | 25 | Every assertion maps to a ticket acceptance criterion and checks a user-visible outcome (rendered text, URL, enabled/disabled control) — not a CSS class, store state, or DOM-structure detail. |
| **AC coverage / completeness** | 20 | **Every** acceptance criterion in the ticket has at least one backing assertion, and the coverage matches what two competent QA reviewers would manually check. A green run exercises the whole requirement, not a convenient subset or page-open smoke test. A missing AC is the most common reason a VERIFIED score is wrong. |
| **Locator resilience** | 15 | `getByRole`/`getByLabel`/`getByText`/`getByTestId`; no brittle CSS/XPath where a user-facing handle exists; strict single-match (no `.first()`/`.nth()` dodging a strict-mode violation). |
| **Wait discipline** | 15 | Condition-based waits + web-first auto-retrying assertions only; **zero** fixed `waitForTimeout`/`sleep`; `expect.poll`/`toPass` for eventual conditions. |
| **Isolation & deterministic data** | 15 | Per-test storage/data; own setup + teardown; unique (timestamp/uuid) names; parallel-safe — no cross-test ordering dependency, no race on a shared row. |
| **Reproducibility** | 10 | Stable green across re-runs (not flaky); a green on one run reproduces on the next, in parallel, in random order. |

### Threshold and verdict

The reviewer returns a structured result:

```
{score: <0-100>, threshold: <configured, default 90>, verdict: VERIFIED | HOLD | BLOCKED, findings: [...]}
```

- **VERIFIED** — `score ≥ threshold` **AND all hard gates pass**. The spec has earned the clean test plan; the loop exits.
- **HOLD** — below threshold, or a hard gate failed for a reason the maker can fix (a missing-AC assertion, a brittle locator, a fixed sleep, a vacuous precondition). The `findings` are the punch-list the next verify pass works through; the loop continues.
- **BLOCKED** — a hard gate fails for a reason **no spec edit can fix** (see below).

The threshold is configurable — `[teatree] e2e_confidence_threshold`, default **90**, per-overlay overridable (see `/t3:e2e` § "Verify–Review Loop to Threshold" → Configuration). A stricter overlay raises it; the rubric and the loop read the same knob.

### Recording Your Verdict

A verdict you only narrated is inert — the review loop reads the **recorded** verdict, never your prose, so the one action that advances or terminates the loop is the `review record` CLI call. Do not stop at writing findings: run it.

The canonical command (generic placeholders — substitute your real overlay, PR id, slug, head SHA, and a findings file):

```bash
# Reached a verdict? RECORD it — bind to the EXACT 40-char reviewed head SHA.
HEAD_SHA="$(git rev-parse HEAD)"   # the full hex commit you reviewed; never a short SHA
```

**Clean spec → `merge_safe` (terminates the loop).** When the spec clears all hard gates and `score ≥ threshold`, record a `merge_safe` verdict. This is what drives the `ReviewLoop` to **PASSED** and lets the sweep merge; it does NOT re-open another author round:

```bash
t3 <overlay> review record <pr_id> <slug> \
  --reviewed-sha "$HEAD_SHA" \
  --verdict merge_safe \
  --reviewer-identity "<your-reviewer-id>" \
  --gh-verify-result green
```

**Real defect → `hold` with the punch-list (feeds back, is NOT an approval).** When a hard gate failed or the score is below threshold, record a `hold` and pass the findings as JSON via `--findings-json`. A HOLD re-arms a fresh author leg carrying your punch-list for the next round. The contrast that keeps a HOLD honest:

- The findings written to a JSON file and passed via `--findings-json <path>` ARE the punch-list the author leg works through — that file is the whole point of a HOLD.
- A HOLD is the opposite of an approval, so an approving call (`gh pr approve`, `gh pr review --approve`, `glab mr approve`) is wrong here — it merges an unverified spec. The only action a HOLD takes is the `review record --verdict hold` below.
- A `--verdict merge_safe` recorded while any hard gate failed or the score is below threshold is a faked pass that skips the fix — the verdict must be `hold` until the punch-list is cleared and the spec re-scored.

```bash
cat > /tmp/findings.json <<'JSON'
[
  {"severity": "blocker", "summary": "fixed waitForTimeout(3000) instead of a condition wait", "file": "specs/foo.spec.ts", "line": 42},
  {"severity": "should-fix", "summary": "brittle getByText locator where a getByRole handle exists", "file": "specs/foo.spec.ts", "line": 51}
]
JSON

t3 <overlay> review record <pr_id> <slug> \
  --reviewed-sha "$HEAD_SHA" \
  --verdict hold \
  --reviewer-identity "<your-reviewer-id>" \
  --findings-json /tmp/findings.json
```

`--verdict` accepts exactly `merge_safe` or `hold`. A `BLOCKED(<named-gate>)` is not a recordable CLI verdict — it terminates the loop and surfaces the named gate to the user (next subsection); record neither `merge_safe` nor `hold` for a genuine BLOCKED.

### BLOCKED — when 100% confidence is genuinely unreachable

Some features cannot reach a high score by construction, and forcing one would loop forever:

- A result observable **only via a browser dialog** (a native `alert`/`confirm`, an OS file picker, a print preview) with **no API, no DOM node, and no spec assertion** that can capture it.
- A feature whose data exists **only on a DEV-only catalog** the local DSLR dump legitimately lacks (the `/t3:e2e` § "Documented limitation — some features are DEV-only on local" case) **and** which is not deployed where it could be asserted.
- An **infra-gated** feature: no broker/test account exists and a local stack cannot substitute; a broken login with no available fix.

For these, the terminal verdict is **`BLOCKED(<named-gate>)`** — name the specific gate (`BLOCKED(no-api-for-browser-dialog)`, `BLOCKED(dev-only-catalog-not-on-local)`, `BLOCKED(no-broker-account)`) and record the manual evidence that *was* gathered (the human-observed dialog, the DEV screenshot) as a noted limitation. A BLOCKED is **not** a forced low score that re-loops: it terminates the loop and surfaces the named gate to the user. Never convert a genuine BLOCKED into a caveated VERIFIED, and never let it spin the loop to MAX_ITERATIONS.

## Adopting an outside Playwright suite

When the change isn't a single spec but an externally-authored suite being migrated into the team's tree, review it as a **conversion**, not just a pass/fail gate. The same six principles are the conformance target; the difference is the suite arrived with someone else's conventions baked in.

1. **Inventory before editing.** List the specs, the locator styles in use, the waiting patterns (grep for `waitForTimeout`/`sleep`), and any shared setup. Name what diverges from the principles above so the scope of conversion is explicit.
2. **Re-home structure.** Map flat `tests/` files onto the project's layout, extract repeated selectors/interactions into page objects, and replace copy-pasted setup with the suite's shared fixtures.
3. **Convert the brittle tail mechanically.** Fixed waits → condition-based waits and web-first assertions; CSS/XPath → role/label/test-id (adding `data-testid` to components where needed); hardcoded URLs/creds → config/fixtures.
4. **Prove parity.** The converted suite must run green in the project's runner and each migrated spec must still pass its anti-vacuity check — a conversion that silently weakened an assertion is worse than the original. Land it only once it both conforms and proves the same behaviour it did before.

Keep conversion changes reviewable: prefer one spec (or one cohesive group) per commit so the diff shows the before/after of each pattern, rather than a single opaque "migrated everything" drop.
