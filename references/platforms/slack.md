# Slack Platform Reference

> Recipes for Slack-specific operations. Skills reference this file via `See platforms/slack.md § <section>`.

---

## Access Method

Slack has no official CLI. Use MCP tools (e.g., `slack_send_message`, `slack_search_public_and_private` in Claude Code) or the Slack Web API directly.

## Search for Messages

Search review channels for MR/PR URLs to avoid duplicate notifications.

**Use private-inclusive search** — review channels may be private. Request `response_format: "detailed"` to get message **permalinks**.

```text
slack_search_public_and_private(query: "<MR_URL>", response_format: "detailed")
```

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

**Reminder format:** Post the **clean MR title as a Slack link to the original review request** (not the MR URL). Strip feature flag tags (`[flag_name]`) and ticket URLs from the title:

```text
<https://slack.com/archives/C.../p...|feat: expose deed_date in proof meta liability serializer>
```

This keeps all discussion in the original review thread.

## Slack Connect Channels

MCP Slack integrations cannot post to externally shared (Slack Connect) channels. If posting fails with `mcp_externally_shared_channel_restricted`, fall back to sending the formatted messages to the user's DM so they can copy-paste.

## Caching Chat Data

### Review Request Permalinks

Store in `$T3_DATA_DIR/tickets/<ticket_iid>/mr_review_messages.json`:

```json
{
  "<mr_web_url>": {
    "permalink": "https://slack.com/archives/...",
    "channel": "#tech_backend",
    "ts": "2026-03-05T14:30:00Z"
  }
}
```

Create the directory if it doesn't exist. Merge with existing entries (don't overwrite — a ticket may have MRs sent at different times).

### MR Reminder Cache

Store in `$T3_DATA_DIR/mr_reminders.json`:

```json
{
  "<mr_web_url>": {
    "iid": 123,
    "project": "backend-repo",
    "title": "fix(scope): description",
    "original_review_permalink": "https://slack.com/archives/...",
    "channel": "#tech_backend",
    "last_reminded": "2026-03-05",
    "approved": false
  }
}
```

Remove entries where the MR is approved or merged to keep the cache small.

## Rules

- **Never post without user approval.** Always present a dry-run summary first.
- **Never post duplicates.** Always search before posting.
- **Cache aggressively.** Write to `mr_review_messages.json` after every search to avoid redundant API calls.
- **Never expose MR URLs in reminders.** Post only the Slack permalink to the original review request.
- **One reminder per day per MR.** Use `last_reminded` to prevent spamming.
