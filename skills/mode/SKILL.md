---
name: mode
description: The operating mode — one of five named presets (present / afk / maintenance / token-outage / off) deciding which loops run. Use when switching mode for a holiday or an unattended run, configuring the weekly schedule, answering the deferred-question backlog, or debugging the mode resolver.
eval_exempt: thin chairside reference for the `t3 loop preset` and `t3 teatree questions` commands; behaviour is enforced by the PreToolUse question hook and pinned by scenarios/askuserquestion_slack_resolution.yaml, not by this skill's prose
compatibility: any
metadata:
  version: 0.0.3
requires:
  - rules
---

# Mode — the single operating posture

There is ONE concept. A `Mode` row is a pure per-loop on/off table; `resolve_active_mode()`
resolves the active one and every consumer reads it.
[#4202](https://github.com/souliane/teatree/issues/4202) deleted the three intrinsic
posture booleans that survived the availability merge, so what a mode MEANS is exactly
which loops it admits — there is no second axis to drift from the mask.

The full spec lives in `BLUEPRINT.md` §5.6.3 + §17.1 invariant 9; this skill is a
chairside reference for the day-to-day commands.

## When to load

Load `/t3:mode` when the user wants to:

- See or change the active mode, or clear an override and return to the schedule.
- Configure the weekly schedule that picks a mode per time slot.
- Answer or dismiss the deferred-question backlog.
- Debug why the agent is or isn't intercepting `AskUserQuestion`.

## The five presets

| preset | what it admits |
|---|---|
| `present` | everything — deliver, interact, keep the improvement loops warm |
| `afk` | the daily job with NO colleague interaction: implement and take intake, but review no colleague's MR and post nothing on the owner's behalf (`egress = "forbid"`) |
| `maintenance` | self-repair only: keep the box healthy, drain the local queue, heal CI and back up |
| `token-outage` | only the loops that call no model — the token-budget guard |
| `off` | completely off; the switch reports what it strands, because nothing reaps stacks and nothing answers Slack |

The postures are a CHAIN — `present` >= `afk` >= `maintenance` >= `off` — so stepping
down one can only ever remove work. `token-outage` sits outside it: it is defined by a
PROPERTY (no model call), not by a position, so a new AI loop lands outside it by
construction rather than by someone remembering to add it.

Every preset is TOTAL — it holds a `true`/`false` opinion on EVERY live loop, so what a
preset does is readable from the preset alone, and a loop added later is written `false`
into every one of them. A stored `offline` override / slot / setting migrates to `off`.

The names are operator-editable data, but an override naming a mode no row carries is
REFUSED rather than written — a dangling name would silently fall open to base config.

## Resolution — a single deterministic precedence

1. **A `LoopState` hold** — the emergency brake (`t3 loop pause` / `disable`). A held
   loop never runs, whatever anything below says.
2. **The manual override** — `Loop.enabled` as a TRI-STATE (`None` = no opinion, the
   normal state). It carries the REQUIRED reason it was set, nothing auto-clears it, and
   `t3 loop override <name> clear` is the only thing that lifts it.
3. **The manual preset override** — the `ModeOverride` row, set by
   `t3 loop preset use <mode>`. This is how an operator pins `off` or `token-outage`.
4. **The active schedule slot** — the `active_loop_schedule` calendar's governing slot.
5. **The configured `default_mode`** (`present` when unset).

A keystroke decides nothing here. Presence used to upgrade a schedule- or default-reached
mode, and it was the one arm that flipped with no observable event — raised by typing,
lowered by the mere absence of it — so a decision persisted under one side of it could
not be kept correct. It is still read for live-turn QUESTION routing; it no longer
decides which loops run.

Everything fails OPEN: an unreadable DB, a deleted mode, a malformed slot all resolve to
a mode that admits every loop, because a resolution that could not answer at all must not
mask the whole fleet.
Failing closed to the most restrictive posture is what muted the owner for a week; a
broken control plane must interrupt the user, not silence them.

## CLI surface

The deferred-question verbs below have MCP twins — prefer `mcp__teatree__question_list` (the bare pending backlog) and `mcp__teatree__question_answer`; the block is the CLI fallback for a session whose MCP server isn't connected, and `--all` has no MCP filter.

```bash
# The active mode, the layer that decided it, and the per-loop verdict table.
t3 loop preset show

# Every mode, with the ACTIVE marker.
t3 loop preset list

# Hard hold — every work loop off (a holiday, or "nothing runs today").
t3 loop preset use off --hold

# Drain-only — finish and merge what is in flight, take no new intake.
t3 loop preset use maintenance --until 2026-05-18T22:00:00+02:00
t3 loop preset use maintenance --hold

# Unattended run: keep taking new work, colleague-facing loop off.
t3 loop preset use away

# Back to the full table.
t3 loop preset use present

# Clear the override; the schedule / default decides again.
t3 loop preset auto

# Read the deferred-question backlog.
t3 teatree questions list          # pending only
t3 teatree questions list --all    # include answered/dismissed

# Resolve one — writes a `DeferredQuestionAudit` row.
t3 teatree questions answer 42 "yes, ship it"
t3 teatree questions dismiss 42 --reason "stale"

# Re-post the pending backlog to the user's Slack DM (idempotent).
t3 teatree questions resurface
```

The dashboard's `/dash/loops/` header offers the same switch, routed through the same
`set_mode_override` chokepoint so the two surfaces cannot diverge.

## How the question path works

Questions are never buffered on a mode — they are asked immediately
([#4045](https://github.com/souliane/teatree/issues/4045)). The
`handle_mirror_question_to_slack` PreToolUse hook checks whether the current turn is
**user-driven**:

- **User-driven turn** (`is_live_user_turn` — a `UserPromptSubmit` for the same session
  within `LIVE_TURN_FRESHNESS` = 90 s): the question renders **in-client** and goes
  nowhere else — it is not posted to Slack and not recorded ([#4673](https://github.com/souliane/teatree/issues/4673):
  a recorded row with no Slack ts is exactly what the tick drain re-posts). This is the
  [#189](https://github.com/souliane/teatree/issues/189) escape that makes `/checking`
  work without a mode flip. It is intentionally far shorter than the 15-min
  `PRESENCE_FRESHNESS` used for the schedule upgrade.
- **Autonomous / loop-driven turn**: the hook records a `DeferredQuestion`, kicks its
  delivery to the user's Slack DM, and emits `permissionDecision=deny` naming the row id — a
  suspended autonomous session has no path to receive a Slack reply in-band. Invariant 9
  holds: autonomous questions are always captured.

Either way the `tool_use` block stays in the transcript, so the §807 structured-question
Stop gate sees it and the turn completes — the deferral is a *sanctioned destination* for
the same tool call, never a prose fallback.

## Statusline

The anchors zone shows the active mode and the pending-question backlog depth — so the
user sees both from any terminal consuming the statusline.

## Related

- BLUEPRINT.md §5.6.3 — the full spec.
- BLUEPRINT.md §17.1 invariant 9 — the every-user-directed-question-is-captured guarantee.
- `/t3:rules` § "Always Use AskUserQuestion for Questions" — the §807 gate this composes with.
- `/t3:health` — the preset/schedule editing surface on `/dash/presets/`.
