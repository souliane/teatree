---
name: handover
description: Use when the user wants to transfer an in-flight TeaTree task from Claude to another runtime, or asks whether it is time to switch because Claude usage is getting high.
requires:
  - rules
metadata:
  version: 0.0.1
---

# t3:handover

Use this when the user wants to switch away from Claude without losing the thread.

## Workflow

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
