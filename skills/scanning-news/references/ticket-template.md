# Ticket Body Template — read when filing a `from-news-scan` issue

## Title

A short imperative — what to change. Examples:

- "Adopt evaluation-harness pattern from <article author>"
- "Add `loop schedule-periodic` CLI for daily skill dispatch"
- "Replace claim-next polling with event-driven dispatch (see <article>)"

Avoid titles that name the source instead of the change ("Article on evaluation harnesses" is wrong; "Adopt evaluation-harness pattern" is right).

## Body

```markdown
**Source:** <article title> ([link](<article URL>)) via <TLDR AI | The Rundown AI> <YYYY-MM-DD>.

**Why it could improve t3:**
<2–4 sentences — the concrete change and the expected benefit.>

**Technique / pattern referenced:**
- <bullet 1 — key idea>
- <bullet 2 — key idea>
- <optional bullet 3>

**Scope hint:**
<rough estimate — `skills/`, `src/teatree/loop/`, etc. — keep it short.>

---
Filed by `t3:scanning-news` on <YYYY-MM-DD>.
```

## Multi-source case

If multiple articles inspire the same idea, cite all sources in `**Source:**`:

```markdown
**Source:**
- <article 1 title> ([link](<URL 1>)) via TLDR AI <date>.
- <article 2 title> ([link](<URL 2>)) via The Rundown AI <date>.
```

## Labels

Always apply `from-news-scan`. Add any additional labels that match the change area (e.g., `skills`, `loop`, `ci`) only when obvious from the scope hint.
