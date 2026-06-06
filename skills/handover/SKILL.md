---
name: handover
description: Use when the user wants to hand all current work from one Claude session to another (or to a not-yet-existing session) with a single command, or to transfer an in-flight TeaTree task from Claude to another runtime, or asks whether it is time to switch because Claude usage is getting high.
eval_exempt: describes a one-shot `t3 handover` command surface; produces no recurring multi-step agent trajectory to grade
compatibility: any
triggers:
  priority: 50
  keywords:
    - '\b(hand ?over|hand off this session|hand off the session|hand this off|pick up the handover|take over this session)\b'
requires:
  - rules
metadata:
  version: 0.0.2
  subagent_safe: false
---

# t3:handover

Two distinct hand-offs share this skill:

- **Session → session**: move all of THIS Claude session's in-flight work to another session (or to "whatever session starts next") with a single command, zero copy-paste. The receiving session picks it up automatically on start.
- **Claude → another runtime**: switch away from Claude (e.g. to Codex) without losing the thread, gated on five-hour usage telemetry.

## Session → session hand-off

The hand-off payload is the SAME durable-state snapshot the PreCompact hook builds (active tickets, worktree paths/branches, in-flight sub-agent ids+tasks, open PRs, approach/decisions, failing tests, loaded skills, loop-owner status). A `SessionHandover` DB row is the source of truth; an XDG file (`[teatree] handover_mirror_path`, default `${XDG_STATE_HOME:-~/.local/state}/teatree/handover/latest.md`) mirrors it for human-readability and brand-new-session bootstrap.

### Hand off this session's work

```bash
t3 <overlay> handover create            # hand to the LIVE loop owner; if none, park for the next session
t3 <overlay> handover create --to <id>  # hand to a specific session id
```

No `--to` resolves the target to the live `loop-owner` slot holder; if there is no live owner the hand-off is parked for whichever session starts next to claim. The row is always persisted AND mirrored to the XDG file.

### Know your own session id

A session needs its own id to be a `--to` target. Either:

```bash
t3 loop whoami            # prints THIS session's id
t3 loop owner             # prints "you are: <id>" plus who owns the loop
t3 <overlay> handover whoami
```

### Takeover (automatic, zero copy-paste)

A fresh / non-owner session claims an unclaimed hand-off (targeted at it, or parked for "next session") on `SessionStart` and injects the payload as `additionalContext` — no command needed. The claim is marked once so it injects exactly once. `t3 <overlay> handover claim-on-start --session <id>` is the hook entry point; you do not normally run it by hand.

## Claude → another runtime

Use this when the user wants to switch away from Claude without losing the thread.

### 1. Check Claude usage

Run:

```bash
t3 tool claude-handover --json --current-runtime <runtime>
```

Read these fields from the JSON:

- `current_runtime`
- `preferred_runtime`
- `recommended_runtime`
- `five_hour_used_percentage`
- `should_handover`
- `five_hour_resets_at`

Tell the user the current five-hour usage, which runtime currently has priority, and whether TeaTree recommends switching now.

### 2. Ask before switching

If the user did not explicitly request an immediate switch, ask one short question:

- continue on Claude
- switch now

Do not switch runtimes silently.

### 3. Prepare the handover bundle

Before ending the Claude session, produce a compact handover brief with:

- current goal
- exact repo and branch
- files already changed
- tests already run and their results
- open blockers or unanswered questions
- the next concrete action for the new runtime

Prefer a plain markdown summary in the conversation. If the user asks for a file, write a short markdown handoff note in the repo root or `artifacts/`.

### 4. Make the next runtime efficient

When handing off to Codex or another runtime:

- include the handover brief verbatim
- include the exact command or test that should be run first
- say explicitly that Claude session IDs are not portable across runtimes
- point to the latest TeaTree telemetry if relevant

## Rules

- Do not claim another runtime can resume a Claude session directly.
- Do not hide the recommendation threshold from the user.
- If telemetry is missing, say so and fall back to a manual summary-based handover.
