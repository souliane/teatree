---
name: loops
description: 'Show t3 loop status and trigger DB-configured loops — which loops are running vs stalled, the cadence/next-tick of each, loop ownership, and how to trigger a master tick. Use when the user says "which loops are running", "loop status", "loops", "loop health", "is the loop ticking", "trigger a loop", "run the loops".'
eval_exempt: thin `t3 loop list` / `t3 loops` CLI reference; the loop FSM + DB-table cadence behaviour is covered by tests/teatree_loops/ and the regression corpus, not by agent prose here
compatibility: any
triggers:
  priority: 50
  keywords:
    - '\b(which loops are running|loop status|loops|loop health|is the loop ticking|trigger a loop|run the loops)\b'
requires:
  - rules
metadata:
  version: 0.0.2
  subagent_safe: false
---

# Loops — DB-Configured Loop Status + Trigger

The day's autonomous work is driven by **DB-configured loops** (#1796/#2513). Each `Loop` row is the durable definition of one autonomous loop — a unique name, exactly one of a `script` or a `Prompt` (the loop XOR), its cadence (`delay_seconds` interval or `daily_at` wall-clock), an `enabled` flag, and `last_run_at` (the cadence anchor). The DB `Loop` table is the **single source of truth** for which loops run and on whose cadence: the live tick reads the table (#2513 cutover), and so do the statusline and `t3 loop list`. The domain scanners under `teatree.loops` stay as the scan units a loop invokes — they are not separate loops.

## When to load

Load `/t3:loops` to read the live loop state ("which loops are running", "loop status", "loop health", "is the loop ticking") or to trigger the DB-configured loops ("trigger a loop", "run the loops").

## Reading status (read-only)

```bash
t3 loop list            # live loop status, computed from the DB Loop table
t3 loop list --json      # the same status as a machine-readable payload
t3 loops list            # the DB Loop rows directly: name, enabled, cadence, last run, next due
```

`t3 loop list` is the live glance — it recomputes on every call, unlike `t3 loop status` which prints the cached statusline written at the last tick (its countdowns go stale). Both `t3 loop list` and the statusline now read the `Loop` table, so they never drift.

### Output contract (`t3 loop list`)

- Two labeled sections in fixed order — **`infra slots:`** first, then **`mini-loops:`** — never merged. The mini-loop rows reflect the `Loop` table (each row's `enabled`, cadence, `last_run_at`, next-due).
- Each loop line is `<name>  <enabled|disabled>  cadence <dur>  last <age>  next <when>`; infra-slot lines append `held` or `idle`.
- `next` reads `overdue` when the next fire is in the past and `—` when the loop has never fired (`last` is also `—`).
- One **loop-owner** line: the owning session id, its `owner_pid`, whether that pid is `alive` or `dead/unknown`, and whether the claim is `live` or `stale`; `unclaimed` when no session holds it.
- A **STALL** line appears only when the most recent tick is older than twice the tick cadence: `STALLED — last tick <age> ago`, followed by a one-line remediation hint.
- No preamble, no "Here is the loop status."

## Triggering loops

```bash
t3 loops tick            # the MASTER tick: run every enabled, due Loop row ONCE (each on its own cadence), then render
t3 loops run             # the master CONTINUOUSLY: tick, wait --interval, tick — until interrupted
```

The master claims the singleton `t3-master` lease; a non-owner session SKIPs. Only loops whose `Loop` row is `enabled` AND `is_due` AND that `LoopsConfig.is_enabled` admits fan out — a disabled or cooling row is skipped, AND a loop held by a `LoopState` pause/disable (`t3 loop pause`/`disable`) or the `T3_LOOPS_DISABLED` env kill-switch is skipped too (the unified verdict, #2584), so triggering the master never runs a held loop and never bumps a held loop's cadence anchor. The scoped per-loop runner (`run_scoped_tick`, the entry every `Loop` row references via `run.py`) honours `Loop.enabled` symmetrically. Per-loop config (cadence, enabled, prompt vs script) is edited in the Django admin (`Loop` rows). A prompt-backed loop runs its `Prompt` body as the per-tick instruction — see `/t3:prompts`.

For ownership hand-off, claiming, the lease/owner machinery, and how the cron drives the tick, see `t3:teatree`.
