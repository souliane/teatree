---
name: dreaming
description: Runs the idle-time "dreaming" memory-consolidation pipeline end to end with one command — replay recent transcripts + curated memories, distil drift into the ConsolidatedMemory ledger, cross-link / re-index / decay the memory files, run the §4 acceptance gates, triage each row into keep-as-memory vs core-gap → drive each core gap to a MERGED fix under the standing umbrella issue, and promote/stage eval candidates. Use when user says "dream", "dreaming", "consolidate memory", "run the dream pass", "memory consolidation", or wants the full dream pipeline.
eval_exempt: thin command-runner skill — its only action is `t3 dream run --full`, whose whole pipeline (durable_destination persistence, triage core-gap vs user-specific, umbrella-checkbox upsert + schedule_coding, reconcile-on-merge, eval promotion/derivation, the §4 acceptance gates) is graded deterministically by the dream engine/command/model tests; downstream fix delivery delegates to the code/ship skills, whose own evals grade that behaviour.
compatibility: macOS/Linux, git, gh CLI.
requires:
  - workspace
  - rules
  - platforms
metadata:
  version: 0.0.1
---

# Dreaming — end-to-end memory consolidation

One command runs every phase; then drive the output the rest of the way.

## Run the full pass

`t3 dream run --full`  (add `--dry-run` to preview without writing rows, files, or tickets)

It runs, in order:

1. replay recent session transcripts + curated `~/.claude` memories
2. distil drift into the `ConsolidatedMemory` ledger (phases 1-3)
3. cross-link / re-index / decay the memory files (phases 4-6)
4. the §4 acceptance gates — a lossy pass is NOT stamped success, and it **exits non-zero** so a blocked pass never reads as a healthy one ([#3993](https://github.com/souliane/teatree/issues/3993)). Read the `WARN … acceptance gate(s) FAILED` line: it names the failing gate. `t3 doctor check` hard-FAILs once a once-working pass has withheld the marker for 6 days.
5. triage each ledger row: keep-as-memory (user-specific) vs core-gap → drive each core gap to a fix-and-merge (§ "promote = fix-and-merge" below)
6. promote grounded eval candidates to live `under_load` scenarios (anti-vacuity guard) and stage LLM-derived ones for review

## promote = fix-and-merge (the standing umbrella, [#2663](https://github.com/souliane/teatree/issues/2663))

The promote/compliance phases no longer file a fresh `needs-triage` issue per gap (those piled up — the issue scanner SKIPs `needs-triage`). Instead each grounded gap (a core-gap memory, a compliance recurrence, an automatable ask) is QUEUED into the pass's shared `batch_promote.PromotionBatch` and driven to a MERGED fix tracked under ONE standing umbrella issue ([#2663](https://github.com/souliane/teatree/issues/2663) — reused daily, NEVER closed):

1. **Upsert a checkbox** under #2663 for every pending gap, keyed on a stable gap key (an invisible `<!-- dream-gap <key> -->` marker per line) so the same gap never double-adds.
2. **Schedule ONE fix for the WHOLE pass** ([#4776](https://github.com/souliane/teatree/issues/4776)) — not one per gap: `batch_promote.promote_batch` mints AT MOST ONE ticket carrying every pending gap's manifest, once every promoting phase has run, via the existing `Ticket.schedule_coding()`. A coder fixes each gap independently and drops any it cannot deliver rather than stretching the change to cover it; a PR claiming a gap it did not deliver is a review HOLD.
3. **Reconcile on merge**: when a batch Ticket reaches MERGED, `batch_promote.reconcile_batches` CHECKS the umbrella checkbox and retires the linked `ConsolidatedMemory` ONLY for the gaps the coder/reviewer recorded as delivered (a BINDING memory is never retired, and its source file is never deleted). An undelivered gap's checkbox stays unchecked and the next pass re-offers it. No new model — an in-flight batch is a `Ticket` row (carrying the full gap manifest) + each gap's `ConsolidatedMemory` entry; the #2663 checkbox is the durable cross-night state.

The umbrella-checkbox upsert + batch scheduling is **default ON** for core-gap memory promotion (`[loops.dream] memory_promote` / `T3_DREAM_MEMORY_PROMOTE`, flipped ON by [#4685](https://github.com/souliane/teatree/issues/4685)/[#4776](https://github.com/souliane/teatree/issues/4776) now that batching bounds the fan-out) and **default OFF** for compliance escalation (`[loops.dream] compliance_escalate` / `T3_DREAM_COMPLIANCE_ESCALATE`). Each toggle ALONE suffices — `--full` turns them all on for one manual pass but is never a phase's only way in, because the nightly `tick` cannot set it ([#4176](https://github.com/souliane/teatree/issues/4176)).

The nightly pass is not the only producer of umbrella gaps: `t3 <overlay> retro finding` puts one there synchronously, as a batch of one through the same `batch_promote` path and the same dedup, so a retro lesson and a dreamt gap drain identically ([`skills/retro/SKILL.md`](../retro/SKILL.md) § Tooling). It obeys the `memory_promote` toggle above; with the toggle off it records the gap and defers the write.

**`promotion_cap` is deleted** ([#4776](https://github.com/souliane/teatree/issues/4776)). There is nothing left to ration: a pass with 300 pending gaps schedules ONE ticket, not 300 and not a capped 5 — the batch boundary is the pass itself.

**Compliance is measured on every pass.** The instruction-compliance accountant ([#2663](https://github.com/souliane/teatree/issues/2663) — the root KPI) is split in two: MEASUREMENT persists a snapshot every pass and is **default ON** (`[loops.dream] compliance_measure` / `T3_DREAM_COMPLIANCE_MEASURE`); ESCALATION files the enforcement fixes and is **default OFF**, gated by `compliance_escalate`. So `t3 dream compliance show` starts reflecting the rate after any pass, while ticket-filing stays opt-in. A pass observing 0 instructions records nothing (WARN).

## Drive the rest of the output

- Core-gap fixes are auto-scheduled and merge via the keystone; you don't hand-work them. Watch #2663 for the checkbox trend (checked = its fix merged).
- Commit any auto-promoted eval scenarios (`evals/scenarios/promoted_drift.yaml`) and ratify staged ones via PR. <!-- skill-symbol-ref: the promotion destination, written on the first promotion -->
- A binding-reconciliation conflict still files a deduped `dream-binding-reconcile` issue for a human (distinct from the gap pile-up the umbrella replaces).

## Trigger surface

- Manually: `/t3:dreaming` or `t3 dream run --full`.
- In a sub-agent: load this skill and run the same command.
- Unattended: `t3 dream tick` fires on the nightly cadence. It never sets `--full`, so the default-OFF ticket-filing / LLM-derivation / live-eval-validation phases stay off — but each is reachable from the tick by setting its own `[loops.dream]` toggle; every promoting phase still collapses into at most one batch ticket per pass.
