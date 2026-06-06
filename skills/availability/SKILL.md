---
name: availability
description: 24/7 dual question-mode — switch between asking the user now (present) and capturing questions as durable `DeferredQuestion` rows (away). Use when configuring work hours, switching to away-mode for a holiday, answering the deferred-question backlog, or debugging the availability resolver.
eval_exempt: thin chairside reference for the `t3 teatree availability` and `t3 teatree questions` commands; behaviour is enforced by the PreToolUse availability hook and pinned by scenarios/askuserquestion_slack_resolution.yaml, not by this skill's prose
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

1. **Manual override (unexpired)** — `t3 teatree availability away --until <ISO8601>` (or `present`). A deliberate `away` (a holiday) is authoritative and wins over everything below.
2. **Live presence beats a schedule `away`** — a `UserPromptSubmit` recorded within the last 15 min (`availability.PRESENCE_FRESHNESS`) is direct evidence the user is at the keyboard *now*, so it upgrades a schedule-derived `away` to `present` (source `live`). A user actively typing outside their configured work hours is demonstrably present — their questions must reach them, not get deferred. This layer only *upgrades* a schedule `away`; it never downgrades a present schedule and never overrides a manual `away`.
3. **Cron-window schedule** — any active expression in `[teatree.availability].windows` (evaluated in `[teatree.availability].timezone`).
4. **Default** — `present`.

`t3 teatree availability auto` clears the override so the schedule (and live presence) decide again. The heartbeat is stamped by the `handle_record_presence` `UserPromptSubmit` hook on every prompt; it is fail-open — a heartbeat that cannot be written just lets the schedule decide as before.

## CLI surface

```bash
# Show the effective mode and the layer that decided it.
t3 teatree availability show

# Force away-mode for the rest of the day (or forever).
t3 teatree availability away --until 2026-05-18T22:00:00+02:00
t3 teatree availability away

# Force present-mode (cancel an `away` override). On an away→present
# transition this auto-drains the deferred backlog to the user's Slack DM.
t3 teatree availability present

# Drop the override; let the cron schedule decide.
t3 teatree availability auto

# Read the deferred-question backlog.
t3 teatree questions list          # pending only
t3 teatree questions list --all    # include answered/dismissed

# Resolve one — writes a `DeferredQuestionAudit` row.
t3 teatree questions answer 42 "yes, ship it"
t3 teatree questions dismiss 42 --reason "stale"

# Manually re-post the pending backlog (idempotent; the away→present
# transition already auto-fires this same drain).
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

When mode resolves to `away` and the agent calls `AskUserQuestion`, the `handle_route_away_mode_question` PreToolUse hook first checks whether the current turn is **user-driven**:

- **User-driven turn** (`is_live_user_turn` returns True — a `UserPromptSubmit` for the same session within `LIVE_TURN_FRESHNESS` = 90 s): the hook passes through and the question renders **in-client**, even under manual-away. No defer, no Slack mirror. This is the #189 escape that makes `/checking` work without an availability flip. `LIVE_TURN_FRESHNESS` is intentionally short (seconds), distinct from the 15-min `PRESENCE_FRESHNESS` used for the schedule-upgrade layer.
- **Autonomous / loop-driven turn** (no recent same-session prompt): the hook defers as described below. Invariant 9 holds — autonomous questions are always captured.

For the **defer path** (autonomous turns or no live-turn signal):

1. Records the question as a `DeferredQuestion` row (durable, single-use).
2. Mirrors the question text + option labels to the user's Slack DM. Idempotent by a stable hash of the question payload + session, so a harness retry does not double-post; fail-open, so a Slack/IO error never blocks the deny.
3. Emits `permissionDecision=deny` with a friendly reason naming the row id.
4. Lets the `tool_use` block stay in the transcript so the §807 structured-question Stop gate sees it and the turn completes — the away-mode path is a *sanctioned destination* for the same tool call, never a prose fallback.

The agent then proceeds with any work that does not depend on the answer. The user answers later via `t3 teatree questions answer <id> <text>`; the resolution writes a `DeferredQuestionAudit` row.

In **present** mode the question still renders in the client; the separate `handle_mirror_question_to_slack` PreToolUse handler only ADDS the Slack DM (it never denies), so the user sees it on their phone too.

## Returning from away — the drain (auto-fires)

Returning from away must never silently swallow questions, and it must not depend on the agent remembering to run a command. The away→present transition therefore **auto-drains** the backlog: `write_override(MODE_PRESENT)` — the function behind `t3 teatree availability present` — reads the prior effective mode before flipping, and when it was `away` it re-posts every pending `DeferredQuestion` to the user's Slack DM. The drain only fires on a real transition (setting present while already present is a no-op, so no spurious re-asks) and is fully fail-open (a Slack failure is swallowed and never blocks the availability flip).

`t3 teatree questions resurface` is the manual / idempotent entry point to the **same** `teatree.core.notify.drain_deferred_questions` egress — idempotent per question (the `BotPing` ledger dedupes the `resurface-deferred-question-<pk>` key), routed through the per-overlay bot, fail-open. Because both paths share one code path, running it after the auto-drain never double-posts.

**Known gap:** the auto-drain hooks `write_override`, so it covers the explicit `t3 teatree availability present` flip. A transition that happens *without* writing an override — a timed `away --until` override lapsing, or a cron window opening — does not auto-drain; surface the backlog with a manual `resurface` (or its idempotent re-run on a present-mode tick) in those cases. A durable cross-tick transition detector (drain whenever the effective mode is present and the last-seen mode was away) would close this, but it is net-new persistent state, not a cheap add-on, so it is deferred.

## Statusline

The anchors zone shows `mode=away · N queued` whenever mode is `away` and there are pending questions — so the user can see both the mode and the backlog depth from any terminal consuming the statusline.

## Related

- BLUEPRINT.md §5.6.3 — full Availability spec.
- BLUEPRINT.md §17.1 invariant 9 — the every-user-directed-question-is-captured guarantee.
- BLUEPRINT.md §17.3 C3 — Availability component charter.
- `/t3:rules` § "Always Use AskUserQuestion for Questions" — the §807 gate this composes with.
