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

### 2. Auto-Enqueue the Next-Phase Task

**When:** the current task is an interactive phase task (`scoping`, `coding`, `testing`, `reviewing`, `shipping`) with a clear outcome that does **not** require further user input. Skip when the user hasn't confirmed the outcome, or when you'll set `needs_user_input: true` below. **Also skip when the work has already shipped** — the issue is closed, the PR is merged, and you ran the phase as a post-merge audit. Enqueueing a `reviewing` task for a closed ticket creates a no-op headless run; record the audit outcome in the structured result and stop.

**Why:** the `next_steps` JSON field is descriptive — the pipeline does NOT parse it to create follow-up tasks. Interactive task completion does NOT record a `TaskAttempt`, so `_advance_ticket()` never fires and the ticket is orphaned. The next pending task the worker picks up will be for a different ticket, and the just-completed phase stalls.

**What to do:** use `t3 <overlay> tasks create` to enqueue the next-phase Task as `HEADLESS` (so a worker claims it immediately). The `--reason` body is the prompt the headless worker will see — include the locked decision from this session and the concrete implementation task list.

```bash
t3 <overlay> tasks create <TICKET_PK> \
  --phase <next phase> \
  --reason-file <path-to-prompt>.md
```

For short prompts, pass `--reason "<text>"` directly. For multiline prompts, write them to a tempfile and use `--reason-file`, or pipe via `--reason -` (stdin).

**Phase transitions:** `scoping → coding`, `coding → testing`, `testing → reviewing`, `reviewing → shipping`.

**Gotcha:** the `<TICKET_PK>` argument takes the teatree **ticket PK** (visible as `ticket_id` in `t3 <overlay> tasks list`), NOT the external issue number — they differ. Confirm via the CLI output, not by guessing.

### 3. Emit Structured Result

Output a JSON block on the **last line** of the session. **When step 2 enqueued a next-phase task, include its `task_id` in `next_steps`** so the user can trace the handoff.

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

`next_steps` is a contract field for downstream tooling and the user. Each entry must describe action that will be taken (by a queued task, the next pipeline step, or the user explicitly). Items prefixed with "follow-up:" or "worth doing later" indicate the writer punted — these are not next steps, they are deferrals, and they belong fixed/bundled/asked-about, not listed.

### 4. Display Summary

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
- Do not manually advance the ticket **state** — the FSM transitions are owned by `_advance_ticket()`. Step 2 creates the next-phase **Task** (which is different) so the headless worker has something to claim.
- Do not skip step 2 with "the user can dispatch it from the dashboard." A session that locked a phase decision but left no follow-up Task orphans the ticket — the pipeline picks unrelated pending tasks instead.
- Do not list unaddressed session findings (operational notes, bugs raised, unfixed lapses) as `next_steps` items. `next_steps` describes legitimate forward-looking work the pipeline will do — NOT a dumping ground for things you noticed but didn't address. Apply `t3:rules § Do Work Now` to each finding before listing it: fix in this session, bundle into the next-phase task, or use `AskUserQuestion` to choose a lane. If a finding ends up in `next_steps`, it must be a deliberate handoff (e.g., "task #88 ships #23 + bundled proxy parity fix"), not a deferral.
