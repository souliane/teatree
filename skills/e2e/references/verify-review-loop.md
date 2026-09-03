# The verify–review loop — FSM edges, terminal states, and the threshold

The mechanics behind `/t3:e2e` § "Verify–Review Loop to Threshold". That section carries why the loop exists; this file carries its five FSM edges, the three terminal states, and the `e2e_confidence_threshold` setting.

## The loop as FSM edges (max 5 iterations per ticket)

1. **`test` / e2e phase — `/t3:e2e`.** Run the spec — against **DEV** if the feature is deployed there, else a **local stack** restored from the DEV dump (§ "Dual-Env Testing" and § "Replicating a DEV object to local"). On failure, **bug-hunt**: browser console first (§ "Browser Console First"), then screenshot sanity (§ "Screenshot Sanity Check"), driving the page with chrome-devtools-mcp where it helps. **Codify every confirmed finding into a committed Playwright spec** — a browser observation that isn't captured as a durable assertion is lost; the bug-hunt's output is *new committed test code*, not a note. If a real **product bug** surfaces, fix it. Opportunistically **consolidate** duplicated/outside specs into the canonical suite via the `/t3:e2e-review` § "Adopting an outside Playwright suite" conversion method. Then `/next`.
2. **→ `e2e_reviewing` phase — `/t3:e2e-review`.** Score the spec (and its run) with the **E2E Confidence Rubric** (`/t3:e2e-review` § "E2E Confidence Rubric"): every hard gate, then the six weighted criteria, returning `{score, threshold, verdict, findings}`.
3. **VERIFIED** — `score ≥ threshold` AND all hard gates pass. `/next` advances toward `ship`: commit the specs, open/merge the e2e PR, and **post the clean test plan** (§ "Post Testing Evidence on the Ticket"), recording the rubric score alongside the run. **If the ticket also changed product code**, the normal `review` phase (code review, maker ≠ checker) sits between `e2e_reviewing` and `ship`; for a **pure test-adding ticket**, `e2e_reviewing → ship` directly. An optional `review-request` follows. Exit the loop.
4. **BLOCKED** — a **hard external gate** blocks (no broker account and local can't substitute; a broken login with no available fix; a result observable nowhere programmatically — the rubric's `BLOCKED(<named-gate>)`). Terminal: surface the **named gate** to the user, post **nothing caveated**, exit. Do not loop.
5. **HOLD** — below threshold (and fixable). The FSM loops **back to the `test`/e2e phase** (`e2e_reviewing --/next--> e2e`): a fresh `/t3:e2e` that applies the top rubric `findings` — fix spec brittleness, add the missing-AC assertions, fix the bug, de-flake — then re-scores. Re-loop.

## Terminal states (never loop forever)

- **VERIFIED** (`score ≥ threshold`, all hard gates pass) — the clean test plan is posted, the rubric score recorded.
- **BLOCKED(named gate)** — a genuinely-unreachable feature (manual-only/no-API, infra-gated). The named gate is surfaced to the user; no caveated note is posted.
- **MAX_ITERATIONS** (5 verify↔review rounds without VERIFIED) — stop and report the **best score reached** and the **precise remaining gap** (the specific rubric criteria/findings still short of threshold). Do not silently keep looping.

Never post a caveated note as a substitute for reaching the threshold: a note that says "verified, except…" is not a VERIFIED — it is a HOLD or a BLOCKED wearing a green coat. The whole point of the threshold is that 100% confidence is unreachable for some tickets, so the loop terminates honestly (BLOCKED or MAX_ITERATIONS) rather than pretending.

## Configuration

The pass bar is the DB-home **`e2e_confidence_threshold`** setting — an integer 0–100, **default 90**, **per-overlay overridable**. Set it in the `ConfigSetting` store; a stricter client overlay can raise it, a fast dogfood overlay can lower it. It is the single knob both the rubric (`/t3:e2e-review`) and this loop read, so "the threshold" means one value, resolved through the DB-home chain: overlay-scope DB row → global DB row → the dataclass default (no env layer for this setting).

Prefer the `mcp__teatree__config_setting_set` MCP tool — it accepts this key (a reviewed carve-out, since a quality tunable is not a safety gate) and applies the same registry validation; fall back to the CLI below when the MCP server isn't connected.

```bash
# CLI fallback (MCP server not connected)
t3 <overlay> config_setting set e2e_confidence_threshold 90   # rubric score a spec must reach to be VERIFIED (0-100)
t3 <overlay> config_setting set e2e_confidence_threshold 95 --overlay client-x   # stricter bar for a client overlay
```
