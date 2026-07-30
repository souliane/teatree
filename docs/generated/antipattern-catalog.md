# Architectural Anti-Pattern Catalog

Generated from `src/teatree/quality/antipatterns.yaml` by
`scripts/hooks/generate_antipattern_catalog.py`. Do not edit by hand —
edit the YAML and regenerate.

This is the single source of truth feeding the three review tiers:
design-time (`architecture-design`), per-PR deterministic
(`scripts/hooks/check_antipatterns.py`, manual stage), and periodic
holistic (`ac-reviewing-codebase`).

**20 entries** — 4 greppable, 16 judgement.

## Index

- [Test function with no assertion](#assert-nothing-test) — high, judgement
- [Lower-level module importing a higher-level one](#backwards-dependency-edge) — high, judgement
- [Test that writes its own baseline / snapshot](#baseline-auto-accept) — high, judgement
- [FloatField for currency](#float-for-money) — high, judgement
- [Liveness path hard-fails a transient and locks the factory out](#gate-fails-closed-on-transient) — high, judgement
- [Security or merge gate fails open on exception](#gate-fails-open-on-error) — high, judgement
- [Gate classifies read-vs-write by verb instead of effective mutation](#gate-ignores-effective-write-semantics) — high, judgement
- [GET request with side effects](#get-with-side-effects) — high, greppable
- [Strip a qualifier to force an identity match](#identity-strip-to-match) — high, greppable
- [One item's exception aborts the whole sweep](#loop-scanner-no-fault-isolation) — high, judgement
- [Same fact in two co-equal stores with no authority](#multi-store-no-arbiter) — high, judgement
- [Canonicalization that is not idempotent](#non-idempotent-canonicalization) — high, judgement
- [Deny handler keyed on a tool no matcher delivers](#phantom-gate) — high, greppable
- [Identity matching that depends on the filesystem](#fs-dependent-identity-matching) — medium, judgement
- [Module past the health threshold](#god-module) — medium, judgement
- [Business logic in a view or management command](#logic-in-view-or-command) — medium, judgement
- [Overlay re-wraps a platform API instead of using the extension point](#plugin-wraps-platform-api) — medium, judgement
- [Signal carrying core domain flow](#signal-for-core-flow) — medium, greppable
- [Fallback chain that hides the primary failure](#silent-fallback-chain) — medium, judgement
- [List/fetch reads only the first page](#silent-truncation-pagination) — medium, judgement

## Strip a qualifier to force an identity match

<a id="identity-strip-to-match"></a>

- **id:** `identity-strip-to-match`
- **severity:** high
- **detection:** greppable
- **grep hint:** `\.split\(":"\)\[-1\]|\.rsplit\(":", ?1\)\[-1\]`
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase, linter
- **refs:** arch-design-check-8, identity-normalization-skill-note

**Anti-pattern.** Dropping a namespace/scope/prefix off one side (split(":")[-1], rsplit("/", 1)[-1], removeprefix) so an under-qualified reference matches a qualified key. Stripping discards qualifying information and silently conflates genuinely distinct entities.

**Preferred.** Canonicalize UP to the fully-qualified form at every boundary through one source-of-truth normalization function; never strip down to make a comparison succeed.

## Canonicalization that is not idempotent

<a id="non-idempotent-canonicalization"></a>

- **id:** `non-idempotent-canonicalization`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** arch-design-check-8

**Anti-pattern.** A normalize() whose output, fed back in, differs from its first output — so the same logical identity resolves differently depending on how many times it was passed through the boundary.

**Preferred.** normalize(normalize(x)) == normalize(x) for every input; assert idempotence in a test against the real corpus of forms.

## Identity matching that depends on the filesystem

<a id="fs-dependent-identity-matching"></a>

- **id:** `fs-dependent-identity-matching`
- **severity:** medium
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** arch-design-check-8

**Anti-pattern.** Resolving whether two references denote the same entity by touching the filesystem (case-folding via the OS, realpath, glob) — so the same inputs match on one machine and not another.

**Preferred.** Decide identity from the canonical string form alone; keep matching pure and platform-independent. Read the filesystem only to act, never to decide identity.

## Security or merge gate fails open on exception

<a id="gate-fails-open-on-error"></a>

- **id:** `gate-fails-open-on-error`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** enforcement-gate-family

**Anti-pattern.** A gate that protects a privacy/merge/publish boundary swallows an exception and returns "allow", so a transient error silently disables the protection.

**Preferred.** A boundary-protecting gate fails CLOSED — an error denies. Only liveness paths fail open, and never the public-egress leak gate.

## Liveness path hard-fails a transient and locks the factory out

<a id="gate-fails-closed-on-transient"></a>

- **id:** `gate-fails-closed-on-transient`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** enforcement-gate-family, gate-self-rescue-note

**Anti-pattern.** An always-on liveness gate (orchestrator-boundary, skill-loading, plan) hard-denies on a transient/broken-env condition, so a recoverable blip locks the factory out with no self-rescue.

**Preferred.** Route liveness denials through a shared fail-open chokepoint with an always-allow-listed self-rescue command and an out-of-repo kill-switch.

## Gate classifies read-vs-write by verb instead of effective mutation

<a id="gate-ignores-effective-write-semantics"></a>

- **id:** `gate-ignores-effective-write-semantics`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **eval invariant:** `no_raw_review_post`
- **consumers:** architecture-design, ac-reviewing-codebase, eval
- **refs:** effective-method-classifier, gh-glab-last-wins-finding

**Anti-pattern.** Deciding whether a forge command is a read or a write by the literal verb/tool-name or the mere presence of a method flag, so a last-wins -X/--method override (gh/glab) bypasses the write gate.

**Preferred.** Classify by EFFECTIVE HTTP method — last -X/--method wins; body/field flags default to POST — mirroring the transcript-conformance effective-method classifier.

## Deny handler keyed on a tool no matcher delivers

<a id="phantom-gate"></a>

- **id:** `phantom-gate`
- **severity:** high
- **detection:** greppable
- **grep hint:** `phantom_reason\s*=\s*\w`
- **linter:** `gate-liveness`
- **consumers:** architecture-design, ac-reviewing-codebase, eval
- **refs:** gate-liveness-corpus, phantom-gate-roster

**Anti-pattern.** A correct-looking deny handler keyed on a tool/skill that no registered hooks.json matcher delivers to its event, so the gate never fires in production despite passing its unit test.

**Preferred.** Add a gate-liveness corpus row asserting reachability (the matched tool is delivered to the handler's event); a handler whose tool is absent from every matcher is xfail-tracked as a known phantom, never silently shipped.

## Test function with no assertion

<a id="assert-nothing-test"></a>

- **id:** `assert-nothing-test`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** anti-vacuous-eval

**Anti-pattern.** A def test_... whose body exercises code but asserts nothing (no assert, no self.assert*, no pytest.raises), so it stays green no matter how the behaviour regresses. (Detecting "body lacks an assert" needs an AST walk, not a regex, so this stays a judgement call.)

**Preferred.** Every test names the observable contract and asserts it; if there is nothing to assert, there is no test.

## Test that writes its own baseline / snapshot

<a id="baseline-auto-accept"></a>

- **id:** `baseline-auto-accept`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** anti-vacuous-eval, baseline-auto-accept-redcard

**Anti-pattern.** A test path that re-captures or rewrites its snapshot/baseline as part of the run, so the assertion auto-accepts whatever the buggy screen currently shows. (A regex cannot tell a deliberate, reviewed baseline refresh from a test that silently rewrites its own expectation, so this stays judgement.)

**Preferred.** Baselines are captured deliberately and reviewed; the test asserts against a committed baseline and never rewrites it during a normal run.

## Business logic in a view or management command

<a id="logic-in-view-or-command"></a>

- **id:** `logic-in-view-or-command`
- **severity:** medium
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** ac-django-antipatterns-22

**Anti-pattern.** Domain logic living in a view, DRF viewset, or management command instead of on the model/queryset, so the same rule cannot be reused or tested in isolation.

**Preferred.** Fat models, thin views: domain behaviour lives as methods on the model and reusable query logic on a custom QuerySet (see ac-django antipatterns §22).

## FloatField for currency

<a id="float-for-money"></a>

- **id:** `float-for-money`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** ac-django-antipatterns-22

**Anti-pattern.** Storing money in a FloatField, accumulating binary floating-point rounding error on every arithmetic operation. (A regex cannot tell a money field from a legitimate float metric, so this stays a judgement call.)

**Preferred.** Use DecimalField (with explicit max_digits/decimal_places) for monetary values; consider a money type that carries the currency code.

**Accepted waivers.**

- TaskAttempt.cost_usd, EvalScenarioResult.cost_usd/main_cost_usd/aux_cost_usd — provider-cost telemetry, never invoicing; accepted 2026-07 full-tree review (F1.1/F1.2/F9.2). Revisit if these ever feed billing or exact-equality gates.

## GET request with side effects

<a id="get-with-side-effects"></a>

- **id:** `get-with-side-effects`
- **severity:** high
- **detection:** greppable
- **grep hint:** `@require_GET`
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase, linter
- **refs:** ac-django-antipatterns-22

**Anti-pattern.** A view bound to a safe HTTP GET that mutates state (delete/create/update), so a crawler, prefetch, or refresh silently triggers the mutation.

**Preferred.** Keep GET safe and idempotent; perform mutations under POST/PUT/PATCH/DELETE and enforce with @require_POST / @require_http_methods.

## Signal carrying core domain flow

<a id="signal-for-core-flow"></a>

- **id:** `signal-for-core-flow`
- **severity:** medium
- **detection:** greppable
- **grep hint:** `@receiver\(\s*post_save|@receiver\(\s*pre_save`
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase, linter
- **refs:** ac-django-antipatterns-22

**Anti-pattern.** A post_save/pre_save receiver implementing core business logic, hiding the flow from the call site and skipping it in data migrations and bulk operations.

**Preferred.** Call domain behaviour explicitly from a model method; reserve signals for integrating third-party app events.

## Module past the health threshold

<a id="god-module"></a>

- **id:** `god-module`
- **severity:** medium
- **detection:** judgement
- **linter:** `check_module_health`
- **consumers:** architecture-design, ac-reviewing-codebase, linter
- **refs:** module-health-hook

**Anti-pattern.** A single module accumulating unrelated responsibilities until it exceeds the LOC / module-function / typed-data health thresholds and becomes the place every change has to touch.

**Preferred.** Split by responsibility into cohesive units; prefer methods on classes over module-level functions and typed dataclasses over dict[str, object]. The LOC/function thresholds are mechanized by check_module_health over the diff.

## Lower-level module importing a higher-level one

<a id="backwards-dependency-edge"></a>

- **id:** `backwards-dependency-edge`
- **severity:** high
- **detection:** judgement
- **linter:** `tach`
- **consumers:** architecture-design, ac-reviewing-codebase, linter
- **refs:** module-dependency-graph

**Anti-pattern.** A lower-level module (utils, config, types) importing from a higher-level one (cli, core.management), inverting the dependency DAG and coupling the foundation to its consumers.

**Preferred.** Respect the tach-enforced DAG; break a backwards edge with a callback, registration, or Protocol before implementing the feature. tach decides direction from the declared edges — a regex over imports cannot.

## Overlay re-wraps a platform API instead of using the extension point

<a id="plugin-wraps-platform-api"></a>

- **id:** `plugin-wraps-platform-api`
- **severity:** medium
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** overlay-extension-points

**Anti-pattern.** An overlay reaching past OverlayBase to call platform internals directly, re-implementing behaviour the extension point already provides and drifting from the contract every other overlay honours.

**Preferred.** Implement the OverlayBase hook / Protocol method; let the platform own the behaviour uniformly across overlays.

## List/fetch reads only the first page

<a id="silent-truncation-pagination"></a>

- **id:** `silent-truncation-pagination`
- **severity:** medium
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** silent-truncation-note

**Anti-pattern.** A list or fetch that consumes only page one of a paginated API and treats the partial result as complete, silently dropping everything past the first page.

**Preferred.** Page to exhaustion (or assert the result fits one page); never let a truncated read masquerade as the full set.

## One item's exception aborts the whole sweep

<a id="loop-scanner-no-fault-isolation"></a>

- **id:** `loop-scanner-no-fault-isolation`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** loop-topology, fault-isolation-note

**Anti-pattern.** A loop/scanner sweep where one item raising an exception aborts the entire pass, so a single bad row stops every sibling from being processed.

**Preferred.** Isolate each item — catch and record per-item failure, continue the sweep — so one poison item never starves the rest.

## Fallback chain that hides the primary failure

<a id="silent-fallback-chain"></a>

- **id:** `silent-fallback-chain`
- **severity:** medium
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** resilience-invariants

**Anti-pattern.** A try/except-or-or chain that silently substitutes a fallback when the primary path fails, so a degraded result looks identical to a healthy one and the failure is never surfaced.

**Preferred.** A fallback is a sanctioned secondary path that records it was taken (log/signal/metric); silent substitution that masks the primary failure is the anti-pattern.

## Same fact in two co-equal stores with no authority

<a id="multi-store-no-arbiter"></a>

- **id:** `multi-store-no-arbiter`
- **severity:** high
- **detection:** judgement
- **linter:** _(none — gap)_
- **consumers:** architecture-design, ac-reviewing-codebase
- **refs:** resilience-invariants, single-source-of-truth-note

**Anti-pattern.** The same fact persisted in two or more co-equal stores with no declared source of truth, so the copies drift and readers disagree with no way to say which is right.

**Preferred.** Name ONE authoritative store; every other copy is a derived cache that refreshes from the authority and is never written independently.
