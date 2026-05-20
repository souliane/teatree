# Fetch Prompts — read when fetching TLDR or The Rundown AI editions

WebFetch summarises through a small model that drifts on vague prompts. Use these exact prompts. Do not paraphrase.

## TLDR AI edition

URL: `https://tldr.tech/ai/<YYYY-MM-DD>`

Prompt:

```text
Return this page as a single TLDR AI newsletter edition, NOT an aggregator summary.

Required output:
1. Issue date — verbatim from the page header (format YYYY-MM-DD).
2. Section headings as printed (e.g., "Big Tech & Startups", "Science & Futuristic Technology", "Programming, Design & Data Science", "Miscellaneous", "Quick Links").
3. ONLY stories that appear in this single issue (typically 5–9 stories total).
4. For each story: title, 1–2 sentence brief, outbound URL, read time.

Constraints (must follow):
- Do NOT aggregate stories from other TLDR tracks (Tech, Dev, Product, IT, Marketing, Design, Infosec, Crypto, Founders, DevOps, Data, Fintech).
- Do NOT include sections labelled with the names of other tracks.
- If this page is an aggregator/index and not a single issue, say so explicitly with the line: "AGGREGATOR PAGE DETECTED — not a single issue."
- If the issue date in the header does not match the date in the URL, say so explicitly with the line: "DATE MISMATCH — header date is <X>, URL date is <Y>."
```

After receiving the result:

1. Look for the `AGGREGATOR PAGE DETECTED` sentinel. If present → re-fetch with the prompt above and append: "IMPORTANT: previous fetch returned the aggregator. Return ONLY the single-issue stories listed under the named sections." If second attempt still aggregator → mark TLDR AI as fetch-failed.
2. Look for the `DATE MISMATCH` sentinel. If present → use the header date as the canonical edition date and record it for the DM. Do not silently assume the URL date.

## The Rundown AI edition

URL discovery: start from `https://www.therundown.ai/` or `https://www.therundown.ai/archive`.

Prompt for landing/archive:

```text
List the most recent posts on this page. For each, return the post date (YYYY-MM-DD), title, and absolute outbound URL. Return the top 3 most recent.
```

Then fetch the post URL matching today's date with this prompt:

```text
Return this Rundown AI daily edition as structured content.

Required output:
1. Post date — verbatim from the page header (format YYYY-MM-DD).
2. Top story / lead — title + 2–3 sentence summary + outbound URL.
3. The day's numbered items — for each: title, 1–2 sentence brief, outbound URL.
4. Skip ads, sponsorship sections, "in partnership with" blocks, and signup CTAs.

Constraint: if the post date does not match the date the user expected, say so explicitly with the line: "DATE MISMATCH — post date is <X>, expected <Y>."
```

Apply the same `DATE MISMATCH` recovery logic as for TLDR AI.

## Failure paths

- Fetch returns 404 → source not yet published; use latest published or mark as fetch-failed.
- Fetch returns aggregator after retry → mark as fetch-failed; surface in the DM as `<source>: fetch failed (aggregator)`.
- Fetch returns wrong date after retry → use the actual date (record it for the DM); do not pretend the date was today's.
