---
name: scanning-news
description: Scans today's TLDR AI and The Rundown AI editions for ideas that could improve teatree, fetches the full article for promising items, and hands each concrete t3-improvement candidate back through the result envelope's article_suggestions field. The loop queues each behind the ask-gate (PendingArticleSuggestion) for per-article user approval before any souliane/teatree issue is filed, and DMs the batch to the user. Use when user says "scan news", "scanning news", "ai newsletter scan", "improvement scan", "tldr scan", or runs the daily improvement routine.
compatibility: macOS/Linux, WebFetch. Runs shell-denied — no git/gh/CLI.
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
3. **Never auto-create issues (#1391).** When the ask-gate is on (`ask_before_creating_news_tickets`, default true — signalled by the `ASK-GATE` marker in the task directive), you RETURN each candidate in the envelope and the loop queues it as a `PendingArticleSuggestion` for user approval. Default is no-op: no approval, no ticket. See Workflow step 7.
4. **Dedupe is by source URL, server-side.** The loop's recorder is idempotent by URL hash, so returning the same article on a later scan never enqueues a duplicate. You run shell-denied, so you do NOT `gh issue list` against filed issues — the ask-gate means nothing is filed without approval, so cross-issue dedupe is not your job.
5. **No AI signature** on anything (per `t3:rules`).
6. **Never invent stories.** If a source fetch fails, omit that source and note the failure in the envelope `summary`.
7. **Every cited URL must resolve (PR-15).** A fabricated or 404 article link is dropped, not queued. The loop's recorder probes each URL via `teatree.verification.url_check.check_url` and DROPS an `UNRESOLVED` (2xx/3xx = ok, 4xx/5xx = drop) one, so a hallucinated citation never reaches the backlog. A `NETWORK_ERROR` (teatree could not tell) records the candidate anyway — a transient failure never drops a real article. Do the URL-presence pass yourself too (step 6b) and drop obvious 404s before returning, so you never hand back a known-dead link.

## How candidates are recorded — envelope hand-back (no shell)

You run **shell-denied**: do NOT run `manage.py`, `gh`, or `t3`. You RETURN every
t3-improvement candidate in the result envelope's `article_suggestions` field, and
the loop persists and surfaces them for you:

- **Record + dedupe:** each `{title, url, rationale}` you return becomes one
  `PENDING` `PendingArticleSuggestion`, idempotent by source-URL hash.
- **URL verification:** the recorder drops an `UNRESOLVED` URL (Non-Negotiable 7).
- **Batch-approval DM:** after it records the batch, the loop DMs the user ONE
  approval message listing each candidate. You do NOT post any Slack DM yourself.
- **Filing:** only the user, on approval, files a `from-news-scan` issue — never
  the scan.

Return shape:

```json
{
  "summary": "<one-line scan summary, incl. any source that failed to fetch>",
  "article_suggestions": [
    {"title": "<title>", "url": "<article URL>", "rationale": "<why it could improve t3>"}
  ]
}
```

## Workflow

### 1. Resolve today's date

Read the workspace clock; format `YYYY-MM-DD`. Use this string consistently for both fetches and in the envelope `summary`.

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

### 6b. Verify each cited URL resolves (PR-15)

Before returning any candidate, confirm its article URL actually exists — a triaged story whose link was hallucinated or has 404'd is not a real source. The loop's recorder enforces this (it drops an `UNRESOLVED` URL), but do the pass explicitly so you drop known-dead links yourself and note the count:

- For each surviving candidate, check the cited URL resolves (a `HEAD` / ranged `GET` — the same probe `check_url` runs). A 2xx/3xx keeps it; a 4xx/5xx drops it — do not return it.
- Note `stories dropped (url unresolved): N` in the envelope `summary`.
- A network error (timeout / DNS) is NOT a drop: teatree could not verify it, so keep the candidate and return it; never drop a real story on a transient failure.

### 7. Return candidates in the envelope (#1391) — never auto-file

**Never auto-create issues.** Auto-filing every "could improve t3" article is backlog pollution — it confuses "I read this" with "we should build this". You hand each candidate back behind the ask-gate and the user decides.

The dispatched task's `execution_reason` carries the gate directive (the `ASK-GATE` marker) when `ask_before_creating_news_tickets` is on (the default). Whether or not the marker is present, you always return candidates the same way — you cannot file issues (shell-denied), so filing is never your job:

1. Put every surviving candidate (after the URL pass) in the envelope's
   `article_suggestions` list — one `{title, url, rationale}` per candidate. The
   loop records each as a `PENDING` `PendingArticleSuggestion` (idempotent by URL).
2. The loop DMs the user the batch for approval — one line per candidate.
3. **Default = no-op.** With no approval, the row stays `PENDING` and no issue is filed.
4. On approval, the user (not the scan) files the `from-news-scan` issue. One issue
   per *idea*, not per article — three articles inspiring one idea collapse to one
   issue citing all three URLs (`references/ticket-template.md` is the body template
   the user applies).

### 8. Hand back the envelope

You do NOT post a Slack DM — the loop posts the batch-approval DM after it records your candidates. Return the envelope (see "How candidates are recorded" above) with a one-line `summary` and every candidate in `article_suggestions`. Fold the step-6b drop count into the `summary`.

## Periodic Mode

When invoked with `--periodic` (from the teatree loop's periodic dispatcher, or cron):

- Non-interactive — no blocking confirmation prompts mid-scan.
- **Filing is gated, never automatic.** Return `article_suggestions`; the loop queues them behind the ask-gate and DMs the batch. No approval → no ticket.
- The loop posts the DM — you do not.
- Print a summary to stdout for log capture.

## Scheduling via the teatree main loop

The teatree main loop owns periodic dispatch. The `ScanningNewsScanner` (in `teatree.loop.scanners.scanning_news`) fires once every 24 hours by default and queues a `scanning_news` Task; the dispatcher then routes it to this skill via `subagent_for_phase` (the canonical `SUBAGENT_BY_PHASE` map in `teatree.core.modelkit.phases`), which `loop_dispatch.py` consults.

These knobs are DB-home — set them in the `ConfigSetting` store (add `--overlay <name>` for a per-overlay value):

```bash
t3 <overlay> config_setting set scanning_news_disabled false
t3 <overlay> config_setting set scanning_news_skill '"scanning-news"'
t3 <overlay> config_setting set scanning_news_cadence_hours 24
# Ask-gate (#1391): when true (default), returned candidates are queued as
# PendingArticleSuggestion rows and issues are filed only on user approval —
# never auto-created. Set false to opt back into direct filing.
t3 <overlay> config_setting set ask_before_creating_news_tickets true
```

If the loop scanner is not yet wired on an older install, the fallback path is documented in `references/cron-fallback.md` (same shape as `t3:followup`'s cron block).

## Rules

- Never auto-create issues — return candidates in the envelope; the loop queues them and files only on user approval (#1391).
- You run shell-denied: no `manage.py`, no `gh`, no `t3`, no Slack DM — hand everything back through `article_suggestions`.
- Dedupe is server-side, by URL.
- One issue per idea (the user files it, `from-news-scan` label).
- Use the workspace clock; never trust a date interpolated by an upstream caller.
- No AI signature anywhere.

## Related skills

- `t3:followup` — daily-routine pattern; its cron fallback is the same shape as this skill's.
- `t3:contribute` — pushes improvements upstream; may pick up issues filed from this skill's approved candidates.
- `t3:sweeping-tickets` — consolidation/triage flow; new issues get folded into a tracking epic or kept standalone on the next sweep.

---

*If this skill was truncated during context compression, re-read it from disk before continuing the scan.*
