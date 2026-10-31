---
name: scanning-news
description: Scans today's AI news for scoped teatree improvements, checks backlog fit and duplicates, and returns article decisions for the server to settle and file through the issue facade. Use when user says "scan news", "scanning news", "ai newsletter scan", "improvement scan", "tldr scan", or runs the daily improvement routine.
compatibility: Linux, WebFetch. Runs shell-denied — no git/gh/CLI.
requires:
  - workspace
  - rules
  - platforms
metadata:
  version: 0.0.1
  last_research_date: "2026-05-20"
---

# t3:scanning-news — Mine AI Newsletters for t3 Improvements

## Non-Negotiables

1. **Date-of-edition verification is mandatory.** Before processing any newsletter, read the issue date verbatim from the page header and compare it to today's workspace date. If they do not match, do not proceed — either re-fetch with a stricter prompt, fall back to the latest published edition, or abort that source. Never silently treat yesterday's edition as today's.
2. **Aggregator-detection is mandatory.** TLDR's `tldr.tech/<track>/<date>` page can return a kitchen-sink summary that lists stories from every TLDR track (Tech, AI, Dev, Product, IT, Marketing, Design, Infosec, Crypto, Founders, DevOps, Data, Fintech). The real per-issue newsletter is 5–9 stories grouped under named sections. If the fetch returns >15 stories or contains track-names that are not the requested track, the prompt drifted — re-fetch with the stricter prompt before continuing.
3. **Decide before filing.** Return one `file` or `drop` decision per candidate. File only a concrete scoped idea with a checked open backlog and a clean base; name an open duplicate so the server extends it. The server files through the issue facade and settles each row in the same step.
4. **Dedupe is by source URL, server-side.** The recorder is idempotent by URL hash, so a later scan does not repeat the decision or create another issue. Inspect the open issue backlog before declaring that a new idea has no duplicate.
5. **No AI signature** on anything (per `t3:rules`).
6. **Never invent stories.** If a source fetch fails, omit that source and note the failure in the envelope `summary`.
7. **Every cited URL must resolve (PR-15).** A fabricated or 404 article link is dropped, not queued. The loop's recorder probes each URL via `teatree.verification.url_check.check_url` and DROPS an `UNRESOLVED` (2xx/3xx = ok, 4xx/5xx = drop) one, so a hallucinated citation never reaches the backlog. A `NETWORK_ERROR` (teatree could not tell) records the candidate anyway — a transient failure never drops a real article. Do the URL-presence pass yourself too (step 6b) and drop obvious 404s before returning, so you never hand back a known-dead link.

## Sources

The merged source table lives in `src/teatree/core/news_sources.py` and reaches you as
the `SOURCES:` block in the task directive — never hard-code a feed list here. It is the
union of the loop's original edition-dated AI newsletters and the wider press-review feed
set, deduped by normalised feed URL (an overlapping source keeps the edition-dated row).
Rolling dedupe across days needs no state file: the loop's recorder is idempotent by
source URL.

## How candidates are recorded — envelope hand-back (no shell)

You run **shell-denied**: do NOT run `manage.py`, `gh`, or `t3`. You RETURN every
t3-improvement candidate in the result envelope's `article_suggestions` field, and
the loop persists and surfaces them for you:

- **Record + dedupe:** each article becomes a settled `PendingArticleSuggestion`, idempotent by source-URL hash.
- **URL verification:** the recorder drops an `UNRESOLVED` URL (Non-Negotiable 7).
- **Slack press-review digest:** after settling new candidates, the loop delivers
  one rendered digest with a clickable link per candidate and code symbols and file
  paths in monospace (see `references/slack-format.md`). Deduped per task; an
  empty scan produces no digest.
- **Filing:** the server creates or extends a `from-news-scan` issue only for an evidenced `file` decision.

Return shape:

```json
{
  "summary": "<one-line scan summary, incl. any source that failed to fetch>",
  "article_suggestions": [
    {"title": "<title>", "url": "<article URL>", "rationale": "<why it could improve t3>",
     "action": "file", "decision_reason": "<why>", "backlog_checked": true,
     "base_red": false, "duplicate_of": "", "ticket_title": "<scoped change>",
     "ticket_body": "<scope and evidence>"}
  ]
}
```

## Workflow

### 1. Resolve today's date

Read the workspace clock; format `YYYY-MM-DD`. Use this string consistently for both fetches and in the envelope `summary`.

### 2. Fetch every source in the dispatch directive

The task's `execution_reason` carries a `SOURCES:` block — the merged table from
`teatree.core.news_sources` (the two edition-dated AI newsletters the loop has always
read, plus the wider press-review feed set: web-dev / devops / infosec / Python
newsletters and Hacker News). Fetch every line. Each names the bucket, the label, the
URL, the item cap, and whether it is edition-dated.

**Only an `edition-dated` source gets the date gate** (Non-Negotiable 1). A rolling RSS
feed has no edition to verify; applying the gate to one would drop every item.

For `TLDR AI`, the dated page is `https://tldr.tech/ai/<YYYY-MM-DD>` — use the WebFetch
prompt in `references/fetch-prompts.md` and apply both gates (Non-Negotiables 1 + 2). On
mismatch:

- Edition date < today's date → today's issue is not yet published (TLDR drops in US morning). Use the latest published edition; record the actual issue date for the DM.
- Aggregator detected → re-fetch with the stricter prompt; if still aggregator, treat as fetch failure.

For `The Rundown AI` there is no clean dated URL pattern: start from the archive URL in
the directive, discover today's post, then fetch it. Same gates.

Breadth does not replace the filter. The merged sources widen what is READ; step 4's
teatree-relevance triage still decides what survives, and it is the reason this loop
exists at all.

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

Drop a borderline idea until it has a scoped improvement and a checked base; an unverified filing clutters the backlog.

### 6b. Verify each cited URL resolves (PR-15)

Before returning any candidate, confirm its article URL actually exists — a triaged story whose link was hallucinated or has 404'd is not a real source. The loop's recorder enforces this (it drops an `UNRESOLVED` URL), but do the pass explicitly so you drop known-dead links yourself and note the count:

- For each surviving candidate, check the cited URL resolves (a `HEAD` / ranged `GET` — the same probe `check_url` runs). A 2xx/3xx keeps it; a 4xx/5xx drops it — do not return it.
- Note `stories dropped (url unresolved): N` in the envelope `summary`.
- A network error (timeout / DNS) is NOT a drop: teatree could not verify it, so keep the candidate and return it; never drop a real story on a transient failure.

### 7. Return a decision per candidate

You cannot file issues from this shell-denied phase. For each article, return `action: file` only after checking the live open issue backlog, reading the default branch's live CI state to confirm the proposed check is not already red, and drafting a scoped ticket. If either read fails, drop the candidate with the failed read named as the reason. Set `backlog_checked: true`, `base_red: false`, and include `ticket_title` and `ticket_body`. If an open issue already covers the idea, set `duplicate_of` to its URL; the server extends that issue. Otherwise set `duplicate_of` to an empty string; the server files through the shared issue facade with `from-news-scan` and `needs-triage`. For every other article, return `action: drop` with `decision_reason`.

The server records each URL once, settles the row immediately, and sends one press-review digest. A failed forge write rolls back that row for a later retry. One issue describes one idea; fold several articles about the same idea into one ticket draft.

### 8. Hand back the envelope

You do NOT post a Slack DM — the loop delivers the **press-review digest** after it
records your candidates. Return the envelope (see "How candidates are recorded"
above) with a one-line `summary` and every candidate in
`article_suggestions`. Fold the step-6b drop count into the `summary`.

Write each `rationale` so it renders well in the digest: name real file paths and
symbols (`src/teatree/loop/tick.py`, `teatree.core.tasks.execute_task`) rather than paraphrasing
them. `teatree.core.news_digest` renders every citation as a clickable Slack link and
puts paths/symbols in monospace, per the owner's standing Slack-formatting directive.

## Periodic Mode

When invoked with `--periodic` (from the teatree loop's periodic dispatcher, or cron):

- Non-interactive — no blocking confirmation prompts mid-scan.
- Return `article_suggestions`; the server settles the decisions and delivers the digest.
- The loop delivers the digest — you do not.
- Print a summary to stdout for log capture.

## Scheduling via the teatree main loop

The teatree main loop owns periodic dispatch. The `ScanningNewsScanner` (in `teatree.loop.scanners.scanning_news`) fires once every 24 hours by default and queues a `scanning_news` Task; the dispatcher then routes it to this skill via `subagent_for_phase` (the canonical `SUBAGENT_BY_PHASE` map in `teatree.core.modelkit.phases`), which `loop_dispatch.py` consults.

These knobs are DB-home — set them in the `ConfigSetting` store with the `mcp__teatree__config_setting_set` MCP tool (it accepts these keys; pass `overlay` for a per-overlay value), or the CLI below when the MCP server isn't connected (add `--overlay <name>` for a per-overlay value):

```bash
t3 <overlay> config_setting set scanning_news_skill '"scanning-news"'
t3 <overlay> config_setting set scanning_news_cadence_hours 24
```

If the loop scanner is not yet wired on an older install, the fallback path is documented in `references/cron-fallback.md` (same shape as `t3:checking`'s cron block).

## Rules

- Return a decision for each candidate; the server handles filing and row settlement.
- You run shell-denied: no `manage.py`, no `gh`, no `t3`, no Slack DM — hand everything back through `article_suggestions`.
- Dedupe is server-side, by URL.
- One issue per idea (`from-news-scan` label).
- Use the workspace clock; never trust a date interpolated by an upstream caller.
- No AI signature anywhere.

## Related skills

- `t3:checking` — daily-routine pattern; its cron fallback is the same shape as this skill's.
- `t3:contribute` — pushes improvements upstream; may pick up issues filed from this skill's scoped candidates.
- `t3:sweeping-tickets` — the daily grouping sweep; new issues get folded into an existing host or kept standalone on the next pass.

---

*If this skill was truncated during context compression, re-read it from disk before continuing the scan.*
