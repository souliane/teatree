---
name: availability
description: 24/7 dual question-mode — switch between asking the user now (present) and capturing questions as durable `DeferredQuestion` rows (away). Use when configuring work hours, switching to away-mode for a holiday, answering the deferred-question backlog, or debugging the availability resolver.
compatibility: any
metadata:
  version: 0.0.1
  subagent_safe: true
requires:
  - rules
---

# Availability — 24/7 Dual Question-Mode

Thin user-facing entrypoint for `t3 teatree availability ...` and `t3 teatree questions ...`. The full spec lives in `BLUEPRINT.md` §5.6.3 + §17.1 invariant 9; this skill is a chairside reference for the day-to-day commands.

## When to load

Load `/t3:availability` when the user wants to:

- See or change the current availability mode (`present` vs `away`).
- Configure the cron-window schedule under `[teatree.availability]`.
- Answer or dismiss the deferred-question backlog.
- Debug why the agent is or isn't intercepting `AskUserQuestion`.

## Mode resolution (single deterministic precedence)

`teatree.core.availability.resolve_mode()` returns the effective mode by:

1. **Manual override (unexpired)** — `t3 teatree availability away --until <ISO8601>` (or `present`).
2. **Cron-window schedule** — any active expression in `[teatree.availability].windows` (evaluated in `[teatree.availability].timezone`).
3. **Default** — `present`.

`t3 teatree availability auto` clears the override so the schedule decides again.

## CLI surface

```bash
# Show the effective mode and the layer that decided it.
t3 teatree availability show

# Force away-mode for the rest of the day (or forever).
t3 teatree availability away --until 2026-05-18T22:00:00+02:00
t3 teatree availability away

# Force present-mode (rare — typical use is to cancel an `away` override).
t3 teatree availability present

# Drop the override; let the cron schedule decide.
t3 teatree availability auto

# Read the deferred-question backlog.
t3 teatree questions list          # pending only
t3 teatree questions list --all    # include answered/dismissed

# Resolve one — writes a `DeferredQuestionAudit` row.
t3 teatree questions answer 42 "yes, ship it"
t3 teatree questions dismiss 42 --reason "stale"

# Re-post the pending backlog to the user's Slack DM (away→present drain).
t3 teatree questions resurface
```

## Example `~/.teatree.toml`

```toml
[teatree.availability]
timezone = "Europe/Paris"
# Work hours: 09:00–16:59 Mon-Fri. Outside this window → away-mode.
windows = ["* 9-16 * * 1-5"]
```

Multiple expressions OR together — any active = present.

## How away-mode works

When mode resolves to `away` and the agent calls `AskUserQuestion`, the `handle_route_away_mode_question` PreToolUse hook:

1. Records the question as a `DeferredQuestion` row (durable, single-use).
2. Mirrors the question text + option labels to the user's Slack DM (the user reads Slack, not the CLI). Idempotent by a stable hash of the question payload + session, so a harness retry does not double-post; fail-open, so a Slack/IO error never blocks the deny.
3. Emits `permissionDecision=deny` with a friendly reason naming the row id.
4. Lets the `tool_use` block stay in the transcript so the §807 structured-question Stop gate sees it and the turn completes — the away-mode path is a *sanctioned destination* for the same tool call, never a prose fallback.

The agent then proceeds with any work that does not depend on the answer. The user answers later via `t3 teatree questions answer <id> <text>`; the resolution writes a `DeferredQuestionAudit` row.

In **present** mode the question still renders in the client; the separate `handle_mirror_question_to_slack` PreToolUse handler only ADDS the Slack DM (it never denies), so the user sees it on their phone too.

## Returning from away — the drain

Returning from away must never silently swallow questions. `t3 teatree questions resurface` re-posts every pending `DeferredQuestion` to the user's Slack DM via the canonical `notify_user` egress — idempotent per question (the `BotPing` ledger dedupes), routed through the per-overlay bot, and fail-open (a delivery failure for one question is recorded on its `BotPing` row and never aborts the drain). Run it on the away→present transition (or on the first present-mode tick) so the backlog surfaces where the user reads it.

## Statusline

The anchors zone shows `mode=away · N queued` whenever mode is `away` and there are pending questions — so the user can see both the mode and the backlog depth from any terminal consuming the statusline.

## Related

- BLUEPRINT.md §5.6.3 — full Availability spec.
- BLUEPRINT.md §17.1 invariant 9 — the every-user-directed-question-is-captured guarantee.
- BLUEPRINT.md §17.3 C3 — Availability component charter.
- `/t3:rules` § "Always Use AskUserQuestion for Questions" — the §807 gate this composes with.
