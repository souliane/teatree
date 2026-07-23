---
name: health
description: Read and act on the global operational-health chip — the green/yellow/red factory-health verdict and its known-issues registry. Use when the statusline health chip is yellow/red, or the user asks "what's wrong", "why is health red", "is the factory healthy", "known issues".
compatibility: any
requires:
  - rules
eval_exempt: thin detail/reference skill — points at the `health` CLI; no standalone agent behaviour to grade.
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Health — Global Operational-Health Chip + Known-Issues Registry

The statusline anchors zone carries a `health: ●` chip: a single green / yellow / red verdict for "is the factory healthy right now", plus the count of open issues. This skill is how you read the detail behind that dot and act on it.

`operational_health` (the global aggregator) is NOT `core.health` (per-worktree readiness probes). This chip is the factory-wide verdict; those probes are one worktree's post-provision checks.

## The verdict

Computed from deterministic durable signals — stale loop ticks, failed tasks, and each overlay's `get_health_signals()` — each persisted as a `KnownIssue` row so the verdict survives compaction.

- **red** — any critical signal, or three-or-more concurrent yellows.
- **yellow** — any non-critical signal.
- **green** — nothing open.

The chip is read-only (it never reconciles at render time). The loop tick reconciles the registry each beat, and `health show` reconciles before printing — so an auto-derived issue whose signal has cleared auto-resolves by construction; you never chase a stale entry.

## The single command

```bash
t3 <overlay> health show                 # reconcile + print the verdict and every open KnownIssue row
t3 <overlay> health show --json          # structured payload instead of the table
t3 <overlay> health add "<text>"         # record a manual issue the signals cannot see (warning)
t3 <overlay> health add "<text>" --critical   # record it at critical severity
t3 <overlay> health dismiss <id>         # acknowledge and close an open issue by id
```

`show` lists each open issue with its severity, overlay, and a **clickable evidence link** (the jump-to-proof URL the signal carried). Auto-derived rows resolve themselves when their signal clears; manual rows only ever close via `dismiss`.

## When to load

Load `/t3:health` when the statusline health chip is yellow or red, or the user asks "what's wrong", "why is health red", "is the factory healthy", or "what are the known issues". Read the detail with `health show`, then act on the specific issue — the chip is a pointer, the row's evidence link is where you look.

## Reading vs acting

`health show` is the read surface — start there. Then:

- A **stale-tick** issue points at a wedged loop — the evidence is the loop, not this skill; go fix or restart it.
- A **failed-tasks** issue is the durable "something failed" proxy — triage the failing tasks.
- An **overlay-declared** issue carries the overlay's own summary + evidence — follow it.
- Something the signals cannot see (a stale DB snapshot, a known-broken external dependency) → `health add` it so it is visible on the chip until resolved.
- An auto-derived issue you have chosen to live with → `health dismiss <id>`.

Resist building a dashboard. The chip + the `health show` table are the whole surface.

## Loop staleness — a health signal

The day's autonomous work is driven by **DB-configured loops** (#1796/#2513). Each `Loop` row is the durable definition of one autonomous loop — a unique name, exactly one of a `script` or a `Prompt` (the loop XOR), its cadence (`delay_seconds` interval or `daily_at` wall-clock), an `enabled` flag, and `last_run_at` (the cadence anchor). The DB `Loop` table is the **single source of truth** for which loops run and on whose cadence: the live tick reads the table (#2513 cutover), and so do the statusline and `t3 loop list`. The domain scanners under `teatree.loops` stay as the scan units a loop invokes — they are not separate loops.

**The `t3 worker` owns the per-loop tick cadence (#2650 / PR-28).** There is no single shared-tick cron and no master tick. The singleton `t3 worker` drains one self-rescheduling `loop_timer` chain per **enabled** `Loop` row (default ON via `loop_runner_enabled`), each firing `t3 loops tick --loop <name>` on that loop's own cadence — so the DB loops run with no Claude session open. PR-28 retired the native Claude `/loop` cron mirror: enabling/disabling a loop is now just the DB toggle below (the reconciler adds/prunes that loop's timer at once); check the worker with `t3 worker status`, ensure one is running with `t3 worker ensure`.

### When to load

Load `/t3:health` to read the live loop state ("which loops are running", "loop status", "loop health", "is the loop ticking") or to trigger the DB-configured loops ("trigger a loop", "run the loops").

### Reading status (read-only)

```bash
t3 loop list            # live loop status, computed from the DB Loop table
t3 loop list --json      # the same status as a machine-readable payload
t3 loops list            # the DB Loop rows directly: name, enabled, cadence, last run, next due
```

`t3 loop list` is the live glance — it recomputes on every call, unlike `t3 loop status` which prints the cached statusline written at the last tick (its countdowns go stale). Both `t3 loop list` and the statusline now read the `Loop` table, so they never drift.

#### Output contract (`t3 loop list`)

- Two labeled sections in fixed order — **`infra slots:`** first, then **`mini-loops:`** — never merged. The mini-loop rows reflect the `Loop` table (each row's `enabled`, cadence, `last_run_at`, next-due).
- Each loop line is `<name>  <enabled|disabled>  cadence <dur>  last <age>  next <when>`; infra-slot lines append `held` or `idle`.
- `next` reads `overdue` when the next fire is in the past and `—` when the loop has never fired (`last` is also `—`).
- One **t3-master** line: the owning session id, its `owner_pid`, whether that pid is `alive` or `dead/unknown`, and whether the claim is `live` or `stale`; `unclaimed` when no session holds it.
- A **STALL** line appears only when the most recent tick is older than twice the tick cadence: `STALLED — last tick <age> ago`, followed by a one-line remediation hint.
- No preamble, no "Here is the loop status."

### Triggering loops

```bash
t3 loops tick --loop <name>   # run ONE enabled, due loop — the per-loop primitive the worker's timer chain fires (#2650)
t3 loops tick                 # HARD ERROR: there is no master tick — `--loop <name>` is required (#2650)
```

`t3 loops tick --loop <name>` is what the worker's `loop_timer` chain runs on each loop's own cadence (and what you run to trigger a loop by hand): it scopes `build_loop_table_jobs` to that single row and claims the disjoint per-loop `loop:<name>` lease (so the N per-loop loops run in parallel, never serialised on the singleton `t3-master`). Running `t3 loops tick` with no `--loop` is a hard error — there is no master tick and no continuous interval-runner loop. It honours the enabled / due / unified-verdict gates on that one row.

Each per-loop tick claims that loop's `loop:<name>` lease; a non-owner session SKIPs. A loop runs only when its `Loop` row is `enabled` AND `is_due` AND the unified `LoopState` verdict (`loop_state_admits`) admits — a disabled or cooling row is skipped, AND a loop held by a `LoopState` pause/disable (`t3 loop pause`/`disable`) is skipped too (the unified verdict, #2584), so triggering a loop never runs a held loop and never bumps a held loop's cadence anchor. Loop control is `/loops` (`t3 loop enable`/`disable`/`pause`/`resume`) + the DB `LoopState` tier only — there is no env kill-switch. Each script-backed `Loop` row carries its OWN on-disk entry point `src/teatree/loops/<name>/loop.py` (the module exposing that loop's `MINI_LOOP`) — the `script` column is per-loop and load-bearing; there is no shared runner. The live tick reads each admitted row's column to decide what to dispatch, and the per-loop runner (`t3 loops tick --loop <name>`, which scopes `build_loop_table_jobs` to that one row — #2650) honours the SAME enabled / due / unified-verdict gates; a row whose `script` does not resolve to a real registered loop module raises loudly rather than silently running nothing. A prompt-backed loop runs its `Prompt` body as the per-tick instruction — `arch_review` is the one prompt-backed default, instructing a sub-agent to run an architectural review with the `ac-reviewing-codebase` skill — see `/t3:prompts`.

### Enabling / disabling a loop

```bash
t3 loop enable <name>     # turn a loop ON  — sets BOTH Loop.enabled=True AND the LoopState control tier to ENABLED
t3 loop disable <name>    # turn a loop OFF — sets BOTH Loop.enabled=False AND the LoopState kill-switch to DISABLED
t3 loop resume <name>     # alias of enable — lift either a pause or a disable, return the loop to running
t3 loop pause <name>      # reversible hold (LoopState only) — does NOT flip the durable Loop.enabled row
t3 loop loop-state <name> # read the durable LoopState status (ENABLED when never touched)
```

`enable`/`disable`/`resume` move the TWO planes the #2584 unified verdict reads in lock-step inside one transaction: the durable `LoopState` control tier (#1913) AND the row-level `Loop.enabled` column that the loop tick gates on (`not row.enabled` skips a loop). They are the agent-facing way to toggle `enabled`; the Django admin (`Loop` rows) remains the place to edit a loop's cadence and prompt-vs-script. `pause` is the reversible control-plane hold only — it leaves `Loop.enabled` untouched so a paused loop returns to running with `resume` without re-enabling a row that was deliberately `disable`d.

#### The toggle IS the whole job (#2650 / PR-28)

PR-28 retired the native Claude `/loop` cron mirror: the DB toggle is now the whole job. The enable/disable chokepoint runs the reconciler, which adds a `loop_timer` chain head for a newly-enabled loop and prunes the timers of a disabled one at once — so the worker starts/stops driving that loop with no `CronCreate`/`CronDelete` step. There is no `claude-spec` to read and no cron to register.

- **Enable a loop `X`:** `t3 loop enable X` — flips `Loop.enabled=True` + `LoopState=ENABLED`; the reconciler heads its timer chain and the worker drives it on its cadence.
- **Disable a loop `X`:** `t3 loop disable X` — flips `Loop.enabled=False` + `LoopState=DISABLED`; the reconciler prunes its queued timers.
- **Confirm the worker is running:** `t3 worker status` (the live flock holder + the resolved `loop_runner_enabled` + per-loop timer counts). If it is enabled but not running, `t3 worker ensure` spawns a detached worker. `loop_runner_enabled` OFF stops the loops entirely (the kill-switch).

### Presets & weekly schedules (mode switching, #3159)

`t3 loop <enable|disable|pause|resume>` are per-loop. **Presets** switch many loops at once as a read-time MASK above the base config and below a `LoopState` hold — no rows are rewritten on a switch. A preset's `entries` are **tri-state** per loop (`on` / `off` / *absent = inherit* the base `Loop.enabled`). Resolution order (first opinion wins): L4 `LoopState` hold → L3 manual override → L2 active-schedule slot → L1 `Loop.enabled`.

```bash
t3 loop preset list                      # every preset + the ACTIVE marker
t3 loop preset show                      # active preset + WHY + per-loop effective verdict (deciding layer)
t3 loop preset show heads-down           # a named preset's entries
t3 loop preset use heads-down            # activate until the next scheduled boundary
t3 loop preset use unattended --hold     # sticky until cleared; --for 2h / --until <iso> for a TTL
t3 loop preset auto                      # clear the override — the schedule decides again
t3 loop preset create|edit <name> --set review=off --set dispatch=on [--pin autonomous_away] [--scope <overlay>]
t3 loop preset delete <name>

t3 loop schedule list | show [<name>]    # the weekly calendars + their slots
t3 loop schedule set-active standard     # one write switches calendars (e.g. flip to a holiday one)
t3 loop schedule clear-active            # no L2 layer — presets apply only via a manual override
```

Seeded defaults (owner-editable DB data, never clobbered by re-seeding): presets `engaged` / `heads-down` / `unattended` (pins `autonomous_away`) / `maintenance` / `low-power` / `off`, and schedules `standard` / `always-unattended`. A fresh install seeds everything but leaves `active_loop_schedule` **unset** — fully opt-in, so with no active schedule and no override every loop admits exactly as its two-plane verdict does today. Everything fails OPEN: a deleted preset/loop/schedule resolves to base config with a WARNING + a `t3 doctor` finding — a broken schedule can never brick the fleet. A preset may pin an availability mode (written through the same `t3 teatree availability` override chokepoint) and a `focus:<overlay>` preset's `overlay_scope` restricts the tick to one backend. `low-power` auto-engages while a usage window is parked, behind the default-off `low_power_auto_engage` flag.

### Reactive infra loops (not DB `Loop` rows)

Three tight-cadence reactive slots run as their OWN dedicated native Claude `/loop`s, separate from the DB-configured domain loops above. They are self-contained cycle commands (not scanner ticks). The **t3-master** session AUTO-registers all three at session start — the owner bootstrap (`hooks/scripts/loop_registrations.py`) emits one `/loop <cadence> Run …` directive per slot, reading the same `teatree.loop.loop_cadences` seam that `t3 loop <slot> start` prints, so you can also register or re-print any single slot by hand. Their cadence is env-overridable:

```bash
t3 loop slack-answer start    # /loop 20s Run `t3 loop slack-answer run`.       (T3_SLACK_ANSWER_CADENCE, floor 15s)
t3 loop self-improve start    # /loop 30m Run `t3 loop self-improve run --tier cheap`.  (T3_SELF_IMPROVE_CHEAP_CADENCE)
t3 loop drain-queue start     # /loop 30s Run `t3 loop drain-queue run`.         (T3_QUEUE_DRAIN_CADENCE, floor 10s)
```

Each acquires its own dedicated `LoopLease` slot (`loop-slack-answer` / `loop-self-improve` / `loop-drain-queue`) so a slow cycle never blocks another, and each is the sub-minute-cadence reason these stay dedicated `/loop`s rather than DB `Loop` rows (the cron-based `Loop` registration is minute-granular). There is no master tick to piggyback them onto — each is driven only by its own `/loop`.

For ownership hand-off, claiming, the lease/owner machinery, and how the cron drives the tick, see `t3:teatree`.
