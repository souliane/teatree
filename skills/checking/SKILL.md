---
name: checking
description: A SHORT "what did I miss" report when the user checks in mid-loop — terse, grouped, clickable; then answer the pending deferred questions in-band. Use when the user says "what did I miss", "checking", "catch me up", "what changed since", or "ask me the questions / shoot me the questions".
compatibility: any
triggers:
  priority: 50
  keywords:
    - '\b(what did i miss|checking|catch me up|what changed since|catch up)\b'
    - '\b(ask|shoot|hit) me (the |my )?(deferred |pending )?questions?\b'
requires:
  - rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Checking — "What Did I Miss?" + Answer the Pending Questions

A SHORT catch-up for when the user checks in while away during an autonomous loop. `/t3:checking` first prints a terse, grouped, clickable report of important changes since the user's last check (READ-ONLY — never starts work, never transitions a ticket, never posts), then advances per-overlay markers. After the report, it walks the user through the pending **deferred questions** one at a time — the user can answer them from right here, without flipping availability.

The user does NOT want a long report. Answer first; one idea per line.

## When to load

Load `/t3:checking` when the user wants a quick "what happened while I was away?" — phrasings like "what did I miss", "catch me up", "what changed since" — or when they want to answer the backlog from here ("ask me the questions", "shoot me the questions").

The catch-up report stays read-only. The ONLY write `/checking` performs is recording the user's own answers to deferred questions via `t3 teatree questions answer`.

## Answering the deferred questions (no availability flip)

After the report, if there are pending deferred questions, walk the user through them one at a time:

1. List them: `t3 teatree questions list` (pending only).
2. If the list is empty, say so in one line (`No pending questions.`) and stop — do not invent a walk-through.
3. For each pending question, in order, raise it with the `AskUserQuestion` tool (one question per call), using the stored question text and option labels. **Do NOT batch** — one decision per call, wait for the answer, then move to the next.
4. Record each answer immediately: `t3 teatree questions answer <id> "<the user's answer text>"`. If the user wants to skip one, `t3 teatree questions dismiss <id> --reason "<why>"`.
5. After the last one, confirm in one line how many were answered/dismissed.

**Why this renders live even when availability is `away`:** running `/checking` is a user-driven turn — the user just typed a prompt this session. The away-mode `AskUserQuestion` PreToolUse hook detects that fresh same-session prompt (`availability.PRESENCE.is_live_user_turn`, a short this-turn window) and lets the question render in-client instead of converting it to a new `DeferredQuestion`. Each in-client render slides that window forward (`availability.PRESENCE.refresh_live_turn`), so a multi-question walk-through keeps EVERY question live even across an intervening background task-notification turn (#2058). So the user answers the backlog in place, the persistent availability override is left UNCHANGED, and the loop's own autonomous questions keep deferring as before (BLUEPRINT §17.1 invariant 9). There is NO `t3 teatree availability present` flip — that is the whole point.

Do NOT use `/checking` to start work, advance a ticket, or post anything. The catch-up is a read-only glance; the only writes are the user's own deferred-question answers.

## The single command

```bash
t3 <overlay> checking show                 # report ALL overlays since their last check, advance each marker
t3 <overlay> checking show --this-overlay  # scope to the current overlay only (backward-compat)
t3 <overlay> checking show --since 2026-05-30T08:00:00   # explicit window start (does NOT advance the marker)
t3 <overlay> checking show --no-advance     # read without moving the last-checked markers
t3 <overlay> checking show --json           # structured payload instead of the terse view
```

The default path aggregates **all configured overlays** into one report. Each overlay's marker advances independently — only after the gather, so an immediate second run reports nothing. `--this-overlay` restores the old single-overlay scope. `--since` and `--no-advance` leave markers untouched.

## Output contract

- **All-overlays (default):** Header `Since <local HH:MM> · all overlays`. Overlay-scoped items carry an `[overlay]` tag in their detail so the reader sees provenance.
- **Single-overlay (`--this-overlay`):** Header `Since <local HH:MM> · <overlay>`.
- Groups in fixed order — `Merged`, `In-flight`, `Needs you`. Group header is the bare word; items are `-` indented, one idea per line.
- Every PR / issue / ticket reference is a markdown link `[label](url)` — never a bare numeric id.
- Each group caps at 5 items; beyond that, append `…and X more`.
- Empty groups are omitted. If everything is empty, say so in one line: `Nothing since <local time>.`
- No preamble, no "Here is your report."

## Sources (all read-only)

- **Merged** — `MergeAudit` joined to `MergeClear`, merged inside the window, overlay-scoped. URL prefers the exact stored `pr_urls`, else a host-aware builder.
- **In-flight** — latest `TicketTransition` per ticket in the window, plus completed background `TaskAttempt` runs.
- **Needs you** — pending `DeferredQuestion` rows (not window-bounded — an old pending question still needs you) plus failed `TaskAttempt` runs (the durable "blocked" proxy). `DeferredQuestion` is queried ONCE for the whole report (not once per overlay) so a pending question never appears more than once. An overlay opts into richer signals via `OverlayBase.get_checking_sources`.

Resist adding a dashboard. The terse text IS the surface.
