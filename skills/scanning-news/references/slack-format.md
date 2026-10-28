# Press-review digest — read when composing a scan result

The scan returns candidates in `article_suggestions`; it does not post a DM.
After recording new `PENDING` rows, the loop records one owner delivery through `notify_user`,
deduped with `news-digest-<task pk>`.

`teatree.core.news_digest.render_digest` renders the digest:

```text
Press review — <YYYY-MM-DD> — <N> candidate(s) queued behind the ask-gate.
Nothing is filed as an issue until you approve.

- <article link|title> — <rationale>
```

Each article link is clickable Slack mrkdwn; file paths and code symbols in the
rationale use monospace. The list contains only new candidates recorded by this
task. A scan with no new candidates posts no digest. Source fetch failures go in
the returned envelope summary, not in the digest.
