---
name: next
description: Wrap up the current session — retro, structured result, pipeline handoff.
requires:
  - retro
metadata:
  version: 0.0.1
---

# t3:next — Session Wrap-Up & Pipeline Handoff

Run this before ending any session. It captures lessons, reports what happened, and lets the pipeline schedule the next step.

## When to Run

- User says "done", "next", "wrap up", "finish"
- Session is about to end (context getting long, user switching tasks)
- Headless agent is about to output its final result

## Workflow

### 1. Run Retro

Load `/t3:retro` and execute it. This captures lessons while the full session context is still available. Do NOT skip this — it compounds learning.

### 2. Emit Structured Result

Output a JSON block on the **last line** of the session:

```json
{
  "summary": "one-line description of what was accomplished",
  "needs_user_input": false,
  "user_input_reason": "",
  "files_modified": [{"path": "src/foo.py", "action": "modified"}],
  "next_steps": ["run e2e tests", "update docs"]
}
```

Set `needs_user_input: true` if the next step requires human judgment. The system will create a new interactive task for it.

### 3. Display Summary

Before ending, show the user what happened:

```text
════════════════════════════════════════════════════════════════
  SESSION COMPLETE

  Summary: <what was done>
  Ticket:  <ticket number> — <current state> → <next state>
  Retro:   <N findings persisted>
  Next:    <what the pipeline will do next, or "pipeline complete">
════════════════════════════════════════════════════════════════
```

This is non-negotiable — the user must see what `/t3:next` did.

## What NOT to Do

- Do not skip the retro to save time
- Do not output the JSON block without running retro first
- Do not end without displaying the summary
- Do not manually advance the ticket state — the system does that via `_advance_ticket()`
