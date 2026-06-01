---
name: todos
description: List the current session's tasks/todos — terse, grouped pending / in_progress / completed, with clickable refs. Use when the user says "task list", "todos", "what's on my list", "where is the task list", or "my tasks".
compatibility: any
triggers:
  priority: 50
  keywords:
    - '\b(task list|todos?|what''?s on my list|where is the task list|my tasks)\b'
requires:
  - rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Todos — The Current Session's Task List

A SHORT, read-only view of everything on the current Claude session's plate. `/t3:todos` never starts work, never transitions a ticket, never posts — it prints the session's tasks grouped by status and stops.

## When to load

Load `/t3:todos` when the user wants to see what is on their list — phrasings like "task list", "todos", "what's on my list", "where is the task list", "my tasks".

## The single command

```bash
t3 <overlay> tasks list --session
```

`--session` scopes the list to the **current Claude session** (resolved from `CLAUDE_SESSION_ID`). It sources from the teatree `Task` model — the durable, DB-backed tasks teatree persists per session — and merges the harness `TodoWrite` list captured for the same session, so one view covers both.

Drop `--session` for the unscoped queue across every ticket; add `--status` / `--execution-target` to filter either view.

## Output contract

- Groups in fixed order — `pending`, `in_progress`, `completed`. Each group header carries its count; an empty group is omitted.
- A task line is `task #<id> (ticket #<n> <phase>): <reason>`; a harness todo line is `todo: <text>`.
- `claimed` tasks show under `in_progress`; `failed` tasks show under `completed` (both terminal-or-active states the user reads as "done with for now").
- No active Claude session → one line saying so (an anonymous caller has no session-scoped list).
- Nothing on the list → one line: `No todos for this session.`
- No preamble, no "Here is your list."

Keep references clickable when surfacing them to the user — link a ticket/PR id, never paste a bare number.
