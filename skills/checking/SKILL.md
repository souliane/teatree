---
name: checking
description: The check-in surface — a SHORT "what did I miss" report, the session task/TODO lists, the pending deferred questions, and the daily follow-up routine (new tickets, ticket statuses, PR reminders). Use when the user says "what did I miss", "checking", "catch me up", "what changed since", "ask me the questions", "task list", "todos", "what's on my list", "my tasks", "followup", "check status", "advance tickets", or "PR reminders".
compatibility: any
requires:
  - workspace
  - rules
  - platforms
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Checking — the check-in surface

One skill for everything the user asks for when they check in: what changed while
they were away, what is on this session's plate, which questions are waiting, and
the daily follow-up routine over tickets and PRs.

## Catch-up — "what did I miss?"

A SHORT catch-up for when the user checks in while away during an autonomous loop. `/t3:checking` first prints a terse, grouped, clickable report of important changes since the user's last check (READ-ONLY — never starts work, never transitions a ticket, never posts), then advances per-overlay markers. After the report, it walks the user through the pending **deferred questions** one at a time — the user can answer them from right here, without flipping availability.

The user does NOT want a long report. Answer first; one idea per line.

## When to load

Load `/t3:checking` when the user wants a quick "what happened while I was away?" — phrasings like "what did I miss", "catch me up", "what changed since" — or when they want to answer the backlog from here ("ask me the questions", "shoot me the questions").

The catch-up report stays read-only. The ONLY write `/checking` performs is recording the user's own answers to deferred questions via `t3 teatree questions answer`.

## Answering the deferred questions (no availability flip)

After the report, if there are pending deferred questions, walk the user through them one at a time:

1. List them: `t3 teatree questions list` (pending only).
2. If the list is empty, say so in one line (`No pending questions.`) and stop — do not invent a walk-through.
3. For each pending question, in order, raise it with the `AskUserQuestion` tool (one question per call), using the stored question text and option labels. **Do NOT batch** — one decision per call, wait for the answer, then move to the next.
4. Record each answer immediately: `t3 teatree questions answer <id> "<the user's answer text>"`. If the user wants to skip one, `t3 teatree questions dismiss <id> --reason "<why>"`.
5. After the last one, confirm in one line how many were answered/dismissed.

**Why this renders live even when availability is `away`:** running `/checking` is a user-driven turn — the user just typed a prompt this session. The away-mode `AskUserQuestion` PreToolUse hook detects that fresh same-session prompt (`availability.PRESENCE.is_live_user_turn`, a short this-turn window) and lets the question render in-client instead of converting it to a new `DeferredQuestion`. Each in-client render slides that window forward (`availability.PRESENCE.refresh_live_turn`), so a multi-question walk-through keeps EVERY question live even across an intervening background task-notification turn (#2058). So the user answers the backlog in place, the persistent availability override is left UNCHANGED, and the loop's own autonomous questions keep deferring as before (BLUEPRINT §17.1 invariant 9). There is NO `t3 teatree availability present` flip — that is the whole point.

Do NOT use `/checking` to start work, advance a ticket, or post anything. The catch-up is a read-only glance; the only writes are the user's own deferred-question answers.

## The single command

```bash
t3 <overlay> checking show                 # report ALL overlays since their last check, advance each marker
t3 <overlay> checking show --this-overlay  # scope to the current overlay only (backward-compat)
t3 <overlay> checking show --since 2026-05-30T08:00:00   # explicit window start (does NOT advance the marker)
t3 <overlay> checking show --no-advance     # read without moving the last-checked markers
t3 <overlay> checking show --json           # structured payload instead of the terse view
```

The default path aggregates **all configured overlays** into one report. Each overlay's marker advances independently — only after the gather, so an immediate second run reports nothing. `--this-overlay` restores the old single-overlay scope. `--since` and `--no-advance` leave markers untouched.

## Output contract

- **All-overlays (default):** Header `Since <local HH:MM> · all overlays`. Overlay-scoped items carry an `[overlay]` tag in their detail so the reader sees provenance.
- **Single-overlay (`--this-overlay`):** Header `Since <local HH:MM> · <overlay>`.
- Groups in fixed order — `Merged`, `In-flight`, `Needs you`. Group header is the bare word; items are `-` indented, one idea per line.
- Every PR / issue / ticket reference is a markdown link `[label](url)` — never a bare numeric id.
- Each group caps at 5 items; beyond that, append `…and X more`.
- Empty groups are omitted. If everything is empty, say so in one line: `Nothing since <local time>.`
- No preamble, no "Here is your report."

## Sources (all read-only)

- **Merged** — `MergeAudit` joined to `MergeClear`, merged inside the window, overlay-scoped. URL prefers the exact stored `pr_urls`, else a host-aware builder.
- **In-flight** — latest `TicketTransition` per ticket in the window, plus completed background `TaskAttempt` runs.
- **Needs you** — pending `DeferredQuestion` rows (not window-bounded — an old pending question still needs you) plus failed `TaskAttempt` runs (the durable "blocked" proxy). `DeferredQuestion` is queried ONCE for the whole report (not once per overlay) so a pending question never appears more than once. An overlay opts into richer signals via `OverlayBase.get_checking_sources`.

Resist adding a dashboard. The terse text IS the surface.

## The session lists — todos

### Canonical glossary — TODO vs task (Non-Negotiable)

Two words, two stores. They are routinely conflated; keep them strictly apart in every identifier, docstring, log line, config key, and prose surface:

- **TODO** = the **harness** ("Claude") task list — the `TaskCreate` / `TaskList` / `TaskUpdate` tools (formerly `TodoWrite`). Live, in-memory, harness-owned. Never persisted by teatree.
- **task** = a teatree **`Task` DB row** — a claimable lifecycle work unit with a phase, lease, and ticket (read via the `mcp__teatree__task_list` MCP tool, or the `t3 <overlay> tasks list` CLI). DB-backed, teatree-owned.

There is **no such thing** as a "teatree todo". A loop unit, setting, or signal that acts on `Task` rows is named `task_*` / `task.*`, never `todo_*` / `todo.*` — e.g. the `task_sweep` scanner (`TaskSweepScanner`, settings `task_sweep_*`, signals `task.completion_detected` / `task.orphaned`, handler `task_completion`) reconciles teatree `Task` rows, **not** the harness TODO list. Cross-ref: `BLUEPRINT.md` § "Loop scanners" (`TaskSweepScanner` row).

A SHORT, read-only view of everything on the current harness session's plate. `/t3:checking` never starts work, never transitions a ticket, never posts — it prints the session's lists grouped by status and stops.

There are **two distinct stores**, and they are built two different ways — never conflate them:

- **harness TODO** — the agent harness's own working list (the `TaskCreate` / `TaskUpdate` items, formerly `TodoWrite`). This is the agent's **live, in-memory** session list. Build it **dynamically from the `TaskList` harness tool** — see below.
- **teatree task** — a row in teatree's DB-backed `Task` model: a claimable lifecycle work unit with a phase, lease, and ticket. Read it with the `mcp__teatree__task_list` MCP tool (structured JSON — no text parsing), or the `t3 <overlay> tasks list` CLI.

There is no such thing as a "teatree todo" — use **teatree task** for the DB model and **harness TODO** for the harness list.

### When to load

Load `/t3:checking` when the user wants to see what is on their list — phrasings like "task list", "todos", "what's on my list", "where is the task list", "my tasks".

### Build the harness TODO list DYNAMICALLY from the live `TaskList` tool (Non-Negotiable)

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

### Harness-TODO maintenance — reconcile the live list against the conversation EACH TURN (Non-Negotiable)

`/t3:checking` (above) *shows* the harness TODO list. This section is the other half: *keeping it clean and current*. The harness TODO list is a faithful, live view of everything still on your plate — or it is noise. It only stays faithful if **you, the in-session agent, maintain it**, because nothing else can.

**Why it has to be you, not a background job.** The Task tools (`TaskCreate` / `TaskUpdate` / `TaskList`) bypass `PreToolUse` / `PostToolUse` hooks (the same harness regression — `docs/claude-code-internals.md` § 9), so teatree has no hook that can write the live list and a background loop has no way to reach it. Only the in-session agent — you, holding those tools — can read and write it. There is no teatree-written mirror file to lean on (the old `<session>.todos` materialiser was removed as a stale mistake-source). The maintenance is an in-session discipline, full stop.

**The discipline — run it EVERY turn that touched the list's truth** (the user added or dropped an ask, you finished a step, you started a new one, or you are about to wrap up):

1. **Reconcile against the conversation.** Call `TaskList`, then read back over the conversation since you last reconciled. Every still-open ask the user made — and every concrete step you committed to — must have a TODO. Add the forgotten ones with `TaskCreate`. The list is a view of the *conversation's* open work, not just the work you happened to enqueue.
2. **Consolidate / dedupe.** Collapse duplicate or overlapping items into one with `TaskUpdate`. Three half-stated items for one piece of work is worse than one faithful item — it hides the real state. Merge them.
3. **Mark completed items done.** Every finished item moves to `completed` (`TaskUpdate`); the one you are actively on moves to `in_progress`. Never leave a done item sitting `pending`, and never leave a stale `in_progress` lingering after you moved on. A list where status lies is noise.

**Do X, never Y:**

```text
## do X — reconcile with your OWN tools each turn (the only path that reaches the live list):
TaskList()                                  # read the live list
TaskCreate(subject="<forgotten ask>")       # add what the conversation shows is still open
TaskUpdate(id=<n>, status="completed")      # mark finished work done
## never Y — do NOT expect a background job / CLI / hook to maintain it; none can reach the live list.
```

**Deterministic helper — the reconcile-checklist emitter.** Because teatree cannot write the live list, the strongest deterministic aid it can give is to *emit the checklist for you to apply*. Run it when you want the reconcile steps spelled out alongside this session's open teatree tasks (the loop-tracked work that may correspond to TODOs you should mark done):

```bash
t3 <overlay> tasks reconcile-checklist       # prints the reconcile/dedupe/complete steps + this session's open teatree tasks
```

It is a read-only emitter — it makes no reconciliation writes (it never creates, completes, or transitions a task for you; the live list is unreachable from a subprocess). The one write it shares with every `tasks` read is the standard stale-claim reaper, which only fails a task whose lease is already expired. You then apply each step with your own `TaskList` / `TaskUpdate` / `TaskCreate` tools; it makes sure you do not forget a step and surfaces the teatree tasks worth cross-checking.

**Wall / harness-support note.** Full automation — a background process that keeps the live harness TODO list reconciled without the in-session agent — is **not feasible today**: the Task tools bypass `PreToolUse`/`PostToolUse`, so neither a hook nor a `t3` subprocess can read or write the live, in-memory list. Only the in-session agent holding the tools can. The harness support that would unblock real automation is a hook event that fires on `TaskCreate`/`TaskUpdate`/`TaskList` (or a documented writable store the harness keeps in sync), comparable to the `TaskCreated` event teatree already rides for the fan-out skill-loading gate (#1488). Until that exists, this in-session discipline plus the `reconcile-checklist` emitter is the best feasible mechanism.

### The teatree-tasks half (a separate, DB-backed concern)

The teatree `Task` rows (the loop's claimable lifecycle work units) are a **separate** store — DB-backed, not your harness list. Prefer the `mcp__teatree__task_list` MCP tool for the overlay-wide queue — it returns structured JSON (filter by `status` / `phase` / `ticket` / `overlay`); read its fields directly, no CLI text parsing (`mcp__teatree__loop_stats` gives the per-status counts). Fall back to the CLI when the MCP server isn't connected:

```bash
t3 <overlay> tasks list                      # CLI fallback: the teatree-tasks queue across every ticket (MCP: mcp__teatree__task_list)
t3 <overlay> tasks list --session            # CLI-only: teatree tasks scoped to THIS harness session
```

`--session` scopes **only the teatree `Task` rows** to the current harness session (resolved via `current_session_id()` — `CLAUDE_SESSION_ID`, the loop-session override, then the loop registry) and is CLI-only — the MCP tool serves the overlay-wide queue, not the per-harness-session view. It renders a single `teatree tasks` section — it does **not** print a harness-TODO section, because the CLI cannot read your live harness list. Add `--status` / `--execution-target` to filter the teatree-tasks view.

When the user asks "show my task list", render both halves: the live harness TODOs (from `TaskList`) under a `harness TODOs` heading, then the teatree tasks (from `t3 ... tasks list --session`) under a `teatree tasks` heading.

### Resolving a single `TODO-<n>` id (not the same as listing)

Looking up *what one task id is* is a different operation from listing the live list. A bare numeric id or a `TODO-<n>` id is a **harness task id**, not a forge issue — resolve it against the harness task store (`~/.claude/tasks/<session-id>/*.json`, override the root with `CLAUDE_TASKS_DIR`) or via the `mcp__teatree__task_list` MCP tool (or the `t3 <overlay> tasks list` CLI), never by querying the issue tracker (`gh issue view <n>` / `glab issue view <n>`). That store is **not** `~/.claude/todos.json` (no such file exists). For the *live current list*, still use `TaskList` — the store read is only for resolving an individual id reference.

### Output contract

- Two labeled sections, never merged: **`harness TODOs`** (from the live `TaskList` tool) first, then **`teatree tasks`** (from the CLI). Each section header carries its total count; an empty section is omitted entirely.
- Within each section, groups in fixed order — `pending`, `in_progress`, `completed` — each with its own count; an empty group is omitted.
- A teatree-task line is `task TODO-<id> (ticket #<n> (<short title>) <phase>): <reason>`; a harness-TODO line is `todo: <text>`. The task id carries the `TODO-` prefix and the ticket carries a namespace-qualified `#<n>` plus its short title inline (#2092) so the reader knows *what* `#<n>` is — never a bare/title-less id — and the two id namespaces never collide when both are the same number (a harness/teatree **task id** is a different namespace from a **forge ticket id**). The inline title comes from the single `teatree.core.ref_render.render_ref` chokepoint every id-listing surface shares; a ticket with no known title degrades to the plain `#<n>` (no empty parens). See `/t3:rules` § "ID Namespace Disambiguation".
- `claimed` teatree tasks show under `in_progress`; `failed` show under `completed` (both states the user reads as "done with for now").
- No active harness session → one line saying so (an anonymous caller has no session-scoped list).
- Nothing in either store → one line: `No todos for this session.`
- No preamble, no "Here is your list."

Keep references clickable when surfacing them to the user — link a ticket/PR id, never paste a bare number.

## The daily follow-up routine

Fetch all "Not started" issues assigned to the current user, then implement each one sequentially using the full delivery cycle (code, test, push, PR) — the cycle itself is the single one documented in [`../t3:wip/SKILL.md`](../t3:wip/SKILL.md) § Workflow. Interactive followup runs that cycle in the main conversation; see § Rules for the sequential-only constraint and how it relates to the batch-mode singleton sub-agent exception.

### Periodic Mode

When invoked with `--periodic` (e.g., from a cron job or scheduler), run in **non-interactive mode**:

- **Report status — never start work.** Periodic mode reads ticket status and posts reminders; it does **not** create a worktree, check out a branch, provision an environment, or write code. The one command to read in-flight status — copy-paste, no narration:

  ```bash
  # Periodic status read — refresh the followup cache (PRs, pipelines, approvals, merges) in one pass.
  t3 <overlay> followup sync

  # Or the one-shot variant that also processes due PR reminders:
  t3 <overlay> daily
  ```

  Never follow a status read with `git worktree add`, `git checkout -b`, or `t3 <overlay> worktree provision`/`start` in periodic mode — those start work, which periodic mode must not do.

- **Skip ticket implementation** (§3–§8) — periodic mode only checks status, it never starts new work.
- **Automatic steps:** § 9 (ticket transitions), § 10 (status check), § 11 (PR review reminders).
- **No user confirmation** — execute safe actions (status checks, cache updates) silently. For actions that modify external state (transitions, chat posts), respect existing safeguards:
  - **Ticket transitions:** execute automatically (idempotent, gate-checked).
  - **PR reminders:** post automatically if `last_reminded` is >1 day ago. The daily cache prevents spamming.
- **Output a summary** to stdout (for cron email) and optionally post to a team chat channel (configured via the `T3_FOLLOWUP_CHANNEL` environment variable).

**Cron setup** (add to `crontab -e`):

```bash
## Run t3:followup every 2 hours during work hours (Mon-Fri, 9-18)
0 9-18/2 * * 1-5 <agent-cli-command> "/t3:checking --periodic" >> $T3_DATA_DIR/followup.log 2>&1
```

> **Note:** Replace `<agent-cli-command>` with the CLI invocation for your agent platform.

**Configuration:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `T3_FOLLOWUP_CHANNEL` | (none) | Team chat channel for periodic summaries. If unset, output to stdout only. |
| `T3_FOLLOWUP_INTERVAL` | `24h` | Minimum interval between PR reminders per PR. |

### Dependencies

- **t3:workspace** (required) — provides worktree creation, setup, and dev servers. **Load `/t3:workspace` now** if not already loaded.

### Platform Note

The workflow below is platform-neutral. Platform-specific recipes (CLI commands, API calls, GraphQL mutations) are in [`../platforms/references/`](../platforms/references/). The default implementation uses GitLab (`glab` CLI). **Project overlays can override** the tracker-specific parts via extension points (`ticket_update_external_tracker`, `ticket_get_mrs`, `ticket_check_deployed`). If your project uses GitHub Issues, Jira, Linear, or another tracker, implement these extension points in your project overlay.

### Workflow

#### 1. Detect Username

Prefer the `mcp__teatree__github_current_user` / `mcp__teatree__gitlab_current_user` MCP tool (the forge group registered for the active overlay) — it returns the authenticated handle directly, no text parsing. Fall back to the tracker CLI (`glab auth status`, `gh api user`) when the MCP server isn't connected, extracting the username field.

#### 2. Fetch "Not Started" Issues

Query the issue tracker for all open issues assigned to the user with "Not started" status. See your [issue tracker platform reference](../platforms/references/) § "List Issues by Label" for the CLI recipe.

Parse the response to extract: issue ID, project ID, title, URL, and project path.

#### 3. Present Confirmation Table

| # | IID | Project | Title | URL |
|---|-----|---------|-------|-----|
| 1 | 8166 | backend-repo | End of year allowance | ... |
| 2 | 8170 | frontend-repo | Fix address fields | ... |

**The project where the issue is filed does NOT determine where the implementation happens.** Tickets may require work in any combination of repos.

Ask the user to pick "All" or specific ticket numbers. Use the agent platform's native question UI if it has one; otherwise ask plainly in the conversation. **Never start without explicit user approval.**

#### 4. Pre-Fetch External Context

For each confirmed ticket:

**a.** Extract external links from the issue description (external tracker URLs, chat links, etc.)

**b.** Fetch each external page (Notion, Confluence, Linear, etc.) using the appropriate integration. Save the full content.

**c.** Download any embedded images from issues or external pages.

#### 5. Scope Analysis

For each confirmed ticket, analyze the external spec + issue description to determine:

- **Which repos need changes.** Common patterns:
  - New data field → backend + frontend + translations
  - New acceptance rule → backend verificators only
  - New UI element → frontend + translations + tenant config repo
  - Tooltip/label changes → translations + tenant config repo only
- **Primary repo** for worktree creation (most significant changes).
- **Whether backend is in scope** → load the project's backend convention references.

#### 6. Execute Tickets Sequentially

For each confirmed ticket, in order:

**a. Update ticket status** across the issue tracker and external tracker:

Move the issue label/status from "Not started" to "Doing" / "In progress". See your [issue tracker platform reference](../platforms/references/) § "Transition Logic" for the CLI recipe.

**External tracker — update status** (if configured by the project skill):

If the project uses an external tracker (e.g., Notion), update the ticket status to "In progress" there too. The project skill's references should document the tracker's data source ID, status options, and API calls. If no external tracker is configured, skip this step.

If not found in the external tracker, log a warning but continue — not all tickets have an entry.

**b. Run the full per-ticket delivery cycle.** The delivery cycle (intake → implementation → testing → delivery) is documented once in [`../t3:wip/SKILL.md`](../t3:wip/SKILL.md) § Workflow and is not restated here — followup runs the same cycle. The two skills differ only in how the cycle is hosted: batch mode delegates each ticket to a singleton delivery sub-agent (its § Rules "Singleton delivery sub-agent" exception), whereas interactive followup runs the cycle in the main conversation per the constraint in § Rules below.

**c. Report result** before moving to the next ticket:

```text
✓ #{IID} — {TITLE}
  PRs: !123 (backend-repo), !456 (frontend-repo)
```

**d. Proceed to next ticket.** Lessons from one ticket carry into the next.

#### 7. Summary

After all tickets are processed, present:

| Ticket | Status | PR URLs | Errors |
|--------|--------|---------|--------|
| #8166 | Done | !123, !456 | — |
| #8170 | Failed | — | Test failure in test_address.py |

#### 8. Post-Delivery

- For successful tickets: suggest `/t3:review-request` for batch review notifications.
- For failed tickets: report the phase, error, and worktree path so the user can investigate or switch to `/t3:debug`.

#### 8b. Auto-Discover In-Flight Tickets (Cache Bootstrap)

Before running transition checks (§9) or status check mode (§10), ensure `$T3_DATA_DIR/tickets/` is populated. If the cache is empty or missing tickets, **auto-discover from open PRs:**

1. **Detect repos.** List repos the user works in — scan `$T3_WORKSPACE_DIR` for known repo directories (e.g., `backend-api`, `frontend-app`), or use a configured repo list from the project overlay.

2. **List open PRs.** For each repo, list all open, non-draft PRs authored by the current user (see [issue tracker platform reference](../platforms/references/) § "List PRs").

3. **Extract ticket IID.** Parse the source branch name for the ticket number (first `\d+` match after the branch prefix). Skip PRs with no extractable ticket number.

4. **Bootstrap cache per ticket.** For each discovered ticket IID where `$T3_DATA_DIR/tickets/<iid>/` does not yet exist:
    - `mkdir -p $T3_DATA_DIR/tickets/<iid>/`
    - Fetch the issue's current label/status from the issue tracker CLI.
    - Write `status.json`: `{"label": "Process::Doing", "last_checked": "<ISO timestamp>", "discovered_from": "open_mrs", "mrs": ["<mr_url>"]}`
    - No review-message cache file is created: "review requested?" is resolved live via `t3 review-request check`/`discover` against the channel + the `ReviewRequestPost` DB row (#1084), never a JSON oracle.

5. **Merge, don't overwrite.** If the ticket directory already exists, only add newly discovered PR URLs to `status.json.mrs` — never overwrite existing cache data (review messages, transition history).

6. **Report discoveries.** Log each newly cached ticket: `Discovered ticket #<IID> (<status>) from <N> open PR(s)`.

This step is **idempotent** — running it multiple times only adds missing entries. It runs automatically before §9 and §10, so the user never needs to manually populate the cache.

#### 9. Check Ticket Transitions

After all tickets are processed (or when invoked in "check status" mode), scan in-flight tickets for possible status transitions. This covers tickets discovered by §8b as well as any ticket with prior state in `$T3_DATA_DIR/tickets/`.

Run the gate checks from [`references/ticket-transitions.md`](references/ticket-transitions.md) for each in-flight ticket:

- **Doing → Technical Review:** all PRs have review request messages cached
- **Technical Review → DEV Review:** all PRs merged AND deployed

Each transition updates the issue tracker and calls `ticket_update_external_tracker`. See the reference file for the full gate logic, storage format, and extension points.

#### 10. Status Check Mode

When invoked with "check status", "check tickets", or "advance tickets" (without batch-implementing):

0. Run §8b (auto-discover) to ensure the cache is populated from open PRs.
1. Scan `$T3_DATA_DIR/tickets/` for tickets with cached state.
2. For each, fetch current issue label/status to determine current state.
3. Run the appropriate gate check (§9a or §9b).
4. Present a summary table:

| Ticket | Current Status | Gate | Ready? | Action |
|--------|---------------|------|--------|--------|
| #8166 | Doing | All PRs reviewed? | Yes (2/2) | → Technical Review |
| #8170 | Technical Review | Merged + deployed? | Partial (merged, not deployed) | Waiting |

5. Ask user confirmation before executing transitions.

#### 11. PR Review Reminders

Daily nudge for PRs that were sent for review but haven't been approved yet. Designed for daily use — caches aggressively to avoid redundant API calls.

##### 11a. Discover Unapproved PRs

For each repo the user works in:

List all user's open PRs across repos, then filter to those that are **open**, **not draft**, **not yet approved**. See your [issue tracker platform reference](../platforms/references/) § "List PRs" and § "Check Approval Status" for CLI recipes.

Also check for colleague comments (exclude system notes and author's own) via the PR notes API.

**Cache PR metadata** in `$T3_DATA_DIR/mr_reminders.json` — see your [chat platform reference](../platforms/references/) § "PR Reminder Cache" for the format.

Populate `original_review_permalink` from the live channel: `t3 review-request check --mr-url <url>` returns the existing message `permalink` on `suppress` (or use the `review_permalink` field from `t3 review-request discover`). The live read is the source of truth — no JSON cache lookup (#1084).

**Skip PRs that:** have no original review message (never sent for review), were already reminded today (`last_reminded` == today), are already approved, **already have colleague comments** (being actively reviewed), or **have a non-success pipeline** (failed, running, pending — only send review requests for green pipelines).

##### 11b. Group by Channel and Present

Group remaining PRs by their review channel. Present the filtered list:

| # | Channel | PR | Pipeline | Title |
|---|---------|-----|----------|-------|
| 1 | #code-review | !123 | success | feat: add login |
| 2 | #code-review | !456 | success | fix: resolve timeout |

Do **not** ask for confirmation on each PR individually — the auto-filtering already removed ineligible PRs. Present the full list once and ask the user to confirm or exclude specific PRs. Then post all confirmed PRs as **draft messages** (one per PR or grouped per ticket if multiple PRs belong to the same ticket).

**Never post without explicit approval of the batch.**

##### 11c. Post Reminders

For each channel with PRs to remind:

1. **Post one message** to the channel: "Hey team, I have PRs waiting for review. Could you please have a look?"

2. **Reply in thread** — one message per PR, posting the **clean PR title as a link to the original review request** (not the PR URL). Strip feature flag tags (`[flag_name]`) and ticket URLs from the title. This keeps all discussion in the original review thread.

3. **Update cache:** set `last_reminded` to today's date in `$T3_DATA_DIR/mr_reminders.json`.

See your [chat platform reference](../platforms/references/) for known limitations (e.g., externally shared channels).

##### 11d. Cleanup

After posting (or during any follow-up invocation), remove entries from `mr_reminders.json` where the PR is now approved or merged. This keeps the cache file small.

#### 12. Data Sync (First Action on Every Load)

**Execute immediately when the skill is loaded** — before responding to the user, before asking what they want, before anything else. This is the first thing followup does on every invocation (both interactive and periodic). The user should never have to ask for a data refresh.

**Always use `t3 <overlay> followup sync`** (or `t3 <overlay> daily` as a one-shot shortcut that also processes reminders) to collect data. This command handles PR discovery, pipeline status, approvals, merge detection, and cache cleanup in one deterministic pass. Never manually call issue tracker APIs to build followup data — the CLI command is the single entry point.

The command discovers open PRs from the repos returned by the overlay's `get_followup_repos()` method. Overlays can return a static list or query the GitLab group API dynamically. The legacy `T3_FOLLOWUP_REPOS` env var is not read by the code — configure the overlay instead.

Internally the command:

1. Discovers open PRs across all configured repos.
2. Enriches each entry with pipeline status, approvals, and colleague comments.
3. Fetches issue labels for linked tickets.
4. Detects PRs merged since the last run and logs them.
5. Cleans review tracking entries for merged PRs.

**During long sessions:** Also re-run data sync after significant events (ticket completed, PR pushed, context switch) — don't wait for the next explicit `/t3:checking` invocation.

### `followup.json` Schema

See [`references/followup-schema.md`](references/followup-schema.md) for the full cache schema at `$T3_DATA_DIR/followup.json`.

### Rules

#### Interactive mode (default)

- **Sequential only.** Interactive followup runs each ticket's delivery cycle in the main conversation and does not delegate ticket implementation to sub-agents — the [`../rules/SKILL.md`](../rules/SKILL.md) § "Sub-Agent Limitations" default applies here. The batch-mode singleton delivery sub-agent (the explicit exception documented in that same section and in [`../t3:wip/SKILL.md`](../t3:wip/SKILL.md) § Rules) is the deliberate carve-out for unattended batch runs, not for interactive followup; the two are not in conflict because they apply to different hosting modes.
- **Never start without user approval.** Always show the confirmation table first.
- **Always pre-fetch external context.** Read all specs before starting implementation.
- **Always run scope analysis.** The issue tracker project ≠ the implementation repo.
- **`t3 <overlay> worktree provision` is mandatory for every ticket.** Never skip it (see `/t3:workspace` § Never Hand-Edit Generated Files).
- **Confirm before transitioning.** In status check mode, always present the table and wait for user approval before executing transitions.
- **Never post reminders without approval.** Always show the dry-run table first.

#### Both modes

- **Label transitions are best-effort.** If the API call fails, log a warning but continue.
- **Transition checks are idempotent.** Running them multiple times is safe — they only transition if the gate is satisfied and the ticket isn't already at the target status.
- **Resolve "review requested?" live, never from a JSON cache.** `t3 review-request check`/`discover` read the channel + the `ReviewRequestPost` DB row; a stale or deleted cache must never cause a duplicate post (#1084).
- **PR URLs stay hidden in reminders.** Post only the permalink to the original review request — this avoids leaking PR context outside the original thread.
- **One reminder per interval per PR.** The `last_reminded` cache prevents spamming. Interval is `T3_FOLLOWUP_INTERVAL` (default 24h).
- **Cache aggressively.** PR metadata, review request permalinks, and approval status are cached in `$T3_DATA_DIR/mr_reminders.json`. Only re-fetch what's stale.

#### Periodic mode only

- **Never implement tickets.** Periodic mode is read-only + status transitions + reminders. It never creates worktrees, writes code, or pushes.
- **Auto-execute safe actions.** Ticket transitions (idempotent, gate-checked) and PR reminders (interval-limited) proceed without confirmation.
- **Log everything.** Write a timestamped summary to `$T3_DATA_DIR/followup.log` for auditability.
- **Fail silently on auth issues.** If the forge CLI or chat integration is not authenticated, log the error and exit 0 — don't block the cron job.
