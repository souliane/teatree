# Press-review digest — read when composing a scan result

The scan returns candidates in `article_suggestions`; it does not post a DM.
After settling new article rows, the loop records one owner delivery through `notify_user`,
deduped with `news-digest-<task pk>`.

`teatree.core.news_digest.render_digest` renders the digest:

```text
Press review — <YYYY-MM-DD> — <N> article decision(s) recorded.

- <article link|title> — Filed <issue link|issue> — <rationale>
- <article link|title> — Dropped — <rationale>
```

Each article link is clickable Slack mrkdwn; file paths and code symbols in the
rationale use monospace. The list contains only new candidates recorded by this
task. A scan with no new candidates posts no digest. Source fetch failures go in
the returned envelope summary, not in the digest.
