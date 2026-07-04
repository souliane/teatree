---
name: scanning-news
description: Scans today's TLDR AI and The Rundown AI editions for ideas that could improve teatree, fetches the full article for promising items, queues each concrete t3-improvement candidate behind an ask-gate (PendingArticleSuggestion) for per-article user approval before any souliane/teatree issue is filed, and posts a terse Slack DM summary. Use when user says "scan news", "scanning news", "ai newsletter scan", "improvement scan", "tldr scan", or runs the daily improvement routine.
compatibility: macOS/Linux, git, gh CLI, Slack integration.
requires:
  - workspace
  - rules
  - platforms
metadata:
  version: 0.0.1
  subagent_safe: false
  last_research_date: "2026-05-20"
---

# t3:scanning-news — Mine AI Newsletters for t3 Improvements

## Non-Negotiables

1. **Date-of-edition verification is mandatory.** Before processing any newsletter, read the issue date verbatim from the page header and compare it to today's workspace date. If they do not match, do not proceed — either re-fetch with a stricter prompt, fall back to the latest published edition, or abort that source. Never silently treat yesterday's edition as today's.
2. **Aggregator-detection is mandatory.** TLDR's `tldr.tech/<track>/<date>` page can return a kitchen-sink summary that lists stories from every TLDR track (Tech, AI, Dev, Product, IT, Marketing, Design, Infosec, Crypto, Founders, DevOps, Data, Fintech). The real per-issue newsletter is 5–9 stories grouped under named sections. If the fetch returns >15 stories or contains track-names that are not the requested track, the prompt drifted — re-fetch with the stricter prompt before continuing.
3. **Never auto-create issues (#1391).** When the ask-gate is on (`ask_before_creating_news_tickets`, default true — signalled by the `ASK-GATE` marker in the task directive), record each candidate as a `PendingArticleSuggestion` and surface the batch for user approval. Default is no-op: no approval, no ticket. See Workflow step 7.
4. **Dedupe candidates by source URL** before recording. `record_candidate` is idempotent by URL hash; existing `from-news-scan` issues also cite the article URL in their body.
5. **No AI signature** on issues or DMs (per `t3:rules`).
6. **Never invent stories.** If a source fetch fails, omit that source from the DM and note the failure.

## Command Reference

```bash
# Record a candidate behind the ask-gate (idempotent by URL hash) — #1391.
# record_candidate is a Django ORM classmethod, so it must run through the
# Django entrypoint (manage.py shell -c) — a bare `python -c` raises
# ImproperlyConfigured because settings are not configured.
uv run python manage.py shell -c "from teatree.core.models import PendingArticleSuggestion as P; P.record_candidate(url='<article URL>', title='<title>', summary='<why>', overlay='t3-teatree')"

# Issue creation — only after the user approves the candidate
gh --repo souliane/teatree issue create --title "<title>" --body "<body>" --label "from-news-scan"

# Stamp the approval/rejection on the candidate row
uv run python manage.py shell -c "from teatree.core.models import PendingArticleSuggestion as P; r=P.objects.get(pk=<id>); r.approve(issue_url='<issue URL>')"
uv run python manage.py shell -c "from teatree.core.models import PendingArticleSuggestion as P; P.objects.get(pk=<id>).reject()"

# Dedupe check
gh --repo souliane/teatree issue list --label from-news-scan --state all --search "<article URL>"

# Slack DM to the user (teatree bot, self-DM → bot token).
# Body via stdin (`-`) avoids shell-quoting a multi-line message; the
# --idempotency-key is required (the helper enforces it). The DM to the
# user themselves is sanctioned and not gated by on_behalf_post_mode.
echo '<message>' | t3 teatree notify send - --idempotency-key "scanning-news:<YYYY-MM-DD>" --kind info
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

### 7. Queue candidates behind the ask-gate (#1391) — never auto-file

**The scanner must NOT auto-create issues.** Auto-filing every "could improve t3" article is backlog pollution — it confuses "I read this" with "we should build this". Instead, record each candidate behind the ask-gate and let the user decide.

The dispatched task's `execution_reason` carries the gate directive (the `ASK-GATE` marker) when `ask_before_creating_news_tickets` is on (the default). When you see it:

1. For each surviving candidate (after the dedupe check), record one `PendingArticleSuggestion` row — `record_candidate` is idempotent by source-URL hash, so a re-scan never enqueues a duplicate:

   ```bash
   uv run python manage.py shell -c "from teatree.core.models import PendingArticleSuggestion as P; P.record_candidate(url='<article URL>', title='<title>', summary='<why interesting>', overlay='t3-teatree')"
   ```

2. Surface the batch to the user (the Slack DM in step 8, or `AskUserQuestion` for a small batch). One line per candidate: title + why-interesting + URL.
3. **Default = no-op.** With no approval, the row stays `PENDING` and no issue is filed.
4. File an issue **only** for candidates the user approves — then record the approval:

   ```bash
   # On approval — file the issue, then stamp the row:
   gh --repo souliane/teatree issue create --title "<title>" --body "<body>" --label "from-news-scan"
   uv run python manage.py shell -c "from teatree.core.models import PendingArticleSuggestion as P; r=P.objects.get(pk=<id>); r.approve(issue_url='<issue URL>')"
   # On rejection — no issue, just stamp:
   uv run python manage.py shell -c "from teatree.core.models import PendingArticleSuggestion as P; P.objects.get(pk=<id>).reject()"
   ```

Use the body template in `references/ticket-template.md` for approved issues. One issue per *idea*, not per article — three articles inspiring one idea collapse to one issue citing all three URLs.

When the gate is explicitly opted out (`ask_before_creating_news_tickets = false`, no `ASK-GATE` marker in the directive), the dedupe gate alone guards filing and you may file approved-by-config directly — but the default and recommended posture is the ask-gate ON.

### 8. Post the Slack DM

Format defined in `references/slack-format.md`. Always post even when zero items are interesting — a "0 items, 0 candidates" DM is the honest signal that the scan ran. When candidates were queued, the DM is the approval surface: list each `PendingArticleSuggestion` so the user can approve or reject.

## Periodic Mode

When invoked with `--periodic` (from the teatree loop's periodic dispatcher, or cron):

- Non-interactive — no blocking confirmation prompts mid-scan.
- **Ticket filing is gated, never automatic.** Record `PendingArticleSuggestion` candidates and DM the batch for approval (step 7). No approval → no ticket.
- DM automatic.
- Print a summary to stdout for log capture.

## Scheduling via the teatree main loop

The teatree main loop owns periodic dispatch. The `ScanningNewsScanner` (in `teatree.loop.scanners.scanning_news`) fires once every 24 hours by default and queues a `scanning_news` Task; the dispatcher then routes it to this skill via the `_SUBAGENT_BY_PHASE` table in `loop_dispatch.py`.

These knobs are DB-home — set them in the `ConfigSetting` store (a value left in `~/.teatree.toml` is ignored on read; add `--overlay <name>` for a per-overlay value):

```bash
t3 <overlay> config_setting set scanning_news_disabled false
t3 <overlay> config_setting set scanning_news_skill '"scanning-news"'
t3 <overlay> config_setting set scanning_news_cadence_hours 24
# Ask-gate (#1391): when true (default), the skill records candidates as
# PendingArticleSuggestion rows and files issues only on user approval —
# it never auto-creates. Set false to opt back into direct filing.
t3 <overlay> config_setting set ask_before_creating_news_tickets true
```

If the loop scanner is not yet wired on an older install, the fallback path is documented in `references/cron-fallback.md` (same shape as `t3:followup`'s cron block).

## Rules

- Never auto-create issues — record `PendingArticleSuggestion` candidates and file only on user approval (#1391).
- Dedupe before recording.
- One issue per idea.
- Label every filed issue `from-news-scan`.
- Use the workspace clock; never trust a date interpolated by an upstream caller.
- No AI signature on posts.

## Related skills

- `t3:followup` — daily-routine pattern; its cron fallback is the same shape as this skill's.
- `t3:contribute` — pushes improvements upstream; may pick up issues this skill files.
- `t3:sweeping-tickets` — consolidation/triage flow; new issues get folded into a tracking epic or kept standalone on the next sweep.

---

*If this skill was truncated during context compression, re-read it from disk before continuing the scan.*
