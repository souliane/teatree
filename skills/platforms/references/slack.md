# Slack Platform Reference

> Recipes for Slack-specific operations. Skills reference this file via `See platforms/slack.md § <section>`.

---

## Access Method

Slack has no official CLI. Use MCP tools (e.g., `slack_send_message`, `slack_search_public_and_private`) or the Slack Web API directly.

## Review-Request Dedup (race-safe — #1084)

**Do not hand-search the channel to dedup a review request.** A manual search separated from the post is a race: the user (or a retry) can post the same request in the gap. Use the enforced gate **in the same turn as the post**:

```bash
t3 review-request check --mr-url "<MR_URL>"
```

It reads the live channel's recent history (`conversations.history`, recency-bounded, no `search:read` scope) and takes an atomic DB claim. Post only on `{"action": "post"}`; on `{"action": "suppress", ...}` a request already exists (any author — a user's own post suppresses the agent) — record the returned `permalink` and skip.

**Connect-token read note (load-bearing).** The gate reads with the *same* token an outbound post to that channel would use. For a Slack-Connect externally-shared channel the bot token (`xoxb-…`) cannot read history (`mcp_externally_shared_channel_restricted`) — the user OAuth token (`xoxp-…`) is required, and the gate routes to it via the single token-selection policy (`teatree.backends.slack_token_policy.channel_token`) exactly when the post would. Read-token == post-token: a dedup that read with a token the channel rejects would always see an empty history and never suppress a duplicate.

### Manual search (non-dedup uses only)

For non-dedup lookups, a private-inclusive search with `response_format: "detailed"` returns permalinks:

```text
slack_search_public_and_private(query: "<MR_URL>", response_format: "detailed")
```

## Colleague-Slack On-Behalf Egress (single chokepoint)

Every Slack post or reaction made under the user's identity on a **colleague** surface (a colleague's DM, a review/broadcast channel) goes through one class — `teatree.core.on_behalf_egress.OnBehalfSlackEgress.post/.react` — which runs gate→route→emit→audit in one place: the #1750 `route_token` classifier decides self-vs-colleague (fail-closed to colleague on an unknown surface), a self-DM short-circuits ungated (a bot→user / self-ack is never on-behalf), and a colleague surface calls the on-behalf gate before the wire call (BLOCK with no recorded approval raises and nothing posts), then the routed post/react, then the after-receipt DM on success. `t3 notify post/react` and `t3 slack react` route through it; the FSM transition/approval reactions (`signals.py`) and GitLab approve/comment are out of scope (already gated on their own transports). Never reintroduce a raw `react_routed`/`post_routed`/personal-`xoxp` `reactions.add` colleague egress outside that class — an import-guard test fails the build if you do.

## Send Messages

### Post to a Channel

```text
slack_send_message(channel: "#channel-name", text: "message text")
```

**Message format for review requests:** `<MR_title_without_ticket_url> <MR_URL>` — one line, nothing else.

### Reply in Thread

Use the parent message's `ts` (timestamp) to reply in-thread:

```text
slack_send_message(channel: "#channel-name", text: "message text", thread_ts: "<parent_ts>")
```

**Reminder format:** Post the **clean PR title as a Slack link to the original review request** (not the PR URL). Strip feature flag tags (`[flag_name]`) and ticket URLs from the title:

```text
<https://slack.com/archives/C.../p...|feat: expose deed_date in proof meta liability serializer>
```

This keeps all discussion in the original review thread.

## Slack Connect Channels

MCP Slack integrations cannot post to externally shared (Slack Connect) channels. If posting fails with `mcp_externally_shared_channel_restricted`, fall back to sending the formatted messages to the user's DM so they can copy-paste.

## Caching Chat Data

### Review Request Permalinks

**The source of truth for "review requested?" is the live channel + the `ReviewRequestPost` DB row, not a JSON file (#1084).** `t3 review-request check` / `discover` read the live channel and reconcile the DB; killing any cache file must never (and by design does not) cause a duplicate post. Do not maintain a `mr_review_messages.json` dedup oracle. The Slack review-sync attaches the discovered permalink onto the PR's ticket record automatically.

### PR Reminder Cache

Store in `$T3_DATA_DIR/mr_reminders.json`:

```json
{
  "<mr_web_url>": {
    "iid": 123,
    "project": "backend-repo",
    "title": "fix(scope): description",
    "original_review_permalink": "https://slack.com/archives/...",
    "channel": "#code-review",
    "last_reminded": "2026-03-05",
    "approved": false
  }
}
```

Remove entries where the PR is approved or merged to keep the cache small.

## Rules

- **Never post without user approval.** Always present a dry-run summary first.
- **Never post duplicates — enforced.** Run `t3 review-request check --mr-url <url>` in the same turn as the post and abort on `suppress` (#1084). The gate's live read + atomic DB claim, not a hand-search or a JSON cache, is what makes a duplicate impossible.
- PR URLs stay hidden in reminders. Post only the Slack permalink to the original review request.
- **One reminder per day per PR.** Use `last_reminded` to prevent spamming.
