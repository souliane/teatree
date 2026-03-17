---
name: t3-review-request
description: Batch review requests — discover open MRs, validate metadata, check team chat for duplicates, post to review channels. Use when user says "ask for review", "request review", "send for review", "post MRs", "review request", or wants to batch-notify reviewers.
compatibility: macOS/Linux, git, issue tracker CLI (glab, gh, etc.), team chat integration.
requires:
  - t3-workspace
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Batch Review Requests

From "MRs exist" to "reviewers are notified." Operates across all user's open MRs, not just the current branch.

## Dependencies

- **t3-workspace** (required) — provides environment context. **Load `/t3-workspace` now** if not already loaded.

## Workflow

### 1-4. Discover, Validate, and Fix MRs

Run the review request script to discover all open non-draft MRs, check CI, and validate metadata in one step:

```bash
$T3_REPO/scripts/review_request.py
```

Uses `T3_FOLLOWUP_REPOS` (same as `t3-followup`) for repo list. Use `--json` for machine-readable output. The script reuses `lib/gitlab.py` (shared with `collect_followup_data.py`).

The script outputs a summary table with CI status, validation results, and readiness. For MRs that fail validation:

- Fix title/description using the issue tracker CLI. See your [issue tracker platform reference](../references/platforms/) § "Update MR" for the recipe.
- When fixing descriptions, **preserve the full body** — only prepend/fix the first line.
- If a ticket URL is missing, ask the user.

### 5. Check Team Chat for Existing Requests

Search review channels for each MR URL to avoid duplicate notifications. See your [chat platform reference](../references/platforms/) § "Search for Messages" for the recipe. Use private-inclusive search — review channels may be private.

Store the permalink for each MR found — it's displayed in the summary table.

### 6. Present Summary Tables

Always present **two tables** before posting:

**Table 1 — MR Overview** (sorted by updated_at descending):

| MR | Title | CI | validate_mr | Review asked? | Ready? |
|---|---|---|---|---|---|
| [!123](https://example.com/mrs/123) | fix(scope): description | ✅/❌/🔄 | ✅/❌ reason | [link](https://example.com/chat/review-requests/123) / ❌ | ✅/⏳/❌ |

- MR column: clickable link
- CI: ✅ green, ❌ failed, 🔄 running
- validate_mr: ✅ passes, or ❌ with specific failure reason
- Review asked?: permalink to the existing request, or ❌ if not yet posted
- Ready?: ✅ yes (CI green + valid + not yet asked), ⏳ wait for CI, ❌ needs fixes

**Table 2 — Messages to Send** (only "Ready" MRs):

| # | Channel | Message |
|---|---|---|
| 1 | #channel | `type(scope): description URL` |

### 7. Send Review Requests

Only after user approval, post messages to the review channels. Use the project's channel routing rules.

**Message format:** `<MR_title_without_ticket_url> <MR_URL>` — one line, nothing else.

**Batching rules** (project-specific, see extension points):

- Default: one message per MR
- Some projects batch multiple MRs from the same repo into one message

### 8. Persist Review Messages

After sending each review request, save the message permalink in `$T3_DATA_DIR/tickets/<ticket_iid>/mr_review_messages.json`. See your [chat platform reference](../references/platforms/) § "Caching Chat Data" for the format.

Create the directory if it doesn't exist. Merge with existing entries (don't overwrite — a ticket may have MRs sent at different times).

Extract the ticket IID from the MR's source branch name or from `TICKET_URL` in `.env.worktree`.

### 9. Check Doing → Technical Review Transition

After all messages are sent (or skipped), check if the ticket is ready to transition:

1. List ALL MRs for the ticket (across all repos).
2. For each MR, check `$T3_DATA_DIR/tickets/<iid>/mr_review_messages.json`.
3. For any MR not in the cache, search the team chat for the MR URL. Cache any results found.
4. If ALL MRs have a review request message → trigger the transition:
    - Update issue tracker label/status. See your [issue tracker platform reference](../references/platforms/) § "Transition Logic".
    - Call `ticket_update_external_tracker` extension point
    - Report: `Ticket #<IID> → Technical Review (all MRs have review requests)`
5. If some MRs are missing review messages → report which ones and skip the transition.

See [`../references/ticket-transitions.md`](../references/ticket-transitions.md) for the full transition system.

### 10. Handle Deferred MRs

After sending, remind the user about MRs that couldn't be sent:

- **Running CI:** "!123 and !456 are still running — re-run this skill when CI completes"
- **Draft MRs:** skip silently (exclude from tables)
- **Failed CI:** "!789 has a failed pipeline — fix before requesting review"

## Rules

- **Never post without user approval.** Always present the summary tables first and wait for explicit "send" / "go ahead".
- **Never post duplicates.** Always check team chat before posting.
- **Draft MRs are invisible.** Exclude them from all tables and counts.
- **Validate before posting.** Never send a review request for an MR that fails validation — fix it first.
- **Preserve description bodies.** When fixing the first line, never lose the rest of the description.
- **Sort consistently.** Always sort by `updated_at` descending.

## Extension Points

Project skills can override these behaviors:

| Extension Point | Default | Override Example |
|---|---|---|
| `review_channel_routing` | Single channel for all MRs | Route by repo (e.g., microservice X → #team-x-reviews) |
| `review_message_batching` | One message per MR | Batch backend MRs into one message |
| `mr_validation_script` | None (skip validation) | `release-notes-scripts/validate_mr.sh` |
| `mr_repos` | Current repo only | All repos in ticket workspace |
