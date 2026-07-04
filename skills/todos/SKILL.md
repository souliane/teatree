---
name: todos
description: List the current session's tasks/todos — terse, grouped pending / in_progress / completed, with clickable refs. Use when the user says "task list", "todos", "what's on my list", "where is the task list", or "my tasks".
compatibility: any
requires:
  - rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Todos — The Current Session's Lists

## Canonical glossary — TODO vs task (Non-Negotiable)

Two words, two stores. They are routinely conflated; keep them strictly apart in every identifier, docstring, log line, config key, and prose surface:

- **TODO** = the **harness** ("Claude") task list — the `TaskCreate` / `TaskList` / `TaskUpdate` tools (formerly `TodoWrite`). Live, in-memory, harness-owned. Never persisted by teatree.
- **task** = a teatree **`Task` DB row** — a claimable lifecycle work unit with a phase, lease, and ticket (`t3 <overlay> tasks list`). DB-backed, teatree-owned.

There is **no such thing** as a "teatree todo". A loop unit, setting, or signal that acts on `Task` rows is named `task_*` / `task.*`, never `todo_*` / `todo.*` — e.g. the `task_sweep` scanner (`TaskSweepScanner`, settings `task_sweep_*`, signals `task.completion_detected` / `task.orphaned`, handler `task_completion`) reconciles teatree `Task` rows, **not** the harness TODO list. Cross-ref: `BLUEPRINT.md` § "Loop scanners" (`TaskSweepScanner` row).

A SHORT, read-only view of everything on the current harness session's plate. `/t3:todos` never starts work, never transitions a ticket, never posts — it prints the session's lists grouped by status and stops.

There are **two distinct stores**, and they are built two different ways — never conflate them:

- **harness TODO** — the agent harness's own working list (the `TaskCreate` / `TaskUpdate` items, formerly `TodoWrite`). This is the agent's **live, in-memory** session list. Build it **dynamically from the `TaskList` harness tool** — see below.
- **teatree task** — a row in teatree's DB-backed `Task` model: a claimable lifecycle work unit with a phase, lease, and ticket. Read it with the `t3 <overlay> tasks list` CLI.

There is no such thing as a "teatree todo" — use **teatree task** for the DB model and **harness TODO** for the harness list.

## When to load

Load `/t3:todos` when the user wants to see what is on their list — phrasings like "task list", "todos", "what's on my list", "where is the task list", "my tasks".

## Build the harness TODO list DYNAMICALLY from the live `TaskList` tool (Non-Negotiable)

The harness TODO list lives **only** in the harness's live, in-memory session state. The Task tools (`TaskCreate` / `TaskUpdate` / `TaskList`) bypass `PreToolUse` / `PostToolUse` hooks (a known harness regression — see `docs/claude-code-internals.md` § 9), so teatree has **no hook keeping a copy in sync**. A `t3` CLI subprocess therefore cannot read your live harness list — at best it reads a stale on-disk snapshot (`~/.claude/tasks/<session>/*.json`) that lags the live session and is never reliably in sync. That stale-disk read is exactly why an older `t3 ... tasks list --session` view was always out of date.

So, to show the harness TODO list, **call the live `TaskList` harness tool yourself and render its result** — do X, never Y:

1. **Do** issue the harness `TaskList` tool call (the live, in-memory list). It returns the current session's tasks with `id`, `subject`, `status`, `owner`, `blockedBy`. Render them grouped by status (`pending`, `in_progress`, `completed`). To narrow, pass the `status` filter the tool accepts.

   ```text
   # the ONE tool call that reads the LIVE session list — render its result grouped by status:
   TaskList()                  # all items; or TaskList(status="in_progress") to narrow
   ```

2. **Never** shell out to a `t3` CLI to render the live harness list. The CLI runs in a subprocess that cannot see your in-memory `TaskList` — it can only read the lagging disk snapshot, so its output is the stale list the user is complaining about.

   ```text
   # FORBIDDEN as the source of the live session list — it reads a stale on-disk snapshot:
   t3 <overlay> tasks list --session
   ```

If you genuinely cannot sync a file, the list must be built dynamically — and you can: `TaskList` gives you the live list directly. There is no excuse to render the stale store.

## Harness-TODO maintenance — reconcile the live list against the conversation EACH TURN (Non-Negotiable)

`/t3:todos` (above) *shows* the harness TODO list. This section is the other half: *keeping it clean and current*. The harness TODO list is a faithful, live view of everything still on your plate — or it is noise. It only stays faithful if **you, the in-session agent, maintain it**, because nothing else can.

**Why it has to be you, not a background job.** The Task tools (`TaskCreate` / `TaskUpdate` / `TaskList`) bypass `PreToolUse` / `PostToolUse` hooks (the same harness regression — `docs/claude-code-internals.md` § 9), so teatree has no hook that can write the live list and a background loop has no way to reach it. Only the in-session agent — you, holding those tools — can read and write it. There is no teatree-written mirror file to lean on (the old `<session>.todos` materialiser was removed as a stale mistake-source). The maintenance is an in-session discipline, full stop.

**The discipline — run it EVERY turn that touched the list's truth** (the user added or dropped an ask, you finished a step, you started a new one, or you are about to wrap up):

1. **Reconcile against the conversation.** Call `TaskList`, then read back over the conversation since you last reconciled. Every still-open ask the user made — and every concrete step you committed to — must have a TODO. Add the forgotten ones with `TaskCreate`. The list is a view of the *conversation's* open work, not just the work you happened to enqueue.
2. **Consolidate / dedupe.** Collapse duplicate or overlapping items into one with `TaskUpdate`. Three half-stated items for one piece of work is worse than one faithful item — it hides the real state. Merge them.
3. **Mark completed items done.** Every finished item moves to `completed` (`TaskUpdate`); the one you are actively on moves to `in_progress`. Never leave a done item sitting `pending`, and never leave a stale `in_progress` lingering after you moved on. A list where status lies is noise.

**Do X, never Y:**

```text
# do X — reconcile with your OWN tools each turn (the only path that reaches the live list):
TaskList()                                  # read the live list
TaskCreate(subject="<forgotten ask>")       # add what the conversation shows is still open
TaskUpdate(id=<n>, status="completed")      # mark finished work done
# never Y — do NOT expect a background job / CLI / hook to maintain it; none can reach the live list.
```

**Deterministic helper — the reconcile-checklist emitter.** Because teatree cannot write the live list, the strongest deterministic aid it can give is to *emit the checklist for you to apply*. Run it when you want the reconcile steps spelled out alongside this session's open teatree tasks (the loop-tracked work that may correspond to TODOs you should mark done):

```bash
t3 <overlay> tasks reconcile-checklist       # prints the reconcile/dedupe/complete steps + this session's open teatree tasks
```

It is a read-only emitter — it makes no reconciliation writes (it never creates, completes, or transitions a task for you; the live list is unreachable from a subprocess). The one write it shares with every `tasks` read is the standard stale-claim reaper, which only fails a task whose lease is already expired. You then apply each step with your own `TaskList` / `TaskUpdate` / `TaskCreate` tools; it makes sure you do not forget a step and surfaces the teatree tasks worth cross-checking.

**Wall / harness-support note.** Full automation — a background process that keeps the live harness TODO list reconciled without the in-session agent — is **not feasible today**: the Task tools bypass `PreToolUse`/`PostToolUse`, so neither a hook nor a `t3` subprocess can read or write the live, in-memory list. Only the in-session agent holding the tools can. The harness support that would unblock real automation is a hook event that fires on `TaskCreate`/`TaskUpdate`/`TaskList` (or a documented writable store the harness keeps in sync), comparable to the `TaskCreated` event teatree already rides for the fan-out skill-loading gate (#1488). Until that exists, this in-session discipline plus the `reconcile-checklist` emitter is the best feasible mechanism.

## The teatree-tasks half (a separate, DB-backed concern)

The teatree `Task` rows (the loop's claimable lifecycle work units) are a **separate** store — DB-backed, not your harness list — read with the CLI:

```bash
t3 <overlay> tasks list                      # the teatree-tasks queue across every ticket
t3 <overlay> tasks list --session            # teatree tasks scoped to THIS harness session
```

`--session` scopes **only the teatree `Task` rows** to the current harness session (resolved via `current_session_id()` — `CLAUDE_SESSION_ID`, the loop-session override, then the loop registry). It renders a single `teatree tasks` section — it does **not** print a harness-TODO section, because the CLI cannot read your live harness list. Add `--status` / `--execution-target` to filter the teatree-tasks view.

When the user asks "show my task list", render both halves: the live harness TODOs (from `TaskList`) under a `harness TODOs` heading, then the teatree tasks (from `t3 ... tasks list --session`) under a `teatree tasks` heading.

## Resolving a single `TODO-<n>` id (not the same as listing)

Looking up *what one task id is* is a different operation from listing the live list. A bare numeric id or a `TODO-<n>` id is a **harness task id**, not a forge issue — resolve it against the harness task store (`~/.claude/tasks/<session-id>/*.json`, override the root with `CLAUDE_TASKS_DIR`) or via `t3 <overlay> tasks list`, never by querying the issue tracker (`gh issue view <n>` / `glab issue view <n>`). That store is **not** `~/.claude/todos.json` (no such file exists). For the *live current list*, still use `TaskList` — the store read is only for resolving an individual id reference.

## Output contract

- Two labeled sections, never merged: **`harness TODOs`** (from the live `TaskList` tool) first, then **`teatree tasks`** (from the CLI). Each section header carries its total count; an empty section is omitted entirely.
- Within each section, groups in fixed order — `pending`, `in_progress`, `completed` — each with its own count; an empty group is omitted.
- A teatree-task line is `task TODO-<id> (ticket #<n> (<short title>) <phase>): <reason>`; a harness-TODO line is `todo: <text>`. The task id carries the `TODO-` prefix and the ticket carries a namespace-qualified `#<n>` plus its short title inline (#2092) so the reader knows *what* `#<n>` is — never a bare/title-less id — and the two id namespaces never collide when both are the same number (a harness/teatree **task id** is a different namespace from a **forge ticket id**). The inline title comes from the single `teatree.core.ref_render.render_ref` chokepoint every id-listing surface shares; a ticket with no known title degrades to the plain `#<n>` (no empty parens). See `/t3:rules` § "ID Namespace Disambiguation".
- `claimed` teatree tasks show under `in_progress`; `failed` show under `completed` (both states the user reads as "done with for now").
- No active harness session → one line saying so (an anonymous caller has no session-scoped list).
- Nothing in either store → one line: `No todos for this session.`
- No preamble, no "Here is your list."

Keep references clickable when surfacing them to the user — link a ticket/PR id, never paste a bare number.
