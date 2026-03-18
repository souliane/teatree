---
name: t3-followup
description: Daily follow-up — batch process new tickets, check/advance ticket statuses, remind about MRs waiting for review. Use when user says "followup", "follow up", "autopilot", "batch tickets", "process all tickets", "not started issues", "check status", "check tickets", "advance tickets", "remind reviewers", "MR reminders", "nudge", or wants a daily routine check on all in-flight work.
compatibility: macOS/Linux, zsh, git, issue tracker CLI (glab, gh, etc.).
requires:
  - t3-workspace
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
# Run t3-followup every 2 hours during work hours (Mon-Fri, 9-18)
0 9-18/2 * * 1-5 <agent-cli-command> "/t3-followup --periodic" >> $T3_DATA_DIR/followup.log 2>&1
```

> **Note:** Replace `<agent-cli-command>` with the CLI invocation for your agent platform.

**Configuration:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `T3_FOLLOWUP_CHANNEL` | (none) | Team chat channel for periodic summaries. If unset, output to stdout only. |
| `T3_FOLLOWUP_INTERVAL` | `24h` | Minimum interval between MR reminders per MR. |

## Dependencies

- **t3-workspace** (required) — provides worktree creation, setup, and dev servers. **Load `/t3-workspace` now** if not already loaded.

## Platform Note

The workflow below is platform-neutral. Platform-specific recipes (CLI commands, API calls, GraphQL mutations) are in [`../references/platforms/`](../references/platforms/). The default implementation uses GitLab (`glab` CLI). **Project overlays can override** the tracker-specific parts via extension points (`ticket_update_external_tracker`, `ticket_get_mrs`, `ticket_check_deployed`). If your project uses GitHub Issues, Jira, Linear, or another tracker, implement these extension points in your project overlay.

## Workflow

### 1. Detect Username

Parse the authenticated username from the issue tracker CLI (e.g., `glab auth status`, `gh api user`). Extract the username field.

### 2. Fetch "Not Started" Issues

Query the issue tracker for all open issues assigned to the user with "Not started" status. See your [issue tracker platform reference](../references/platforms/) § "List Issues by Label" for the CLI recipe.

Parse the response to extract: issue ID, project ID, title, URL, and project path.

### 3. Present Confirmation Table

| # | IID | Project | Title | URL |
|---|-----|---------|-------|-----|
| 1 | 8166 | backend-repo | End of year allowance | ... |
| 2 | 8170 | frontend-repo | Fix address fields | ... |

**The project where the issue is filed does NOT determine where the implementation happens.** Tickets may require work in any combination of repos.

Ask the user to pick "All" or specific ticket numbers. Use the agent platform's native question UI if it has one; otherwise ask plainly in the conversation. **Never start without explicit user approval.**

### 4. Pre-Fetch External Context (Non-Negotiable)

For each confirmed ticket:

**a.** Extract external links from the issue description (external tracker URLs, chat links, etc.)

**b.** Fetch each external page (Notion, Confluence, Linear, etc.) using the appropriate integration. Save the full content.

**c.** Download any embedded images from issues or external pages.

### 5. Scope Analysis (Non-Negotiable)

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

Move the issue label/status from "Not started" to "Doing" / "In progress". See your [issue tracker platform reference](../references/platforms/) § "Transition Logic" for the CLI recipe.

**External tracker — update status** (if configured by the project skill):

If the project uses an external tracker (e.g., Notion), update the ticket status to "In progress" there too. The project skill's references should document the tracker's data source ID, status options, and API calls. If no external tracker is configured, skip this step.

If not found in the external tracker, log a warning but continue — not all tickets have an entry.

**b. Run the full lifecycle** using the loaded skills — each phase uses the corresponding t3- skill:

1. **Intake** (`/t3-ticket`): Fetch issue, create worktree (`t3 workspace ticket`), run `t3 lifecycle setup`, verify environment.
2. **Implementation** (`/t3-code`): Implement with TDD. Check ALL repos in scope.
3. **Testing** (`/t3-test`): Run tests, fix failures, ensure lint passes.
4. **Delivery** (`/t3-ship`): Commit, push, create MRs for ALL repos with changes.

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

- For successful tickets: suggest `/t3-review-request` for batch review notifications.
- For failed tickets: report the phase, error, and worktree path so the user can investigate or switch to `/t3-debug`.

### 8b. Auto-Discover In-Flight Tickets (Cache Bootstrap)

Before running transition checks (§9) or status check mode (§10), ensure `$T3_DATA_DIR/tickets/` is populated. If the cache is empty or missing tickets, **auto-discover from open MRs:**

1. **Detect repos.** List repos the user works in — scan `$T3_WORKSPACE_DIR` for known repo directories (e.g., `backend-api`, `frontend-app`), or use a configured repo list from the project overlay.

2. **List open MRs.** For each repo, list all open, non-draft MRs authored by the current user (see [issue tracker platform reference](../references/platforms/) § "List MRs").

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
2. Check `$T3_DATA_DIR/tickets/<iid>/mr_review_messages.json` for cached review request messages. See your [chat platform reference](../references/platforms/) § "Caching Chat Data".
3. For MRs without a cache entry, search the team chat for the MR URL. Cache any results found.
4. If ALL MRs have a review request message → transition the ticket.

**b. Technical Review → DEV Review:**

For each ticket with `Process::Technical Review`:

1. Check if ALL associated MRs are merged (query MR state via the issue tracker CLI).
2. Call `ticket_check_deployed` extension point to verify deployment to target environment.
3. If all merged AND deployed → transition the ticket.

**Transition actions** (for both a and b):

- Update issue tracker label/status. See your [issue tracker platform reference](../references/platforms/) § "Transition Logic" for the CLI recipe.
- Call `ticket_update_external_tracker` extension point (Notion, Jira, etc.).
- Report: `Ticket #<IID> → <new status> (reason)`

See [`../references/ticket-transitions.md`](../references/ticket-transitions.md) for the full transition system.

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

List all user's open MRs across repos, then filter to those that are **open**, **not draft**, **not yet approved**. See your [issue tracker platform reference](../references/platforms/) § "List MRs" and § "Check Approval Status" for CLI recipes.

Also check for colleague comments (exclude system notes and author's own) via the MR notes API.

**Cache MR metadata** in `$T3_DATA_DIR/mr_reminders.json` — see your [chat platform reference](../references/platforms/) § "MR Reminder Cache" for the format.

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

See your [chat platform reference](../references/platforms/) for known limitations (e.g., externally shared channels).

#### 11d. Cleanup

After posting (or during any follow-up invocation), remove entries from `mr_reminders.json` where the MR is now approved or merged. This keeps the cache file small.

### 12. Dashboard Generation

After completing any followup run (interactive or periodic), generate an HTML dashboard at `$T3_DATA_DIR/followup.html` from the cached `followup.json` data. The dashboard provides a visual overview of all in-flight work.

**Generation:** Run `scripts/generate_dashboard.py [INPUT] [OUTPUT]` (defaults to `$T3_DATA_DIR/followup.json` → `$T3_DATA_DIR/followup.html`). The renderer lives in `scripts/lib/dashboard_renderer.py` — pure functions, no I/O, fully tested.

**Theme and layout:**

- Dark theme (Tokyo Night palette), monospace font, `max-width: 1400px`
- `meta http-equiv="refresh" content="120"` for auto-reload
- Header shows "Generated: YYYY-MM-DD HH:MM UTC (Xh Xm ago)" — computed at render time from `generated_at`
- Sections use card-style containers with rounded corners
- Color-coded pill badges: `success` (green), `failed` (red), `running` (yellow), `pending` (muted), `skipped` (gray), `approved` (purple)
- Pill links: no underline on hover, use `filter: brightness(1.3)` instead

**Section 1 — In-Flight Work** (unified table):

- One row per MR, ticket cells use `rowspan` to group MRs under the same ticket
- Ticket-level cells (`ticket-cell` class) have a thicker bottom border to visually separate ticket groups
- Columns (in order):

| Column | Scope | Content | Links to |
|---|---|---|---|
| Ticket | ticket (rowspan) | `#IID` + title | Issue URL |
| Status | ticket (rowspan) | Pill(s) — supports stacked pills for multiple trackers (e.g., GitLab "Doing" + external "In Progress") via `status-stack` flex column | Issue URL |
| Feature Flag | ticket (rowspan) | `code` pill with flag name, or `—` | — |
| MR | per MR | `repo-name !IID` | MR URL |
| Pipeline | per MR | Status pill | Pipeline URL |
| E2E | per MR | "test plan" pill linking to MR comment with screenshots, or `—` for backend-only MRs | MR note anchor |
| Review Request | per MR | Channel name pill linking to review request permalink, or status text ("waiting group", "not sent") | Chat permalink |
| Approved | per MR | `count/required` pill | — |

- **Feature flag source:** `feature_flag` field in `followup.json` ticket entry. Populated from ticket title bracket prefix (e.g., `[flag_name] Title`) or from the project overlay's `followup_enrich_data`.

**Section 2 — Review Comments** (simple table):

- Tracks MRs with active review comment threads — both directions
- Columns: MR | Status | Details
- Status values: `Waiting reviewer` (pending pill), `Addressed` (success pill), `Needs reply` (running pill)
- Populated from `review_comments_tracking` in `followup.json`
- Include MRs from the In-Flight table that have unresolved comment threads, plus standalone MRs not tied to current tickets (e.g., older MRs still under review)

**Section 3 — Draft MRs** (same table layout as In-Flight Work):

- Lists all user's open draft/WIP MRs across repos
- Uses the **same columns** as the In-Flight Work table (Ticket, Status, Feature Flag, MR, Pipeline, E2E, Review Request, Approved) — most columns will show `—` since drafts typically lack reviews/approvals, but the consistent layout makes it scannable
- If a draft MR is linked to a ticket (extractable from branch name), show the ticket info; otherwise show `—` in ticket-level columns
- Populated by querying the issue tracker for open draft MRs authored by the current user
- Stored in `draft_mrs` key in `followup.json`

**Section 4 — Actions Taken This Session:**

- Timestamped bullet list of actions performed during this followup run
- Populated from `actions_log` in `followup.json`

**Extension point: `followup_enrich_dashboard`** — project overlays can inject additional columns or sections (e.g., deployment environment). See [extension points](../references/extension-points.md).

**Extension point: `followup_enrich_data`** — project overlays can add project-specific fields to `followup.json` entries before dashboard generation (e.g., Notion status, customer/tenant info).

### 13. Cache Cleanup (Non-Negotiable — First Action on Every Load)

**Execute immediately when the skill is loaded** — before responding to the user, before asking what they want, before anything else. This is the first thing followup does on every invocation (both interactive and periodic). The user should never have to ask for a cache refresh.

**Always use `t3 mr followup -v`** to collect data. This command handles MR discovery, pipeline status, approvals, merge detection, and cache cleanup in one deterministic pass. Never manually call GitLab APIs to build `followup.json` — the script is the single entry point. After the script runs, use `generate_dashboard.py` to render the HTML dashboard.

The script reads `T3_FOLLOWUP_REPOS` (comma-separated GitLab paths, e.g. `org/repo1,org/repo2`) to discover open MRs. If this var is not set, no MRs will be found.

Internally the script:

1. Discovers open MRs across all configured repos.
2. Enriches each MR with pipeline status, approvals, and colleague comments.
3. Fetches issue labels for linked tickets.
4. Detects MRs merged since the last run and logs them.
5. Cleans review tracking entries for merged MRs.
6. Writes the result to `$T3_DATA_DIR/followup.json`.

**During long sessions:** Also re-run cache cleanup after significant events (ticket completed, MR pushed, context switch) — don't wait for the next explicit `/t3-followup` invocation.

## `followup.json` Schema

The followup cache at `$T3_DATA_DIR/followup.json` is the single source of truth for all in-flight work. It is platform-neutral — the core schema covers what t3-followup needs; project overlays add fields via `followup_enrich_data`.

```json
{
  "generated_at": "ISO timestamp",
  "tickets": {
    "<ticket_id>": {
      "title": "Human-readable title",
      "url": "Issue tracker URL or null",
      "tracker_status": "Platform-neutral status string",
      "feature_flag": "Flag name or null",
      "mrs": ["<repo>!<iid>", ...]
    }
  },
  "mrs": {
    "<repo>!<iid>": {
      "url": "MR web URL",
      "repo": "Repository short name",
      "project_id": 12345,
      "title": "MR title",
      "branch": "Source branch name",
      "ticket": "<ticket_id>",
      "pipeline_status": "success|failed|running|pending|null",
      "pipeline_url": "URL or null",
      "review_requested": true,
      "review_channel": "#channel-name",
      "review_permalink": "Chat permalink or null",
      "review_comments": { "status": "addressed|pending|null", "details": "..." },
      "e2e_test_plan_url": "URL to MR comment with test plan, or null",
      "approvals": { "count": 0, "required": 1 },
      "skipped": false,
      "skip_reason": null
    }
  },
  "review_comments_tracking": {
    "<repo>!<iid>": {
      "url": "MR web URL",
      "status": "waiting_reviewer|addressed|needs_reply",
      "details": "Human-readable summary"
    }
  },
  "draft_mrs": {
    "<repo>!<iid>": {
      "url": "MR web URL",
      "repo": "Repository short name",
      "title": "MR title (without Draft: prefix)",
      "pipeline_status": "success|failed|running|pending|null",
      "pipeline_url": "URL or null"
    }
  },
  "actions_log": ["Action description", ...]
}
```

Project overlays can add extra fields to ticket and MR entries (e.g., `notion_status`, `tenant`). The core schema ignores unknown fields — overlays read/write their own fields alongside the core ones.

## Rules

### Interactive mode (default)

- **Sequential only.** Never use sub-agents for ticket implementation. See [`../references/agent-rules.md`](../references/agent-rules.md) § "Sub-Agent Limitations".
- **Never start without user approval.** Always show the confirmation table first.
- **Always pre-fetch external context.** Read all specs before starting implementation.
- **Always run scope analysis.** The issue tracker project ≠ the implementation repo.
- **`t3 lifecycle setup` is mandatory for every ticket.** Never skip it (see `/t3-workspace` § Never Hand-Edit Generated Files).
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
