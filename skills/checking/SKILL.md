---
name: checking
description: A SHORT "what did I miss" report when the user checks in mid-loop — terse, grouped, clickable, read-only. Use when the user says "what did I miss", "checking", "catch me up", or "what changed since".
compatibility: any
triggers:
  priority: 50
  keywords:
    - '\b(what did i miss|checking|catch me up|what changed since|catch up)\b'
requires:
  - rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Checking — "What Did I Miss?"

A SHORT catch-up for when the user checks in while away during an autonomous loop. `/t3:checking` is **READ-ONLY** — it never starts work, never transitions a ticket, never posts. It prints a terse, grouped, clickable report of important changes since the user's last check, then advances a per-overlay marker so the next run picks up from here.

The user does NOT want a long report. Answer first; one idea per line.

## When to load

Load `/t3:checking` when the user wants a quick "what happened while I was away?" — phrasings like "what did I miss", "catch me up", "what changed since".

Do NOT use it to start work, advance a ticket, or post anything. It is a read-only glance.

## The single command

```bash
t3 <overlay> checking show                 # report changes since last check, then advance the marker
t3 <overlay> checking show --since 2026-05-30T08:00:00   # explicit window start (does NOT advance the marker)
t3 <overlay> checking show --no-advance     # read without moving the last-checked marker
t3 <overlay> checking show --json           # structured payload instead of the terse view
```

The marker advances only on the default path. `--since` and `--no-advance` are inspections and leave it untouched. Because the window is read **before** the marker advances, an immediate second run reports nothing rather than collapsing its own window.

## Output contract

- Header one line: `Since <local HH:MM> · <overlay>`.
- Groups in fixed order — `Merged`, `In-flight`, `Needs you`. Group header is the bare word; items are `-` indented, one idea per line.
- Every PR / issue / ticket reference is a markdown link `[label](url)` — never a bare numeric id.
- Each group caps at 5 items; beyond that, append `…and X more`.
- Empty groups are omitted. If everything is empty, say so in one line: `Nothing since <local time>.`
- No preamble, no "Here is your report."

## Sources (all read-only)

- **Merged** — `MergeAudit` joined to `MergeClear`, merged inside the window, overlay-scoped. URL prefers the exact stored `pr_urls`, else a host-aware builder.
- **In-flight** — latest `TicketTransition` per ticket in the window, plus completed background `TaskAttempt` runs.
- **Needs you** — pending `DeferredQuestion` rows (not window-bounded — an old pending question still needs you) plus failed `TaskAttempt` runs (the durable "blocked" proxy). An overlay opts into richer signals via `OverlayBase.get_checking_sources`.

Resist adding a dashboard. The terse text IS the surface.
