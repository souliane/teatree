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

# Todos — The Current Session's Lists

A SHORT, read-only view of everything on the current harness session's plate. `/t3:todos` never starts work, never transitions a ticket, never posts — it prints the session's lists grouped by status and stops.

There are **two distinct stores**, and the command keeps them apart — never conflate them:

- **harness TODO** — the agent harness's own working list (the `TaskCreate` / `TaskUpdate` items, formerly `TodoWrite`). Ephemeral, harness-owned, not a teatree concept.
- **teatree task** — a row in teatree's DB-backed `Task` model: a claimable lifecycle work unit with a phase, lease, and ticket.

There is no such thing as a "teatree todo" — use **teatree task** for the DB model and **harness TODO** for the harness list.

## When to load

Load `/t3:todos` when the user wants to see what is on their list — phrasings like "task list", "todos", "what's on my list", "where is the task list", "my tasks".

## The single command

```bash
t3 <overlay> tasks list --session
```

`--session` scopes the list to the **current harness session** (resolved via `current_session_id()` — `CLAUDE_SESSION_ID`, the loop-session override, then the loop registry). It surfaces both stores in two clearly-labeled sections: the **harness TODO** list (read from the harness task store on disk) and the teatree `Task` rows persisted for the same session.

Drop `--session` for the unscoped teatree-tasks queue across every ticket; add `--status` / `--execution-target` to filter the teatree-tasks view.

**The harness task store is `~/.claude/tasks/<session-id>/*.json`** (override the root with the `CLAUDE_TASKS_DIR` env var) — per-session-UUID directories of JSON task files, queried directly when you need the raw store. It is **not** `~/.claude/todos.json` (no such file exists). A bare numeric id or a `TODO-<n>` id is a **harness task id**, not a forge issue — resolve it against this store (or via `t3 <overlay> tasks list`), never by querying the issue tracker (`gh issue view <n>` / `glab issue view <n>`).

## Output contract

- Two labeled sections, never merged: **`harness TODOs`** first, then **`teatree tasks`**. Each section header carries its total count; an empty section is omitted entirely.
- Within each section, groups in fixed order — `pending`, `in_progress`, `completed` — each with its own count; an empty group is omitted.
- A teatree-task line is `task TODO-<id> (ticket #<n> (<short title>) <phase>): <reason>`; a harness-TODO line is `todo: <text>`. The task id carries the `TODO-` prefix and the ticket carries a namespace-qualified `#<n>` plus its short title inline (#2092) so the reader knows *what* `#<n>` is — never a bare/title-less id — and the two id namespaces never collide when both are the same number (a harness/teatree **task id** is a different namespace from a **forge ticket id**). The inline title comes from the single `teatree.core.ref_render.render_ref` chokepoint every id-listing surface shares; a ticket with no known title degrades to the plain `#<n>` (no empty parens). See `/t3:rules` § "ID Namespace Disambiguation".
- `claimed` teatree tasks show under `in_progress`; `failed` show under `completed` (both states the user reads as "done with for now").
- No active harness session → one line saying so (an anonymous caller has no session-scoped list).
- Nothing in either store → one line: `No todos for this session.`
- No preamble, no "Here is your list."

Keep references clickable when surfacing them to the user — link a ticket/PR id, never paste a bare number.
