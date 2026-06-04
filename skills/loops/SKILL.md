---
name: loops
description: 'Show t3 loop status — which loops are running vs stalled, the cadence and next tick of each loop, and loop ownership. Use when the user says "which loops are running", "loop status", "loops", "loop health", "is the loop ticking".'
compatibility: any
triggers:
  priority: 50
  keywords:
    - '\b(which loops are running|loop status|loops|loop health|is the loop ticking)\b'
requires:
  - rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Loops — Live Loop Status

A SHORT, read-only view of every loop's live state. `/t3:loops` never ticks, claims, or starts work — it computes the status from the DB, prints it, and stops.

It exists because `t3 loop status` prints the *cached* statusline written at the last tick, so its countdowns go stale — it can still show a live-looking loop line while the loop has actually been dead for hours. `t3 loop list` recomputes the state live on every call.

## When to load

Load `/t3:loops` when the user wants to know the live loop state — phrasings like "which loops are running", "loop status", "loop health", "is the loop ticking".

Do NOT use it to start, claim, or advance a loop — that is `t3 loop claim` / `t3 loop tick` (see `t3:teatree`). This is a read-only glance.

## The single command

```bash
t3 loop list           # live loop status, computed from the DB
t3 loop list --json     # the same status as a machine-readable payload
```

## Output contract

- Two labeled sections in fixed order — **`infra slots:`** first, then **`mini-loops:`** — never merged.
- Each loop line is `<name>  <enabled|disabled>  cadence <dur>  last <age>  next <when>`; infra-slot lines append `held` or `idle`.
- `next` reads `overdue` when the next fire is in the past and `—` when the loop has never fired (`last` is also `—`).
- One **loop-owner** line: the owning session id, its `owner_pid`, whether that pid is `alive` or `dead/unknown`, and whether the claim is `live` or `stale`; `unclaimed` when no session holds it.
- A **STALL** line appears only when the most recent tick is older than twice the tick cadence: `STALLED — last tick <age> ago`, followed by a one-line remediation hint (register the `t3 loop tick` cron, or `t3 loop claim` to take ownership).
- No preamble, no "Here is the loop status."

For ownership hand-off, claiming, or how the loop is driven, see `t3:teatree`.
