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
