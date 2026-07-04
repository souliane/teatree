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
  subagent_safe: true
---

# Dreaming — end-to-end memory consolidation

One command runs every phase; then drive the output the rest of the way.

## Run the full pass

`t3 dream run --full`  (add `--dry-run` to preview without writing rows, files, or tickets)

It runs, in order:

1. replay recent session transcripts + curated `~/.claude` memories
2. distil drift into the `ConsolidatedMemory` ledger (phases 1-3)
3. cross-link / re-index / decay the memory files (phases 4-6)
4. the §4 acceptance gates — a lossy pass is NOT stamped success
5. triage each ledger row: keep-as-memory (user-specific) vs core-gap → drive each core gap to a fix-and-merge (§ "promote = fix-and-merge" below)
6. promote grounded eval candidates to live `under_load` scenarios (anti-vacuity guard) and stage LLM-derived ones for review

## promote = fix-and-merge (the standing umbrella, [#2663](https://github.com/souliane/teatree/issues/2663))

The promote/compliance phases no longer file a fresh `needs-triage` issue per gap (those piled up — the issue scanner SKIPs `needs-triage`). Instead each grounded gap (a core-gap memory, a compliance recurrence) is driven to a MERGED fix tracked under ONE standing umbrella issue ([#2663](https://github.com/souliane/teatree/issues/2663) — reused daily, NEVER closed):

1. **Upsert a checkbox** under #2663, keyed on a stable gap key (an invisible `<!-- dream-gap <key> -->` marker per line) so the same gap never double-adds.
2. **Schedule the fix** for each NEW gap via the existing `Ticket.schedule_coding()` — a coder implements it TDD in a worktree, opens a PR, and the PR merges through the SAME single keystone flow gated by the overlay's autonomy setting (no per-gap human triage).
3. **Reconcile on merge**: when a gap's fix Ticket reaches MERGED, the next pass CHECKS its umbrella checkbox and retires the linked `ConsolidatedMemory` (a BINDING memory is never retired). No new model — an in-flight gap is a `Ticket` row + its `ConsolidatedMemory` entry; the #2663 checkbox is the durable cross-night state.

The umbrella-checkbox upsert + `schedule_coding` runs under `t3 dream run --full`, **default OFF** behind `[loops.dream] memory_promote` / `T3_DREAM_MEMORY_PROMOTE` (and `[loops.dream] compliance` / `T3_DREAM_COMPLIANCE` for recurrences).

## Drive the rest of the output

- Core-gap fixes are auto-scheduled and merge via the keystone; you don't hand-work them. Watch #2663 for the checkbox trend (checked = its fix merged).
- Commit any auto-promoted eval scenarios (`evals/scenarios/promoted_drift.yaml`) and ratify staged ones via PR.
- A binding-reconciliation conflict still files a deduped `dream-binding-reconcile` issue for a human (distinct from the gap pile-up the umbrella replaces).

## Trigger surface

- Manually: `/t3:dreaming` or `t3 dream run --full`.
- In a sub-agent: this skill is `subagent_safe` — load it and run the same command.
- Unattended: `t3 dream tick` fires on the nightly cadence (no `--full`; it promotes grounded evals but leaves the default-OFF ticket-filing / LLM-derivation phases off).
