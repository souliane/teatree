---
name: followup
description: Daily follow-up — batch process new tickets, check/advance ticket statuses, remind about MRs waiting for review. Use when user says "followup", "batch tickets", "check status", "advance tickets", "MR reminders", or wants a daily routine check.
compatibility: macOS/Linux, zsh, git, issue tracker CLI (glab, gh, etc.).
requires:
  - workspace
  - rules
  - platforms
triggers:
  priority: 110
  keywords:
    - '\b(follow.?up|autopilot|batch tickets?|process all tickets|not started issues?|work on all my tickets|check (ticket )?status|advance tickets?|remind reviewers?|mr reminders?|nudge)\b'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Follow-Up — Daily Routine & Batch Processing

Fetch all "Not started" issues assigned to the current user, then implement each one sequentially in the main conversation using the full delivery cycle (code, test, push, MR). See § Rules for the sequential-only constraint.

## Periodic Mode

When invoked with `--periodic` (e.g., from a cron job or scheduler), run in **non-interactive mode**:

- **Skip ticket implementation** (§3–§8) — periodic mode only checks status, it never starts new work.
- **Run automatically:** § 9 (ticket transitions), § 10 (status check), § 11 (MR review reminders).
- **No user confirmation** — execute safe actions (status checks, cache updates) silently. For actions that modify external state (transitions, chat posts), respect existing safeguards:
  - **Ticket transitions:** execute automatically (idempotent, gate-checked).
  - **MR reminders:** post automatically if `last_reminded` is >1 day ago. The daily cache prevents spamming.
- **Output a summary** to stdout (for cron email) and optionally post to a team chat channel (configured via `T3_FOLLOWUP_CHANNEL` in `~/.teatree`).

**Cron setup** (add to `crontab -e`):

```bash
# Run t3:followup every 2 hours during work hours (Mon-Fri, 9-18)
0 9-18/2 * * 1-5 <agent-cli-command> "/t3:followup --periodic" >> $T3_DATA_DIR/followup.log 2>&1
```

> **Note:** Replace `<agent-cli-command>` with the CLI invocation for your agent platform.

**Configuration:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `T3_FOLLOWUP_CHANNEL` | (none) | Team chat channel for periodic summaries. If unset, output to stdout only. |
| `T3_FOLLOWUP_INTERVAL` | `24h` | Minimum interval between MR reminders per MR. |

## Dependencies

- **t3:workspace** (required) — provides worktree creation, setup, and dev servers. **Load `/t3:workspace` now** if not already loaded.

## Platform Note

The workflow below is platform-neutral. Platform-specific recipes (CLI commands, API calls, GraphQL mutations) are in [`../platforms/references/`](../platforms/references/). The default implementation uses GitLab (`glab` CLI). **Project overlays can override** the tracker-specific parts via extension points (`ticket_update_external_tracker`, `ticket_get_mrs`, `ticket_check_deployed`). If your project uses GitHub Issues, Jira, Linear, or another tracker, implement these extension points in your project overlay.

## Workflow

### 1. Detect Username

Parse the authenticated username from the issue tracker CLI (e.g., `glab auth status`, `gh api user`). Extract the username field.

### 2. Fetch "Not Started" Issues

Query the issue tracker for all open issues assigned to the user with "Not started" status. See your [issue tracker platform reference](../platforms/references/) § "List Issues by Label" for the CLI recipe.

Parse the response to extract: issue ID, project ID, title, URL, and project path.

### 3. Present Confirmation Table

| # | IID | Project | Title | URL |
|---|-----|---------|-------|-----|
| 1 | 8166 | backend-repo | End of year allowance | ... |
| 2 | 8170 | frontend-repo | Fix address fields | ... |

**The project where the issue is filed does NOT determine where the implementation happens.** Tickets may require work in any combination of repos.

Ask the user to pick "All" or specific ticket numbers. Use the agent platform's native question UI if it has one; otherwise ask plainly in the conversation. **Never start without explicit user approval.**

### 4. Pre-Fetch External Context

For each confirmed ticket:

**a.** Extract external links from the issue description (external tracker URLs, chat links, etc.)

**b.** Fetch each external page (Notion, Confluence, Linear, etc.) using the appropriate integration. Save the full content.

**c.** Download any embedded images from issues or external pages.

### 5. Scope Analysis

For each confirmed ticket, analyze the external spec + issue description to determine:

- **Which repos need changes.** Common patterns:
  - New data field → backend + frontend + translations
  - New acceptance rule → backend verificators only
  - New UI element → frontend + translations + external-configuration
  - Tooltip/label changes → translations + external-configuration only
- **Primary repo** for worktree creation (most significant changes).
- **Whether backend is in scope** → load the project's backend convention references.

### 6. Execute Tickets Sequentially

For each confirmed ticket, in order:

**a. Update ticket status** across the issue tracker and external tracker:

Move the issue label/status from "Not started" to "Doing" / "In progress". See your [issue tracker platform reference](../platforms/references/) § "Transition Logic" for the CLI recipe.

**External tracker — update status** (if configured by the project skill):

If the project uses an external tracker (e.g., Notion), update the ticket status to "In progress" there too. The project skill's references should document the tracker's data source ID, status options, and API calls. If no external tracker is configured, skip this step.

If not found in the external tracker, log a warning but continue — not all tickets have an entry.

**b. Run the full lifecycle** using the loaded skills — each phase uses the corresponding lifecycle skill:

1. **Intake** (`/t3:ticket`): Fetch issue, create worktree (`t3 <overlay> workspace ticket`), run `t3 <overlay> worktree provision`, verify environment.
2. **Implementation** (`/t3:code`): Implement with TDD. Check ALL repos in scope.
3. **Testing** (`/t3:test`): Run tests, fix failures, ensure lint passes.
4. **Delivery** (`/t3:ship`): Commit, push, create MRs for ALL repos with changes.

**c. Report result** before moving to the next ticket:

```text
✓ #{IID} — {TITLE}
  MRs: !123 (backend-repo), !456 (frontend-repo)
```

**d. Proceed to next ticket.** Lessons from one ticket carry into the next.

### 7. Summary

After all tickets are processed, present:

| Ticket | Status | MR URLs | Errors |
|--------|--------|---------|--------|
| #8166 | Done | !123, !456 | — |
| #8170 | Failed | — | Test failure in test_address.py |

### 8. Post-Delivery

- For successful tickets: suggest `/t3:review-request` for batch review notifications.
- For failed tickets: report the phase, error, and worktree path so the user can investigate or switch to `/t3:debug`.

### 8b. Auto-Discover In-Flight Tickets (Cache Bootstrap)

Before running transition checks (§9) or status check mode (§10), ensure `$T3_DATA_DIR/tickets/` is populated. If the cache is empty or missing tickets, **auto-discover from open MRs:**

1. **Detect repos.** List repos the user works in — scan `$T3_WORKSPACE_DIR` for known repo directories (e.g., `backend-api`, `frontend-app`), or use a configured repo list from the project overlay.

2. **List open MRs.** For each repo, list all open, non-draft MRs authored by the current user (see [issue tracker platform reference](../platforms/references/) § "List MRs").

3. **Extract ticket IID.** Parse the source branch name for the ticket number (first `\d+` match after the branch prefix). Skip MRs with no extractable ticket number.

4. **Bootstrap cache per ticket.** For each discovered ticket IID where `$T3_DATA_DIR/tickets/<iid>/` does not yet exist:
    - `mkdir -p $T3_DATA_DIR/tickets/<iid>/`
    - Fetch the issue's current label/status from the issue tracker CLI.
    - Write `status.json`: `{"label": "Process::Doing", "last_checked": "<ISO timestamp>", "discovered_from": "open_mrs", "mrs": ["<mr_url>"]}`
    - Initialize empty `mr_review_messages.json` (`{}`).

5. **Merge, don't overwrite.** If the ticket directory already exists, only add newly discovered MR URLs to `status.json.mrs` — never overwrite existing cache data (review messages, transition history).

6. **Report discoveries.** Log each newly cached ticket: `Discovered ticket #<IID> (<status>) from <N> open MR(s)`.

This step is **idempotent** — running it multiple times only adds missing entries. It runs automatically before §9 and §10, so the user never needs to manually populate the cache.

### 9. Check Ticket Transitions

After all tickets are processed (or when invoked in "check status" mode), scan in-flight tickets for possible status transitions. This covers tickets discovered by §8b as well as any ticket with prior state in `$T3_DATA_DIR/tickets/`.

**a. Doing → Technical Review:**

For each ticket with `Process::Doing`:

1. List all MRs for the ticket's branch (via `ticket_get_mrs` extension point or the issue tracker CLI).
2. Check `$T3_DATA_DIR/tickets/<iid>/mr_review_messages.json` for cached review request messages. See your [chat platform reference](../platforms/references/) § "Caching Chat Data".
3. For MRs without a cache entry, search the team chat for the MR URL. Cache any results found.
4. If ALL MRs have a review request message → transition the ticket.

**b. Technical Review → DEV Review:**

For each ticket with `Process::Technical Review`:

1. Check if ALL associated MRs are merged (query MR state via the issue tracker CLI).
2. Call `ticket_check_deployed` extension point to verify deployment to target environment.
3. If all merged AND deployed → transition the ticket.

**Transition actions** (for both a and b):

- Update issue tracker label/status. See your [issue tracker platform reference](../platforms/references/) § "Transition Logic" for the CLI recipe.
- Call `ticket_update_external_tracker` extension point (Notion, Jira, etc.).
- Report: `Ticket #<IID> → <new status> (reason)`

See [`references/ticket-transitions.md`](references/ticket-transitions.md) for the full transition system.

### 10. Status Check Mode

When invoked with "check status", "check tickets", or "advance tickets" (without batch-implementing):

0. Run §8b (auto-discover) to ensure the cache is populated from open MRs.
1. Scan `$T3_DATA_DIR/tickets/` for tickets with cached state.
2. For each, fetch current issue label/status to determine current state.
3. Run the appropriate gate check (§9a or §9b).
4. Present a summary table:

| Ticket | Current Status | Gate | Ready? | Action |
|--------|---------------|------|--------|--------|
| #8166 | Doing | All MRs reviewed? | Yes (2/2) | → Technical Review |
| #8170 | Technical Review | Merged + deployed? | Partial (merged, not deployed) | Waiting |

5. Ask user confirmation before executing transitions.

### 11. MR Review Reminders

Daily nudge for MRs that were sent for review but haven't been approved yet. Designed for daily use — caches aggressively to avoid redundant API calls.

#### 11a. Discover Unapproved MRs

For each repo the user works in:

List all user's open MRs across repos, then filter to those that are **open**, **not draft**, **not yet approved**. See your [issue tracker platform reference](../platforms/references/) § "List MRs" and § "Check Approval Status" for CLI recipes.

Also check for colleague comments (exclude system notes and author's own) via the MR notes API.

**Cache MR metadata** in `$T3_DATA_DIR/mr_reminders.json` — see your [chat platform reference](../platforms/references/) § "MR Reminder Cache" for the format.

Populate `original_review_permalink` from `$T3_DATA_DIR/tickets/<iid>/mr_review_messages.json`. If not cached there, search the team chat for the MR URL and cache the result.

**Skip MRs that:** have no original review message (never sent for review), were already reminded today (`last_reminded` == today), are already approved, **already have colleague comments** (being actively reviewed), or **have a non-success pipeline** (failed, running, pending — only send review requests for green pipelines).

#### 11b. Group by Channel and Present

Group remaining MRs by their review channel. Present the filtered list:

| # | Channel | MR | Pipeline | Title |
|---|---------|-----|----------|-------|
| 1 | #code-review | !123 | success | feat: add login |
| 2 | #code-review | !456 | success | fix: resolve timeout |

Do **not** ask for confirmation on each MR individually — the auto-filtering already removed ineligible MRs. Present the full list once and ask the user to confirm or exclude specific MRs. Then post all confirmed MRs as **draft messages** (one per MR or grouped per ticket if multiple MRs belong to the same ticket).

**Never post without explicit approval of the batch.**

#### 11c. Post Reminders

For each channel with MRs to remind:

1. **Post one message** to the channel: "Hey team, I have MRs waiting for review. Could you please have a look?"

2. **Reply in thread** — one message per MR, posting the **clean MR title as a link to the original review request** (not the MR URL). Strip feature flag tags (`[flag_name]`) and ticket URLs from the title. This keeps all discussion in the original review thread.

3. **Update cache:** set `last_reminded` to today's date in `$T3_DATA_DIR/mr_reminders.json`.

See your [chat platform reference](../platforms/references/) for known limitations (e.g., externally shared channels).

#### 11d. Cleanup

After posting (or during any follow-up invocation), remove entries from `mr_reminders.json` where the MR is now approved or merged. This keeps the cache file small.

### 12. Dashboard

The dashboard is a Django view served by teatree. Start it with `t3 dashboard` (top-level command, no overlay needed). It provides a live overview of all in-flight work, reading from the DB populated by `t3 <overlay> followup sync`.

**Extension point: `followup_enrich_data`** — project overlays can add project-specific fields to followup entries (e.g., Notion status, tenant info).

### 13. Data Sync (First Action on Every Load)

**Execute immediately when the skill is loaded** — before responding to the user, before asking what they want, before anything else. This is the first thing followup does on every invocation (both interactive and periodic). The user should never have to ask for a data refresh.

**Always use `t3 <overlay> followup sync`** (or `t3 <overlay> daily` as a one-shot shortcut that also processes reminders) to collect data. This command handles MR discovery, pipeline status, approvals, merge detection, and cache cleanup in one deterministic pass. Never manually call issue tracker APIs to build followup data — the CLI command is the single entry point.

The command discovers open MRs from the repos returned by the overlay's `get_followup_repos()` method. Overlays can return a static list or query the GitLab group API dynamically. The legacy `T3_FOLLOWUP_REPOS` env var is not read by the code — configure the overlay instead.

Internally the command:

1. Discovers open MRs across all configured repos.
2. Enriches each MR with pipeline status, approvals, and colleague comments.
3. Fetches issue labels for linked tickets.
4. Detects MRs merged since the last run and logs them.
5. Cleans review tracking entries for merged MRs.

**During long sessions:** Also re-run data sync after significant events (ticket completed, MR pushed, context switch) — don't wait for the next explicit `/t3:followup` invocation.

## `followup.json` Schema

See [`references/followup-schema.md`](references/followup-schema.md) for the full cache schema at `$T3_DATA_DIR/followup.json`.

## Rules

### Interactive mode (default)

- **Sequential only.** Never use sub-agents for ticket implementation. See [`../rules/SKILL.md`](../rules/SKILL.md) § "Sub-Agent Limitations".
- **Never start without user approval.** Always show the confirmation table first.
- **Always pre-fetch external context.** Read all specs before starting implementation.
- **Always run scope analysis.** The issue tracker project ≠ the implementation repo.
- **`t3 <overlay> worktree provision` is mandatory for every ticket.** Never skip it (see `/t3:workspace` § Never Hand-Edit Generated Files).
- **Confirm before transitioning.** In status check mode, always present the table and wait for user approval before executing transitions.
- **Never post reminders without approval.** Always show the dry-run table first.

### Both modes

- **Label transitions are best-effort.** If the API call fails, log a warning but continue.
- **Transition checks are idempotent.** Running them multiple times is safe — they only transition if the gate is satisfied and the ticket isn't already at the target status.
- **Always cache chat search results.** Write to `$T3_DATA_DIR/tickets/<iid>/mr_review_messages.json` after every review channel search to avoid redundant API calls.
- **Never expose MR URLs in reminders.** Post only the permalink to the original review request — this avoids leaking MR context outside the original thread.
- **One reminder per interval per MR.** The `last_reminded` cache prevents spamming. Interval is `T3_FOLLOWUP_INTERVAL` (default 24h).
- **Cache aggressively.** MR metadata, review request permalinks, and approval status are cached in `$T3_DATA_DIR/mr_reminders.json`. Only re-fetch what's stale.

### Periodic mode only

- **Never implement tickets.** Periodic mode is read-only + status transitions + reminders. It never creates worktrees, writes code, or pushes.
- **Auto-execute safe actions.** Ticket transitions (idempotent, gate-checked) and MR reminders (interval-limited) proceed without confirmation.
- **Log everything.** Write a timestamped summary to `$T3_DATA_DIR/followup.log` for auditability.
- **Fail silently on auth issues.** If the forge CLI or chat integration is not authenticated, log the error and exit 0 — don't block the cron job.
