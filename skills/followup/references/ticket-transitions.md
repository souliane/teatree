# Ticket Status Transitions

Automated status transitions move tickets through the delivery pipeline based on observable events — not manual label changes.

## Persistent Storage

`$T3_DATA_DIR/tickets/<ticket_iid>/` stores per-ticket state that persists across sessions:

| File | Content | Written by |
|---|---|---|
| `status.json` | `{"label": "...", "last_transition": "...", "last_checked": "..."}` | Transition logic after each status change |

"Review requested?" is **not** a cached JSON file — it is resolved live via `t3 review-request check`/`discover` against the channel + the `ReviewRequestPost` DB row (#1084). A deleted/stale cache can never cause a duplicate post or a wrong transition.

## Transition Table

| From | To | Gate (all must be true) |
|---|---|---|
| Not started | Doing | Follow-up starts ticket |
| Doing | Technical Review | All PRs have review request messages |
| Technical Review | DEV Review | All PRs merged to default branch AND deployed to target env |

Platform-specific label/status mappings are in the [platform reference files](../../platforms/references/) (e.g., `gitlab.md` § "Transition Logic").

Each transition also calls `ticket_update_external_tracker` (extension point) for Notion/Jira/etc.

## Gate Checks

### Doing → Technical Review

1. List all open PRs for the ticket's branch across all repos.
2. For each PR, run `t3 review-request check --mr-url <url>` (or read `review_permalink` from `t3 review-request discover`). A `suppress` with a `permalink` means a request exists in the live channel.
3. If ALL PRs have a review request message → transition is ready.

This is a **live** read (#1084), so it works regardless of whether the review was requested via t3:review-request, the loop, or a manual user post, and never falses on a stale cache.

### Technical Review → DEV Review

1. For each PR associated with the ticket, check if it's merged (PR state = "merged").
2. Call `ticket_check_deployed` extension point — project skill checks if the merged code is deployed to the target environment.
3. Both conditions must be true for ALL PRs → transition is ready.

## Extension Points

| Point | Default | Override in project skill for... |
|---|---|---|
| `ticket_check_deployed` | Return False | Project-specific deployment detection (CI pipeline, GCP, k8s, etc.) |
| `ticket_update_external_tracker` | No-op (log "no external tracker configured") | Notion/Jira status updates |
| `ticket_get_mrs` | List PRs by branch name via issue tracker CLI | Custom PR discovery (multi-repo, naming conventions) |

## Transition Logic

Shared by all transitions — update both the issue label and status. See your [issue tracker platform reference](../../platforms/references/) § "Transition Logic" for the CLI recipe.

## Invocation

Transitions can be triggered:

1. **Automatically by t3:review-request** — after sending review messages, checks the Doing → Technical Review gate.
2. **Explicitly by t3:followup** — in "check status" mode, checks all in-flight tickets.
3. **Manually** — user asks "check ticket status" or "advance tickets".
