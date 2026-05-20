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

1. **Date-of-edition verification is mandatory.** Before processing any newsletter, read the issue date verbatim from the page header and compare it to today's workspace date. If they do not match, do not proceed — either re-fetch with a stricter prompt, fall back to the latest published edition, or abort that source. Never silently treat yesterday's edition as today's.
2. **Aggregator-detection is mandatory.** TLDR's `tldr.tech/<track>/<date>` page can return a kitchen-sink summary that lists stories from every TLDR track (Tech, AI, Dev, Product, IT, Marketing, Design, Infosec, Crypto, Founders, DevOps, Data, Fintech). The real per-issue newsletter is 5–9 stories grouped under named sections. If the fetch returns >15 stories or contains track-names that are not the requested track, the prompt drifted — re-fetch with the stricter prompt before continuing.
3. **Dedupe tickets by source URL** before filing. Existing `from-news-scan` issues already cite the article URL in their body.
4. **No AI signature** on issues or DMs (per `t3:rules`).
5. **Never invent stories.** If a source fetch fails, omit that source from the DM and note the failure.

## Command Reference

```bash
# Issue creation
gh --repo souliane/teatree issue create --title "<title>" --body "<body>" --label "from-news-scan"

# Dedupe check
gh --repo souliane/teatree issue list --label from-news-scan --state all --search "<article URL>"

# Slack DM via overlay router (teatree bot)
python -c "from teatree.messaging import messaging_from_overlay; m = messaging_from_overlay(overlay_name='teatree'); m.dm_owner('<text>')"
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

### 7. File tickets

After the dedupe check returns no match, file the issue using the body template in `references/ticket-template.md`. One issue per *idea*, not per article — three articles inspiring one idea collapse to one issue citing all three URLs.

### 8. Post the Slack DM

Format defined in `references/slack-format.md`. Always post even when zero items are interesting — a "0 items, 0 tickets" DM is the honest signal that the scan ran.

## Periodic Mode

When invoked with `--periodic` (from the teatree loop's periodic dispatcher, or cron):

- Non-interactive — no user confirmation prompts.
- Ticket filing automatic (dedupe gate prevents spam).
- DM automatic.
- Print a summary to stdout for log capture.

## Scheduling via the teatree main loop

The teatree main loop owns periodic dispatch. Register `t3:scanning-news` as a daily task by adding it to the loop's periodic-task table. Until a dedicated CLI command lands for this, follow the fallback path documented in `references/cron-fallback.md` (same shape as `t3:followup`'s cron block).

## Rules

- Dedupe before filing.
- One issue per idea.
- Label every filed issue `from-news-scan`.
- Use the workspace clock; never trust a date interpolated by an upstream caller.
- No AI signature on posts.

## Related skills

- `t3:followup` — daily-routine pattern; its cron fallback is the same shape as this skill's.
- `t3:contribute` — pushes improvements upstream; may pick up issues this skill files.
- `t3:teatree-plan` — board reorder; new issues enter the standard prioritization flow.

---

*If this skill was truncated during context compression, re-read it from disk before continuing the scan.*
