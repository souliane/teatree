---
name: slack-formatting
description: Rendering tables and formatting messages for Slack — the native Block Kit table block, the monospace fence fallback, and the mrkdwn gotchas (no pipe tables, single-asterisk bold, angle-bracket links). Auto-loaded as an overlay companion for work that posts to Slack.
eval_exempt: pure formatting-reference companion auto-loaded for overlay work; carries no standalone agent behaviour of its own
compatibility: any
metadata:
  version: 0.0.1
  subagent_safe: true
---

# Slack Formatting

How teatree renders structured output to Slack. Load this alongside any work that posts to a Slack surface — the outbound notify path, a loop digest, a review-request message.

## Tables — use the formatter, never a hand-rolled pipe table

Slack mrkdwn has **no** GitHub-flavored table syntax. A `| col | col |` / `|---|` markdown table renders as inert literal text — pipes and dashes, no columns. Do not build pipe tables in message text.

Render tabular data through the pure formatter beside the Slack backend:

- `teatree.backends.slack.table_format.slack_table_block(headers, rows, *, alignment=None)` → a native Block Kit `table` block (`rich_text` cells; per-column alignment via `column_settings`). Capped at 100 rows **total** (the header row counts toward Slack's limit, so at most 99 data rows) and 20 columns.
- `teatree.backends.slack.table_format.slack_table_fence(headers, rows, *, alignment=None, max_width=72)` → a space-aligned monospace table wrapped in a triple-backtick fence. Over-budget cells are ellipsis-truncated widest-column-first and never wrapped; rows are capped to match the block with an honest `… and N more` trailer naming the dropped rows; empty rows render `(no rows)`.
- `teatree.backends.slack.table_format.render_table_message(headers, rows, *, alignment=None, title="", max_width=72)` → a `TableMessage(blocks, fence)`: the `blocks` for the native rendering, the `fence` for the message `text` (the notification preview and the degradation path a client uses when it cannot render the block).

Post both together — the `table` block plus the fence as the fallback `text`:

```python
from teatree.backends.slack.table_format import render_table_message
from teatree.notify import NotifyKind, notify_user

message = render_table_message(["Ticket", "State"], rows, title="Open PRs")
notify_user(message.fence, kind=NotifyKind.INFO, idempotency_key=key, blocks=message.blocks)
```

`notify_user`'s `blocks` argument is opaque Block Kit JSON — the formatter builds it, so `teatree.core` never imports the Slack backend.

## mrkdwn gotchas

Slack's mrkdwn is not Markdown. When assembling message text:

- **Bold is `*single asterisks*`** — not `**double**` (which renders literally).
- **Links are `<url|label>`** — not `[label](url)`. `slack_linkify` in `teatree.slack_mrkdwn` rewrites GitHub-flavored links and bare `!N` / `#N` refs for you; `normalize_slack_message` splits walls of text. `notify_user` applies both by default.
- **No nested lists.** One level of `-` bullets only; deeper nesting flattens.
- **No pipe tables** — see above; use the table block or the fence.
- Fenced code (triple backtick) and inline code are preserved verbatim by the linkify/normalize passes, so the monospace fence survives untouched.

## CLI tables

For terminal / CI output (not Slack), render record lists through `teatree.core.table_output.print_table(headers, rows, *, title="", stream=None)` — one `rich.table.Table` seam every record-list command shares, safe on a dumb terminal (rich degrades colour on a non-TTY; a generous fixed width keeps piped columns untruncated).
