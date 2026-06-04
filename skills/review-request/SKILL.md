---
name: review-request
description: Batch review requests — discover open PRs, validate metadata, check for duplicates, post to review channels. Use when user says "request review", "send for review", or wants to batch-notify reviewers.
compatibility: macOS/Linux, git, issue tracker CLI (glab, gh, etc.), team chat integration.
requires:
  - workspace
  - platforms
  - followup
  - teatree
companions:
  - verification-before-completion
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Batch Review Requests

## Delegation

This skill reuses `verification-before-completion` (from [obra/superpowers](https://github.com/obra/superpowers), optional) for the final send/no-send gate.
TeaTree keeps the rest locally because PR discovery, chat deduplication, routing, and transition updates are project workflow rather than generic doctrine.

From "PRs exist" to "reviewers are notified." Operates across all user's open PRs, not just the current branch.

## Dependencies

- **t3:workspace** (required) — provides environment context. **Load `/t3:workspace` now** if not already loaded.

## Workflow

### 1-4. Discover, Validate, and Fix PRs

Run the review request discovery to find all open non-draft PRs, check CI, and validate metadata in one step:

```bash
t3 review-request discover
```

Uses the overlay's `get_followup_repos()` (same as `t3:followup`) for repo list. Use `--json` for machine-readable output.

The script outputs a summary table with CI status, validation results, and readiness. For PRs that fail validation:

- Fix title/description using the issue tracker CLI. See your [issue tracker platform reference](../platforms/references/) § "Update PR" for the recipe.
- When fixing descriptions, **preserve the full body** — only prepend/fix the first line.
- If a ticket URL is missing, ask the user.

### 5. Live Dedup Check (Mandatory, race-safe — #1084)

**Do not "search then later post".** A manual search separated from the post is racy: between the search and the post the user (or a retry, or a parallel loop) can post the same request — exactly the incident this guard exists to prevent. Instead, for every Ready PR run the dedup gate **in the same turn as the post** (§7) and obey its verdict:

```bash
t3 review-request check --mr-url <PR_URL>
```

`check` reads the **live** review channel with the *same token the post will use* (a Slack-Connect channel is read with the user `xoxp`, not the bot token — read-token == post-token), bounded and fail-safe, and takes an atomic DB claim. It prints:

- `{"action": "post"}` → you may post this PR's review request (this turn).
- `{"action": "suppress", "permalink": "...", "author": "..."}` → **do not post**. A message for this PR already exists in the channel (a prior agent post, or the user's own out-of-band post — any author suppresses). Record the returned `permalink` in the summary table as the existing request and move on. The guard has already reconciled the DB so the loop will not nag.
- `{"action": "suppress", "reason": "read_failed_failsafe"}` → the live read could not complete; **do not post** (bias to not double-posting). The obligation stays open — a later tick retries.

The guard is the single source of truth for "already requested?". Do not second-guess a `suppress` with a manual search.

### 6. Present Summary Tables

Always present **two tables** before posting:

**Table 1 — PR Overview** (sorted by updated_at descending):

| PR | Title | CI | validate_pr | Review asked? | Ready? |
|---|---|---|---|---|---|
| [!123](https://example.com/mrs/123) | fix(scope): description | ✅/❌/🔄 | ✅/❌ reason | [review request](https://example.com/chat/review-requests/123) / ❌ | ✅/⏳/❌ |

- PR column: clickable link
- CI: ✅ green, ❌ failed, 🔄 running
- validate_pr: ✅ passes, or ❌ with specific failure reason
- Review asked?: permalink to the existing request, or ❌ if not yet posted
- Ready?: ✅ yes (CI green + valid + not yet asked), ⏳ wait for CI, ❌ needs fixes

**Table 2 — Messages to Send** (only "Ready" PRs):

| # | Channel | Message |
|---|---|---|
| 1 | #channel | `type(scope): description URL` |

### 7. Send Review Requests

Only after user approval, **for each Ready PR** run the single sanctioned post command — it bundles the §5 live-channel dedup, the #960 recorded-approval chokepoint, and the post in one classifier-legible transaction (#1098):

```bash
t3 <overlay> review-request post --mr-url <PR_URL> --approver <user-id> --title "<type(scope): description>"
```

This is the **only** sanctioned post path. **Never** post a review request with a raw `messaging_from_overlay(...).post_message(...)` Bash call — that bypasses the dedup claim and the recorded-approval binding and is (correctly) blocked by the auto-mode classifier. The command:

- runs `review_request_check`'s live-channel dedup internally and prints `{"action": "suppress", ...}` + exits 0 with **no post** on a live duplicate;
- requires a matching, unconsumed, exactly-scoped #960 `OnBehalfApproval`. With none it prints the exact `t3 review approve-on-behalf '<MR_URL>' review_request_post --approver <id>` remediation, refuses (`{"action": "refused", "reason": "on_behalf_not_approved"}`, exit 2), and **rolls back** the dedup claim so a later approved retry is not wedged;
- on success posts once, consumes the approval (single-use), writes the #960 audit row, records the permalink, and prints `{"action": "post", "permalink": ...}`.

The user records the approval (no terminal required) with `t3 review approve-on-behalf '<MR_URL>' review_request_post --approver <their-id>`; then re-run the post command. `--approver` is the user id that recorded it. The approver can never be the executing agent/loop (maker≠checker, enforced at record time).

Use the project's channel routing rules. `--title` is recommended (the command does not fetch the MR title over the network — without it the subject defaults to `Please review`).

**Message format:** `<title> <MR_URL>` — one line, nothing else.

**Batching rules** (project-specific, see extension points):

- Default: one message per PR
- Some projects batch multiple PRs from the same repo into one message

### 8. Persistence Is Automatic

`t3 <overlay> review-request post` (§7) takes the atomic `ReviewRequestPost` claim, consumes the #960 approval, writes the audit row, and records the permalink to `mr_review_messages.json` itself — and the Slack review-sync attaches the permalink to the PR's ticket record. **The live channel + the `ReviewRequestPost` row are the dedup source of truth — NOT the JSON file.** `mr_review_messages.json` is only a durable *record* of where the post landed (the permalink, outside Slack); it is never consulted as a dedup oracle, so a stale or missing file can never cause a duplicate post (the guard reads the live channel). No manual persistence step — and no hand-written JSON — is required after posting.

### 9. Check Doing → Technical Review Transition

After all messages are sent (or skipped), check if the ticket is ready to transition:

1. List ALL PRs for the ticket (across all repos).
2. For each PR, run `t3 review-request check --mr-url <PR_URL>` — a `suppress` with a `permalink` (or the PR's `review_permalink` from `t3 review-request discover`) means a request exists. This is a **live** read, so it never falses on a stale cache.
3. (No step — the live check in 2 already replaces the old JSON-cache lookup.)
4. If ALL PRs have a review request message → trigger the transition:
    - Update issue tracker label/status. See your [issue tracker platform reference](../platforms/references/) § "Transition Logic".
    - Call `ticket_update_external_tracker` extension point
    - Report: `Ticket #<IID> → Technical Review (all PRs have review requests)`
5. If some PRs are missing review messages → report which ones and skip the transition.

See [`../followup/references/ticket-transitions.md`](../followup/references/ticket-transitions.md) for the full transition system.

### 10. Handle Deferred PRs

After sending, remind the user about PRs that couldn't be sent:

- **Running CI:** "!123 and !456 are still running — re-run this skill when CI completes"
- **Draft PRs:** skip silently (exclude from tables)
- **Failed CI:** "!789 has a failed pipeline — fix before requesting review"

## Rules

- **Never post without user approval.** Always present the summary tables first and wait for explicit "send" / "go ahead".
- **Post only via the sanctioned command — enforced, not advisory.** `t3 <overlay> review-request post --mr-url <url> --approver <id>` is the ONLY sanctioned post path (#1098). It runs the #1084 live-channel dedup + atomic DB claim and the #960 recorded-approval chokepoint in one transaction, so two posts (agent+agent, or user+agent) for the same PR within the dedup window are impossible and an unapproved post cannot go out. A user's manual out-of-band post suppresses the agent. **Never** post with a raw `messaging_from_overlay(...).post_message(...)` Bash call, a JSON cache, or an out-of-turn manual search — all bypass the dedup+approval binding and are racy and stale-prone.
- **Draft PRs are invisible.** Exclude them from all tables and counts.
- **Validate before posting.** Never send a review request for a PR that fails validation — fix it first.
- **Preserve description bodies.** When fixing the first line, never lose the rest of the description.
- **Sort consistently.** Always sort by `updated_at` descending.

## Extension Points

Project skills can override these behaviors:

| Extension Point | Default | Override Example |
|---|---|---|
| `review_channel_routing` | Single channel for all PRs | Route by repo (e.g., microservice X → #team-x-reviews) |
| `review_message_batching` | One message per PR | Batch backend PRs into one message |
| `mr_validation_script` | None (skip validation) | `release-notes-scripts/validate_mr.sh` |
| `mr_repos` | Current repo only | All repos in ticket workspace |
