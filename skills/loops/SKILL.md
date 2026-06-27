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
  version: 0.0.4
  subagent_safe: false
---

# Loops — DB-Configured Loop Status + Trigger

The day's autonomous work is driven by **DB-configured loops** (#1796/#2513). Each `Loop` row is the durable definition of one autonomous loop — a unique name, exactly one of a `script` or a `Prompt` (the loop XOR), its cadence (`delay_seconds` interval or `daily_at` wall-clock), an `enabled` flag, and `last_run_at` (the cadence anchor). The DB `Loop` table is the **single source of truth** for which loops run and on whose cadence: the live tick reads the table (#2513 cutover), and so do the statusline and `t3 loop list`. The domain scanners under `teatree.loops` stay as the scan units a loop invokes — they are not separate loops.

**One native Claude `/loop` per enabled row (#2650).** There is no single fat-tick cron. The live set of native Claude Code `/loop`s **mirrors** the set of **enabled** `Loop` rows — ONE `/loop` per enabled loop (per-loop, not per-group), each firing `t3 loops tick --loop <name>` on that loop's own cadence. The **loop-owner** session registers them all at session start (a non-owner registers nothing); enabling/disabling a loop mirrors into Claude Code by CronCreate/CronDelete-ing that one loop's `/loop` (see *Enabling / disabling* below).

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
t3 loops tick                 # the MASTER tick: run every enabled, due Loop row ONCE (each on its own cadence), then render
t3 loops tick --loop <name>   # run ONE enabled, due loop — the per-loop primitive each native Claude `/loop` fires (#2650)
t3 loops run                  # the master CONTINUOUSLY: tick, wait --interval, tick — until interrupted
```

`t3 loops tick --loop <name>` is what each native Claude `/loop` runs on its own cadence: it scopes the DB-master to that single row, claims the disjoint per-loop `loop:<name>` lease (so the N per-loop loops run in parallel, never serialised on the singleton `t3-master`), and skips the master piggyback cycles. It still honours the same enabled / due / unified-verdict gates as the full master.

The master claims the singleton `t3-master` lease; a non-owner session SKIPs. Only loops whose `Loop` row is `enabled` AND `is_due` AND that `LoopsConfig.is_enabled` admits fan out — a disabled or cooling row is skipped, AND a loop held by a `LoopState` pause/disable (`t3 loop pause`/`disable`) is skipped too (the unified verdict, #2584), so triggering the master never runs a held loop and never bumps a held loop's cadence anchor. Loop control is `/loops` (`t3 loop enable/disable/pause/resume`) + the DB `LoopState` tier only — there is no env kill-switch. Each script-backed `Loop` row carries its OWN on-disk entry point `src/teatree/loops/<name>/loop.py` (the module exposing that loop's `MINI_LOOP`) — the `script` column is per-loop and load-bearing; there is no shared runner. The live tick reads each admitted row's column to decide what to dispatch, and the scoped per-loop runner (`run_scoped_tick`) honours `Loop.enabled` symmetrically; a row whose `script` does not resolve to a real registered loop module raises loudly rather than silently running nothing. A prompt-backed loop runs its `Prompt` body as the per-tick instruction — `arch_review` is the one prompt-backed default, instructing a sub-agent to run an architectural review with the `ac-reviewing-codebase` skill — see `/t3:prompts`.

## Enabling / disabling a loop

```bash
t3 loop enable <name>     # turn a loop ON  — sets BOTH Loop.enabled=True AND the LoopState control tier to ENABLED
t3 loop disable <name>    # turn a loop OFF — sets BOTH Loop.enabled=False AND the LoopState kill-switch to DISABLED
t3 loop resume <name>     # alias of enable — lift either a pause or a disable, return the loop to running
t3 loop pause <name>      # reversible hold (LoopState only) — does NOT flip the durable Loop.enabled row
t3 loop loop-state <name> # read the durable LoopState status (ENABLED when never touched)
```

`enable`/`disable`/`resume` move the TWO planes the #2584 unified verdict reads in lock-step inside one transaction: the durable `LoopState` control tier (#1913) AND the row-level `Loop.enabled` column that the master tick gates on (`not row.enabled` skips a loop). They are the agent-facing way to toggle `enabled`; the Django admin (`Loop` rows) remains the place to edit a loop's cadence and prompt-vs-script. `pause` is the reversible control-plane hold only — it leaves `Loop.enabled` untouched so a paused loop returns to running with `resume` without re-enabling a row that was deliberately `disable`d.

### Mirror the toggle into Claude Code (#2650)

Flipping the DB row is only half the job: the live set of native Claude `/loop`s must mirror the enabled rows. A CLI **cannot** call `CronCreate`/`CronDelete` itself, so YOU (the agent, holding the harness cron tools) mirror it. `t3 loop claude-spec <name> [--json]` prints the loop's exact `/loop` spec — its stable `slot_id`, the `cron` derived from its cadence, and the recurring `prompt` (`t3 loops tick --loop <name>`).

**To enable a loop `X` and start its `/loop`:**

1. `t3 loop enable X` — flips `Loop.enabled=True` + `LoopState=ENABLED`.
2. `t3 loop claude-spec X --json` — read `{slot_id, cron, prompt}`.
3. Call `CronCreate(cron=<cron>, prompt=<prompt>, recurring=true)` — register the native `/loop`.

**To disable a loop `X` and stop its `/loop`:**

1. `t3 loop disable X` — flips `Loop.enabled=False` + `LoopState=DISABLED`.
2. `t3 loop claude-spec X --json` — read the full `prompt`.
3. `CronList`, find the job whose prompt **equals that full `prompt` string** (equivalently, contains the exact backtick-terminated token `` `t3 loops tick --loop X` `` — the **closing backtick is required**: a bare `--loop X` substring also matches a longer name like `X-fast`, so without it disabling `ship` could delete `ship-fast`'s cron). Then `CronDelete(id=<that job id>)` — remove the native `/loop`.

(`t3 loop claude-spec` computes the spec from the row regardless of `enabled`, so reading it after a `disable` still works.)

For ownership hand-off, claiming, the lease/owner machinery, and how the cron drives the tick, see `t3:teatree`.
