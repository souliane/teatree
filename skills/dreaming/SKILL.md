---
name: dreaming
description: Runs the idle-time "dreaming" memory-consolidation pipeline end to end with one command — replay recent transcripts + curated memories, distil drift into the ConsolidatedMemory ledger, cross-link / re-index / decay the memory files, run the §4 acceptance gates, triage each row into keep-as-memory vs core-gap → file a needs-triage teatree ticket, and promote/stage eval candidates. Use when user says "dream", "dreaming", "consolidate memory", "run the dream pass", "memory consolidation", or wants the full dream pipeline.
eval_exempt: thin command-runner skill — its only action is `t3 dream run --full`, whose whole pipeline (durable_destination persistence, triage core-gap vs user-specific, ticket filing, eval promotion/derivation, the §4 acceptance gates) is graded deterministically by the dream engine/command/model tests; downstream fix delivery delegates to the code/ship skills, whose own evals grade that behaviour.
compatibility: macOS/Linux, git, gh CLI.
requires:
  - workspace
  - rules
  - platforms
triggers:
  priority: 90
  keywords:
    - '\b(dream(ing)?|run the dream( pass)?|memory consolidation|consolidate (the )?(memory|memories)|full dream)\b'
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
5. triage each ledger row: keep-as-memory (user-specific) vs core-gap → file a `needs-triage` teatree backlog ticket
6. promote grounded eval candidates to live `under_load` scenarios (anti-vacuity guard) and stage LLM-derived ones for review

## Drive the output to merged PRs

The pass files core-gap tickets and may stage evals; it never implements. To finish:

- For each filed `dream-memory-gap` ticket: implement the fix in a worktree (TDD), open a PR, merge per the repo's mode. `/t3:teatree-batch` works the queue one ticket at a time.
- Commit any auto-promoted eval scenarios (`evals/scenarios/promoted_drift.yaml`) and ratify staged ones via PR.
- A retired memory's prose is archived once its linked ticket closes (next pass).

## Trigger surface

- Manually: `/t3:dreaming` or `t3 dream run --full`.
- In a sub-agent: this skill is `subagent_safe` — load it and run the same command.
- Unattended: `t3 dream tick` fires on the nightly cadence (no `--full`; it promotes grounded evals but leaves the default-OFF ticket-filing / LLM-derivation phases off).
