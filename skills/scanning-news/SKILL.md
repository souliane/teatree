---
name: scanning-news
description: Scans today's TLDR AI and The Rundown AI editions for ideas that could improve teatree, fetches the full article for promising items, files a souliane/teatree issue when a concrete t3 improvement is identified, and posts a terse Slack DM summary. Use when user says "scan news", "scanning news", "ai newsletter scan", "improvement scan", "tldr scan", or runs the daily improvement routine.
compatibility: macOS/Linux, git, gh CLI, Slack integration.
requires:
  - workspace
  - rules
  - platforms
triggers:
  priority: 90
  keywords:
    - '\b(scan(ning)? (the )?(ai )?news|ai newsletter scan|improvement scan|tldr scan|scan tldr|scan rundown)\b'
metadata:
  version: 0.0.1
  subagent_safe: false
  last_research_date: "2026-05-20"
---

# t3:scanning-news — Mine AI Newsletters for t3 Improvements

## Non-Negotiables

1. **Per-article ask gate is mandatory (#1391).** Never call `gh issue create` directly for a scanned article. Every candidate goes through `teatree.core.article_ingestion_gate.enqueue_candidates_and_notify`, which records a `PendingArticleSuggestion` row, sends the user a batch DM, and waits for explicit approval via `t3 manage news approve <id>`. The user pilots the backlog — silent ticket creation from any third-party-prose scanner is banned.
2. **Date-of-edition verification is mandatory.** Before processing any newsletter, read the issue date verbatim from the page header and compare it to today's workspace date. If they do not match, do not proceed — either re-fetch with a stricter prompt, fall back to the latest published edition, or abort that source. Never silently treat yesterday's edition as today's.
3. **Aggregator-detection is mandatory.** TLDR's `tldr.tech/<track>/<date>` page can return a kitchen-sink summary that lists stories from every TLDR track (Tech, AI, Dev, Product, IT, Marketing, Design, Infosec, Crypto, Founders, DevOps, Data, Fintech). The real per-issue newsletter is 5–9 stories grouped under named sections. If the fetch returns >15 stories or contains track-names that are not the requested track, the prompt drifted — re-fetch with the stricter prompt before continuing.
4. **Idempotency via `url_hash`.** The ask-gate skips already-queued URLs. A second scan of the same edition is a no-op.
5. **No AI signature** on issues or DMs (per `t3:rules`).
6. **Never invent stories.** If a source fetch fails, omit that source from the DM and note the failure.

## Command Reference

```bash
# Enqueue candidates (the only sanctioned path — replaces direct gh issue create)
python -c "
import django; django.setup()
from teatree.core.article_ingestion_gate import enqueue_candidates_and_notify, ArticleCandidate
enqueue_candidates_and_notify([
    ArticleCandidate(url='<url>', title='<title>', summary='<why-interesting>', source='tldr-ai'),
])
"

# Inspect / decide
t3 manage news pending
t3 manage news approve <id>
t3 manage news reject <id> --reason "<one-line>"

# Dedupe check (still useful before enqueueing — gate is idempotent on URL hash)
gh --repo souliane/teatree issue list --label from-news-scan --state all --search "<article URL>"
```

## Workflow

### 1. Resolve today's date

Read the workspace clock; format `YYYY-MM-DD`. Use this string consistently for both fetches and for the DM header.

### 2. Fetch TLDR AI

URL: `https://tldr.tech/ai/<YYYY-MM-DD>`. Use the WebFetch prompt in `references/fetch-prompts.md`.

Apply the two gates (Non-Negotiables 1 + 2). On mismatch:

- Edition date < today's date → today's issue is not yet published (TLDR drops in US morning). Use the latest published edition; record the actual issue date for the DM.
- Aggregator detected → re-fetch with the stricter prompt; if still aggregator, treat as fetch failure.

### 3. Fetch The Rundown AI

The Rundown AI does not have a clean dated URL pattern. Start from `https://www.therundown.ai/` or the archive `https://www.therundown.ai/archive`. Discover today's post URL from the landing/archive, then fetch it. Apply the same date and content-shape gates.

### 4. Triage for relevance to teatree

For each story across both editions, decide if it is relevant to teatree-improvement signals:

- AI agent architectures, lifecycles, orchestration
- Coding agents (Claude Code, Cursor, etc.), skill systems, MCP servers
- Multi-repo / monorepo dev workflows, worktrees, CI pipelines
- Code review automation, test generation, observability for agents
- Prompt engineering, evaluation frameworks, agent safety
- Scheduling, batching, queueing, daemon/loop patterns
- Developer-facing CLI tooling, hooks, plugin ecosystems

Irrelevant: product launches without technique detail, market/funding news, consumer-facing AI apps, hardware reviews, geopolitics.

### 5. Deep-read each relevant item

WebFetch the linked article. Ask for: thesis in 2 sentences, concrete techniques/patterns mentioned, any code or config worth replicating.

### 6. Decide "can this improve t3?"

For each deep-read article, ask: *is there a concrete, scoped change to teatree code, skills, or workflow that would benefit from this idea?*

- **Yes** examples: "an evaluation harness pattern we don't have", "a missing hook category for the loop", "a faster claim-next algorithm for FSM transitions."
- **No** examples: "interesting but not actionable in teatree", "already implemented in `t3:loop`", "out of scope (consumer app pattern)."

Bias toward filing in borderline cases — duplicates are triaged later, missed ideas vanish.

### 7. Enqueue candidates (NOT file tickets)

For each relevant idea, build an `ArticleCandidate(url, title, summary, source)` and pass the batch through `enqueue_candidates_and_notify(...)`. The gate writes one `PendingArticleSuggestion` per new URL and DMs the user the batch. **Do not call `gh issue create` from this skill.** One suggestion per *idea*, not per article — three articles inspiring one idea collapse to one suggestion citing all three URLs in the summary.

### 8. Post the Slack DM

The batch DM is sent by the gate itself (`enqueue_candidates_and_notify`). When zero candidates are interesting, post a separate "0 items, 0 suggestions" DM directly so the honest signal still fires.

## Periodic Mode

When invoked with `--periodic` (from the teatree loop's periodic dispatcher, or cron):

- Non-interactive — no user confirmation prompts inside the scan itself.
- Candidates are queued via the ask gate (never auto-filed).
- DM listing the batch is automatic; ticket creation waits on `t3 manage news approve <id>`.
- Print a summary to stdout for log capture.

## Scheduling via the teatree main loop

The teatree main loop owns periodic dispatch. The `ScanningNewsScanner` (in `teatree.loop.scanners.scanning_news`) fires once every 24 hours by default and queues a `scanning_news` Task; the dispatcher then routes it to this skill via the `_SUBAGENT_BY_PHASE` table in `loop_dispatch.py`.

Tune the cadence or disable the scanner via `[teatree]` in `~/.teatree.toml`:

```toml
[teatree]
scanning_news_disabled = false
scanning_news_skill = "scanning-news"
scanning_news_cadence_hours = 24
```

If the loop scanner is not yet wired on an older install, the fallback path is documented in `references/cron-fallback.md` (same shape as `t3:followup`'s cron block).

## Rules

- Route every candidate through `enqueue_candidates_and_notify` — never `gh issue create` directly.
- One suggestion per idea.
- The gate labels every approved issue `from-news-scan` automatically.
- Use the workspace clock; never trust a date interpolated by an upstream caller.
- No AI signature on posts.

## Related skills

- `t3:followup` — daily-routine pattern; its cron fallback is the same shape as this skill's.
- `t3:contribute` — pushes improvements upstream; may pick up issues this skill files.
- `t3:teatree-plan` — board reorder; new issues enter the standard prioritization flow.

---

*If this skill was truncated during context compression, re-read it from disk before continuing the scan.*
