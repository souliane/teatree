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
6. validate grounded eval candidates and queue their artifacts on the umbrella ticket; stage LLM-derived ones for review

## promote = fix-and-merge (the standing umbrella, [#2663](https://github.com/souliane/teatree/issues/2663))

The promote/compliance phases no longer file a fresh `needs-triage` issue per gap (those piled up — the issue scanner SKIPs `needs-triage`), and a pass mints no ticket at all. Each grounded core-gap memory is QUEUED into the pass's shared `batch_promote.PromotionBatch`, then grouped into existing tickets like any other backlog item:

1. **Queue on the umbrella host**: `batch_promote.promote_batch` appends the pass's gaps to the `dream_gap_pending` ledger of the ticket whose `issue_url` is the `dream_umbrella_url` setting, deduped by gap key, and nudges the backlog sweep.
2. **Fold into existing hosts**: the `/t3:sweeping-tickets` sweep attaches each gap to the best EXISTING host with `t3 <overlay> ticket attach-gaps` (fold appended through the issue-write facade and re-read, a disposition rubric criterion, and planning for an unplanned host). A gap no host fits is attached to the umbrella ticket itself.
3. **Disposition, then reconcile on merge**: the host addresses each gap (`t3 dream gap-disposition <id> <gap_key> --citation …`) or rejects it with a reason (`--reject …`); `t3 dream gap-coverage` proves every gap has exactly one owner. When the host reaches MERGED, `batch_promote.reconcile_batches` retires the linked `ConsolidatedMemory` ONLY for the addressed gaps (a BINDING memory is never retired, and its source file is never deleted).

Core-gap memory promotion follows `T3_DREAM_MEMORY_PROMOTE` (default on). Recurrences are measured and retained for review; dream does not file enforcement tickets automatically.

The nightly pass is not the only producer of umbrella gaps: `t3 <overlay> retro finding` puts one there synchronously, as a batch of one through the same `batch_promote` path and the same dedup, so a retro lesson and a dreamt gap drain identically ([`skills/retro/SKILL.md`](../retro/SKILL.md) § Tooling). It obeys the `memory_promote` toggle above; with the toggle off it records the gap and defers the write.

The nightly pass is not the only producer of umbrella gaps: `t3 <overlay> retro finding` puts one there synchronously, as a batch of one through the same `batch_promote` path and the same dedup, so a retro lesson and a dreamt gap drain identically ([`skills/retro/SKILL.md`](../retro/SKILL.md) § Tooling). It obeys the `memory_promote` toggle above; with the toggle off it records the gap and defers the write.

**`promotion_cap` is deleted** ([#4776](https://github.com/souliane/teatree/issues/4776)). There is nothing left to ration: a pass with 300 pending gaps schedules ONE ticket, not 300 and not a capped 5 — the batch boundary is the pass itself.

**Compliance is measured on every pass.** The instruction-compliance accountant persists a snapshot on each pass and `t3 dream compliance show` reports the rate. A pass observing no instructions records nothing and warns.

## Drive the rest of the output

- Core-gap fixes are auto-scheduled and merge via the keystone; you don't hand-work them. Watch #2663 for the checkbox trend (checked = its fix merged).
- Follow up queued eval artifacts on their ticket and ratify staged `derived_evals.yaml` scenarios via PR.
- A binding-reconciliation conflict is queued as one deduped gap like any other, so a human settles it on the host the sweep folds it into; the pass files no issue.

## Trigger surface

- Manually: `/t3:dreaming` or `t3 dream run --full`.
- In a sub-agent: load this skill and run the same command.
- Unattended: `t3 dream tick` fires on the nightly cadence. It never sets `--full`, so the optional LLM derivation and live eval validation phases stay off. Each remains reachable from the tick through its own `[loops.dream]` setting.
