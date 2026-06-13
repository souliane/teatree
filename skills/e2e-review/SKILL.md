---
name: e2e-review
description: Reviewer-side quality gate for frontend Playwright E2E specs — business-readable scenario names, stable selector contracts, condition-not-clock waits, one-behaviour-per-test, fixture/cleanup discipline, no hardcoded creds/URLs, POM patterns — plus the procedure for converting an externally-authored suite into a project's conventions. Use when reviewing a new or changed E2E spec, deciding whether an E2E test is ready to land, or migrating an outside Playwright suite into the team suite. Use before approving any new or converted E2E spec.
compatibility: macOS/Linux, Playwright, Node.js, git.
requires:
  - review
  - e2e
eval_exempt: reviewer-side checklist that layers on the eval-covered t3:review and t3:e2e skills; it delegates the recurring graded behaviour to those two and carries only an E2E-specific pass/fail rubric of its own, with no standalone multi-step trajectory to grade
metadata:
  version: 0.0.1
  subagent_safe: true
---

# E2E Review

A reviewer-side quality gate for frontend Playwright E2E specs, plus the procedure for landing an
externally-authored suite. This skill is the **reviewer's** companion to two siblings — it does not
restate them:

- **`t3:e2e`** is the **author's** skill: how to write, run, snapshot, and post evidence for a spec.
- **`t3:review`** is the **general** review skill: self-review, giving review, receiving feedback.

`e2e-review` adds only the E2E-specific reviewer layer those two do not carry: a pass/fail checklist
with mechanizable greps, a verdict rule, and a conversion procedure for importing an outside suite.
When a check restates an authoring rule, cite the rule rather than re-deriving it.

**Core principle:** a spec passes when a reader understands the business scenario from its name and
steps alone, every selector is a stable contract, and every wait is a condition — not a clock.

## When this fires

Load `e2e-review` when:

- reviewing a new or changed `*.spec.ts` (or `*.spec.js`) under the project's E2E tree;
- deciding whether an E2E test meets the bar to land;
- migrating an outside Playwright suite into the project's conventions (see § Conversion).

For writing the spec, switch to `t3:e2e`. For non-E2E review, `t3:review` is the general gate.

## Read the project's authoring rules first

Most repositories keep their E2E authoring rules in a canonical doc the author already followed —
commonly an `AGENTS.md`, `CONTRIBUTING.md`, or `README.md` under the E2E directory (selector
strategy, locale rules, web-first assertions, POM principles, fixture imports, test-data and cleanup
conventions, secrets policy). **Read that doc, then review against it.** A finding should cite the
relevant section of the project's own rules rather than restating them — that keeps the review
anchored to the contract the team agreed to, and avoids the review drifting from the docs.

This skill is the generic reviewer layer that sits on top of whatever the project's authoring doc
says. Where the two agree, the project doc wins and the check below is just a mechanizable shortcut
to flag candidates.

## The five things that distinguish a passing spec

1. **API-seeded preconditions; UI only for the thing under test.** Setup creates state via a named
   factory in `beforeEach`; the test body does not re-walk the creation UI. A failure then points
   at the behaviour under test, not at flaky setup.
2. **Every selector is in a Page Object; the spec is pure orchestration.** No raw `[data-test=...]`
   or CSS in the spec body. The spec reads as prose: `summaryPage.openOffer()`, not a selector soup.
3. **Web-first, auto-retrying assertions only.** No hard waits, no `if (await x.isVisible())`, no
   `try/catch` around assertions, no ElementHandle API.
4. **One behaviour per test, narrated with `test.step()`** (Given / When / Then / And). The scenario
   is legible from the step names without reading the bodies.
5. **Data and expected values from typed fixtures**, not inline literals; tenant/variant differences
   live in data factories; dynamic identifiers (e.g. unique emails) are generated; cleanup is wired.

## Review checklist (each item: what to check + mechanizable grep)

Run greps from the E2E root. `FILE` = the spec under review. Each grep is a **flag**, then read in
context — most checks have legitimate exceptions, noted. A grep hit is never an automatic fail; it is
a line to read.

| # | Check | Command (flag, then judge) |
|---|---|---|
| 1 | No hard waits | `grep -n 'waitForTimeout' "$FILE"` — any hit fails for new code |
| 2 | No ElementHandle API | `grep -nE 'page\.\$\$?\(\|\$eval\(\|waitForSelector' "$FILE"` — use the Locator API |
| 3 | No conditional assertions | `grep -nE 'if \(await \|\.isVisible\(\)\|\.textContent\(\)' "$FILE"` — `.textContent()` inside `expect.poll(...)` is correct; flag bare reads |
| 4 | No `try/catch` around assertions | manual: a `try/catch` swallowing an assertion error hides a real failure — let it fail naturally |
| 5 | No raw selectors in the spec body | `grep -nE "locator\('?\[data-test\|::ng-deep" "$FILE"` — chaining on a POM-exposed locator is fine; raw `[data-test]` literals are not |
| 6 | No text/role-name locating | `grep -nE 'getByText\(\|getByLabel\(\|getByRole\([^)]*name' "$FILE"` — these break when the UI locale changes; allowed only for locale-invariant strings (codes, emails) with a comment, or when asserting user-visible copy |
| 7 | Page Object base class | `grep -n '\bextends\b' "$FILE"` (in POM files) — a Page Object should extend the project's shared base, not a deprecated one; locators are `readonly` |
| 8 | Fixture-module import discipline | `grep -nE "from '@playwright/test'" "$FILE"` — when the suite has a fixtures module, `test`/`expect` must come from it (type-only imports excepted) |
| 9 | No top-level tenant/variant branching | `grep -nE 'if \(.*(customer\|tenant\|variant)' "$FILE"` — variants belong in data factories, not an `if (...)` wrapping the spec or a block |
| 10 | No hardcoded credentials/URLs | `grep -nE 'password\|token\|https?://' "$FILE"` — exclude comments, reference-link comments, and intentional external-URL assertions (a spec that asserts a logo links out); read the line |
| 11 | BDD steps present | `grep -cE "test\.step\('(Given\|When\|Then\|And)" "$FILE"` — should be > 0 for a UI journey |
| 12 | One behaviour per `test()` | manual: a single test asserting several unrelated outcomes should split |
| 13 | Cleanup wired for created entities | `grep -nE 'needsCleanup\|register[A-Z]\|finally' "$FILE"` — any spec that creates entities needs cleanup via `try/finally`, a `needsCleanup` flag, or a registration fixture |
| 14 | Skips use `test.skip`/`test.fixme`, not comments | manual: a commented-out test body is invisible to the runner; use `test.skip` / `test.fixme` |
| 15 | No `any` | `grep -nE ': any\b\|<any>\|as any' "$FILE"` |
| 16 | Snapshots only via the masked helper | `grep -nE 'toHaveScreenshot' "$FILE"` — a raw `toHaveScreenshot` in a spec fails; visual snapshots go through the suite's masked helper (animations off) |
| 17 | No committed auth/session artifacts | the diff must not add or modify `state.json` / `storageState*` / `.auth/*.json` or any session dump — auth state is generated by setup and gitignored, never version-controlled (it is a secret) |
| 18 | Reuse shared utilities (no duplication) | manual: flag spec/POM logic that duplicates a helper, page-object base, or component already in the suite's shared directories — name the shared file to use instead (the E2E surface of t3:review's DRY check) |
| 19 | Extract repeated multi-line actions | manual: a repeated multi-line sequence that is one logical action (locate → assert → click → assert) belongs as a method on the relevant Page Object, not inline in the spec |
| 20 | Access-control precondition asserted from identity API | manual: a spec testing role-gated or access-controlled behaviour must resolve the test account's actual identity from the app API (e.g. `/api/me/`) and assert it matches the expected role BEFORE asserting page behaviour — a spec that skips this precondition may pass vacuously on an unexpected identity; the expected-role contract derives from the guard source code, not the ticket description |

**Verdict rule.** Items 1-6, 8, 15, and 17 are **hard fails** for new or converted specs. Items 7,
9-14, 16, 18, 19, and 20 are **judgement fails** — flag them and require justification, but a legitimate
exception (with a comment) can stand. When you find a real bug or a hard-fail, the verdict is
**hold / changes-requested**, not approve-with-follow-ups (see `t3:review` for how a real bug means
hold, and how to post the finding inline on the diff rather than as a monolithic note).

## Test-plan review checks

When reviewing a test plan (the `steps` list inside the evidence note, or a standalone plan posted on a ticket), apply these three checks. Each is a distinct **hold** finding if violated.

**Modality match.** Confirm the plan used the right evidence type for each AC:

- Route-guard / RBAC / redirect / backend-boundary ACs should use a URL + expected redirect or curl code block, not screenshots.
- UI-feature ACs should use browser click steps and screenshots as the compare-against reference, not API substitutes.
- A terminal screenshot in a test plan is always wrong — it must be a browser screenshot or a text code block.
If the plan substituted API evidence for an undeployed FE step (curl instead of clicks), flag it: ask for click steps against a local stack or an explicit "⏳ blocked until deployed" marker.

**Conciseness.** A plan that reads more like a report than a checklist is a hold. Flag plans with repeated caveats, narrative analysis, or steps that do not resolve to a URL/click/expected-outcome tuple. A reviewer should be able to skim it in under two minutes.

**Field-context evidence for generated documents.** When a step asserts a term in a PDF, export, or rendered document, verify the step names the expected **field or labelled row**, not just a substring anywhere in the document. "The PDF contains X" is insufficient — "the Security row shows X" is the required form. Flag any evidence claim whose verification probe is a page-wide substring match; also flag test steps whose fixture names embed the feature keyword (a borrower named "E2E FeatureName" defeats a naive full-text check for "FeatureName").

## Locator hygiene — the one rule worth its own paragraph

The single most common E2E review failure is a locator that is not a stable contract:

- **Prefer a test-id locator** (`getByTestId()` / `[data-test="..."]`) over everything else — it is
  the only locator the frontend and the test agree to keep stable. Most projects configure
  `testIdAttribute` to `data-test`.
- **Flag text / role-name locators** (`getByText`, `getByLabel`, `getByRole({ name })`) — they break
  the moment the UI copy or locale changes. They are acceptable only for locale-invariant strings
  (codes, emails, identifiers that are never translated) with a comment saying so, or when the spec
  is **asserting** user-visible copy rather than **locating** by it.
- **Selectors live in Page Objects, not the spec.** If a stable locator is missing in the app, add a
  `data-test` attribute in the frontend and use it — do not text-match around the gap.

## Conversion — landing an externally-authored suite

A suite that runs green elsewhere is **not** ready to land. Green-elsewhere is necessary, not
sufficient: an outside spec carries structural debt (flat layout, no shared Page Objects, hard waits,
text locators, committed auth artifacts) that the team would inherit. Before a converted spec can
merge, every item below must be true.

### Step 0 — inventory the incoming suite

Size the conversion before touching anything. From the incoming suite's root:

```bash
# spec count
find . -name '*.spec.ts' | wc -l
# debt counters
grep -REc 'waitForTimeout' --include='*.spec.ts' . | grep -v ':0$'
grep -REn 'getByText\(|getByRole\([^)]*name' --include='*.spec.ts' .
grep -REn 'page\.\$\$?\(|\$eval\(|waitForSelector' --include='*.spec.ts' .
# committed auth artifacts (must never land)
find . -name 'state.json' -o -name 'storageState*' -o -path '*.auth/*'
# scratch specs (drop them)
find . -name 'debug-*.spec.ts'
```

### Conversion checklist (every item must hold before merge)

1. **Re-home into the matching suite directory** the project already uses — not a new top-level
   tree. Follow the destination suite's layout.
2. **Move selectors into Page Objects** under the project's page-object directory. No raw
   `[data-test]` / CSS left in the spec. If a stable locator is missing in the app, add a
   `data-test` attribute in the frontend and use it — do not text-match.
3. **Remove hard waits** (`waitForTimeout`), replaced with auto-waiting assertions,
   `waitForResponse`, or form helpers.
4. **Replace text / role-name locators** with `data-test`. Keep text only for asserting
   user-visible copy.
5. **Use the suite's auth setup** (`global-setup`, `storageState`, the project's login helpers). Do
   not invent a login flow. **Never commit `state.json` / `storageState` / any auth artifact — they
   are secrets.**
6. **Seed preconditions via API factories**, not by walking the creation UI. Adopt the project's
   factory pattern. UI is only for the thing under test.
7. **Move tenant/variant/scenario data into typed fixtures**; remove inline `if (variant ...)`
   branches.
8. **Route imports through the suite's fixtures module** (`test` / `expect` re-exported), not
   directly from `@playwright/test`.
9. **Drop debug/scratch specs** (`debug-*.spec.ts` and the like are not suite material).
10. **Tag per the suite's convention** and confirm the suite-scoped **typecheck passes**.
11. **Document net-new coverage** — if the suite covers a surface it did not before, add it to the
    suite's index/README and wire the CI trigger.

A spec that "runs green" but skips items 1-9 is rejected: it imports tech debt the team will pay for.

### Conversion procedure

1. **Inventory** (Step 0 above) — this sizes the work and surfaces the auth artifacts to strip.
2. **Per spec**, apply the conversion checklist. Read the full destination Page Object / fixture /
   data files **before** writing, so the converted spec reuses what already exists instead of
   duplicating it.
3. **Run this review checklist** against each converted spec as the pass/fail gate before declaring
   it ready.
4. **Typecheck and run** the converted specs under the destination suite's config (use `t3:e2e`).

## Reviewer self-check — common mistakes

- **Approving a converted spec because it "runs green."** Green is necessary, not sufficient — run
  the conversion checklist. The structural debt is invisible to a green run.
- **Flagging every `https://` as a hardcoded URL.** Most hits are reference-link comments or an
  intentional external-URL assertion (a spec asserting a logo opens an external site — that IS the
  thing under test). Read the line before flagging.
- **Flagging `.textContent()` inside `expect.poll(...)`.** That is the correct retrying pattern, not
  a bare manual read.
- **Hard-failing the whole legacy corpus.** Scope checks to the changed lines; pre-existing debt the
  change merely sits next to is not the PR's regression (see `t3:review` § what to skip).
- **Restating an authoring rule in the finding instead of citing it.** Link the section of the
  project's E2E doc; do not re-derive the rule in the review note.
- **Posting a real bug as a general note.** A real bug is a hold, posted inline on the offending
  diff line — see `t3:review`.

## What to skip

- **Formatting** — Prettier / the project's formatter runs automatically; do not flag style.
- **Subjective opinions** — flag concrete convention violations, not personal preference.
- **Pre-existing debt unrelated to the diff** — flag a regression the change introduces, not debt it
  merely sits next to.

## Delegation

- `t3:e2e` — the author's skill: writing, running, snapshotting, and posting evidence for a spec.
- `t3:review` — the general review skill: self-review, giving review, receiving feedback, the
  real-bug-means-hold rule, and inline-finding posting.

The `t3:e2e-review` agent (`agents/e2e-review.md`) bundles this skill and is dispatched for the
`e2e_reviewing` phase — the reviewer leg of the EXTERNAL `ReviewLoop` (BLUEPRINT § 5.6, #2298).
