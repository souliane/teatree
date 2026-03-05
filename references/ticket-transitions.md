# Ticket Status Transitions

Automated status transitions move tickets through the delivery pipeline based on observable events — not manual label changes.

## Persistent Storage

`$T3_DATA_DIR/tickets/<ticket_iid>/` stores per-ticket state that persists across sessions:

| File | Content | Written by |
|---|---|---|
| `mr_review_messages.json` | `{"<mr_url>": {"permalink": "...", "channel": "...", "ts": "..."}}` | t3-review-request (after sending) + transition check (after chat search) |
| `status.json` | `{"label": "...", "last_transition": "...", "last_checked": "..."}` | Transition logic after each status change |

## Transition Table

| From | To | Gate (all must be true) |
|---|---|---|
| Not started | Doing | Follow-up starts ticket |
| Doing | Technical Review | All MRs have review request messages |
| Technical Review | DEV Review | All MRs merged to default branch AND deployed to target env |

Platform-specific label/status mappings are in the [platform reference files](platforms/) (e.g., `platforms/gitlab.md` § "Transition Logic").

Each transition also calls `ticket_update_external_tracker` (extension point) for Notion/Jira/etc.

## Gate Checks

### Doing → Technical Review

1. List all open MRs for the ticket's branch across all repos.
2. For each MR, check `$T3_DATA_DIR/tickets/<iid>/mr_review_messages.json` for a cached review request permalink.
3. For any MR without a cached entry, search the team chat for the MR URL. See your [chat platform reference](platforms/) § "Search for Messages".
4. If found, cache the permalink in `mr_review_messages.json`.
5. If ALL MRs have a review request message → transition is ready.

This works regardless of whether the review was requested via t3-review-request or manually.

### Technical Review → DEV Review

1. For each MR associated with the ticket, check if it's merged (MR state = "merged").
2. Call `ticket_check_deployed` extension point — project skill checks if the merged code is deployed to the target environment.
3. Both conditions must be true for ALL MRs → transition is ready.

## Extension Points

| Point | Default | Override in project skill for... |
|---|---|---|
| `ticket_check_deployed` | Return False | Project-specific deployment detection (CI pipeline, GCP, k8s, etc.) |
| `ticket_update_external_tracker` | No-op (log "no external tracker configured") | Notion/Jira status updates |
| `ticket_get_mrs` | List MRs by branch name via issue tracker CLI | Custom MR discovery (multi-repo, naming conventions) |

## Transition Logic

Shared by all transitions — update both the issue label and status. See your [issue tracker platform reference](platforms/) § "Transition Logic" for the CLI recipe.

## Invocation

Transitions can be triggered:

1. **Automatically by t3-review-request** — after sending review messages, checks the Doing → Technical Review gate.
2. **Explicitly by t3-followup** — in "check status" mode, checks all in-flight tickets.
3. **Manually** — user asks "check ticket status" or "advance tickets".
