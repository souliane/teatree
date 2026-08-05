---
name: mode
description: The operating mode — one named posture (reachable / unattended / holiday) that decides whether `AskUserQuestion` asks the user now or captures a durable `DeferredQuestion` row, and which loops run. Use when switching mode for a holiday or an unattended run, configuring the weekly schedule, answering the deferred-question backlog, or debugging the mode resolver.
eval_exempt: thin chairside reference for the `t3 loop preset` and `t3 teatree questions` commands; behaviour is enforced by the PreToolUse posture hook and pinned by scenarios/askuserquestion_slack_resolution.yaml, not by this skill's prose
compatibility: any
metadata:
  version: 0.0.2
requires:
  - rules
---

# Mode — the single operating posture

There is ONE concept. A `Mode` row carries a per-loop mask AND the two booleans that
say whether the user is reachable; `resolve_active_mode()` resolves the active one and
every Django consumer reads it. The bare hooks read the same DB rows Django-free via
`teatree.config.cold_mode.resolve_cold_posture` — [#3826](https://github.com/souliane/teatree/issues/3826)
deleted the mirror file that used to stand between them and drifted a week out of date,
silently muting the owner.

The full spec lives in `BLUEPRINT.md` §5.6.3 + §17.1 invariant 9; this skill is a
chairside reference for the day-to-day commands.

## When to load

Load `/t3:mode` when the user wants to:

- See or change the active mode, or clear an override and return to the schedule.
- Configure the weekly schedule that picks a mode per time slot.
- Answer or dismiss the deferred-question backlog.
- Debug why the agent is or isn't intercepting `AskUserQuestion`.

## The posture — two booleans, three reachable points

- **`defers_questions`** — the user is unreachable NOW: `AskUserQuestion` captures a
  durable `DeferredQuestion`, local TTS is silenced, colleague-facing loops are gated
  off, and returning to a reachable mode drains the backlog.
- **`pauses_self_pump`** — stop self-driving too (the holiday case): `loops_tick` parks
  silently (`skipped: true`, no lease claimed). Requires `defers_questions`, so the
  nonsensical "pump paused but questions answered" point is unrepresentable.

The seeded modes carrying the three points are `engaged` (reachable), `unattended`
(defer questions, keep the factory running — the long-unattended-run state) and
`offline` (holiday: defer AND park). The names are operator-editable data: every
consumer selects a mode by its posture (`Mode.objects.by_posture`), never by literal.

## Resolution — a single deterministic precedence

`resolve_active_mode()` (Django) and `resolve_cold_posture()` (the bare hooks) walk the
same chain over the same rows:

1. **L3 manual override (unexpired)** — the `ModeOverride` row, set by
   `t3 loop preset use <mode>`. A deliberate posture is authoritative and is never
   overridden by a keystroke.
2. **Presence upgrade (upgrade-only)** — a `UserPromptSubmit` heartbeat within
   `PRESENCE_FRESHNESS` (15 min) is direct evidence the user is at the keyboard now, so
   it upgrades an away-class mode reached BY SCHEDULE OR DEFAULT to the configured
   `presence_upgrade_mode`. It never downgrades, and never touches a manual override.
3. **L2 active schedule slot** — the `active_loop_schedule` calendar's governing slot.
4. **L0 default** — the configured `default_mode` (`engaged` when unset).

Everything fails toward ASKING: an unreadable DB, a deleted mode, a malformed slot all
resolve to a reachable posture. Failing closed to the most restrictive posture is what
muted the owner for a week; a broken control plane must interrupt the user, not silence
them.

## CLI surface

```bash
# The active mode, the layer that decided it, its derived posture, and the
# per-loop verdict table.
t3 loop preset show

# Every mode, with the ACTIVE marker.
t3 loop preset list

# Holiday: defer questions AND park the self-pump, until a time or until cleared.
t3 loop preset use offline --until 2026-05-18T22:00:00+02:00
t3 loop preset use offline --hold

# Unattended run: defer questions but KEEP the factory running.
t3 loop preset use unattended

# Back to reachable. Coming back from a deferring mode auto-drains the backlog
# to the user's Slack DM.
t3 loop preset use engaged

# Clear the override; the schedule / default decides again (also drains).
t3 loop preset auto

# Read the deferred-question backlog.
t3 teatree questions list          # pending only
t3 teatree questions list --all    # include answered/dismissed

# Resolve one — writes a `DeferredQuestionAudit` row.
t3 teatree questions answer 42 "yes, ship it"
t3 teatree questions dismiss 42 --reason "stale"

# Manually re-post the pending backlog (idempotent; the return-to-reachable
# transition already auto-fires this same drain).
t3 teatree questions resurface
```

The dashboard's `/dash/loops/` header offers the same switch as one-click postures
(reachable / defer questions / pause everything / auto), routed through the same
`set_mode_override` chokepoint so the two surfaces cannot diverge.

## How the defer path works

When the posture defers and the agent calls `AskUserQuestion`, the
`handle_route_away_mode_question` PreToolUse hook first checks whether the current turn
is **user-driven**:

- **User-driven turn** (`is_live_user_turn` — a `UserPromptSubmit` for the same session
  within `LIVE_TURN_FRESHNESS` = 90 s): the question renders **in-client**, even under a
  manual override. No defer, no Slack mirror. This is the
  [#189](https://github.com/souliane/teatree/issues/189) escape that makes `/checking`
  work without a mode flip. It is intentionally far shorter than the 15-min
  `PRESENCE_FRESHNESS` used for the schedule upgrade.
- **Autonomous / loop-driven turn**: the hook defers. Invariant 9 holds — autonomous
  questions are always captured.

The defer path records a `DeferredQuestion` row, mirrors the question to the user's
Slack DM (idempotent by a stable hash of the payload + session; fail-open), emits
`permissionDecision=deny` naming the row id, and leaves the `tool_use` block in the
transcript so the §807 structured-question Stop gate sees it and the turn completes —
the deferral is a *sanctioned destination* for the same tool call, never a prose
fallback.

In a reachable mode the question renders in the client and the separate
`handle_mirror_question_to_slack` handler only ADDS the Slack DM.

## Returning to reachable — the drain (auto-fires)

Returning must never silently swallow questions, and it must not depend on the agent
remembering to run a command. `set_mode_override` / `clear_mode_override` — the single
override write chokepoint behind `t3 loop preset use` / `auto` and the dash switch —
read the prior posture before flipping, and when `defers_questions` goes T→F they
re-post every pending `DeferredQuestion` to the user's Slack DM. The drain only fires on
a real transition and is fully fail-open (a Slack failure never blocks the flip).

`t3 teatree questions resurface` is the manual, idempotent entry point to the SAME
`drain_deferred_questions` egress (the `BotPing` ledger dedupes per question), so
running it after the auto-drain never double-posts.

**Known gap:** the auto-drain hooks the override write, so a transition that happens
*without* one — a timed override lapsing, or a schedule slot boundary — does not
auto-drain; use `resurface` in those cases. A durable cross-tick transition detector
would close it, but it is net-new persistent state.

## Statusline

The anchors zone shows the active mode and, when questions are deferring, the backlog
depth — so the user sees both from any terminal consuming the statusline.

## Related

- BLUEPRINT.md §5.6.3 — the full spec.
- BLUEPRINT.md §17.1 invariant 9 — the every-user-directed-question-is-captured guarantee.
- `/t3:rules` § "Always Use AskUserQuestion for Questions" — the §807 gate this composes with.
- `/t3:health` — the preset/schedule editing surface on `/dash/presets/`.
