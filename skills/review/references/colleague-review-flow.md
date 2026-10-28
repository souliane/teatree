# Colleague review workflow

**Step -1 — Own PR vs External PR:**

When the PR under review belongs to the **user themselves**, do NOT post review comments. Instead, **implement the fixes directly** on the branch — commit and push. Present findings to the user as a summary of what you fixed, not as review comments to post. The user is asking you to take over and improve their code, not to leave notes for themselves.

**Step 0 — Gather Ticket Context:**

Before reading any code, fetch the referenced ticket/issue to understand the *intended* behavior. Extract the ticket URL from the PR title/description, then fetch it via the `mcp__teatree__github_issue` / `mcp__teatree__gitlab_issue` MCP tool — falling back to the platform CLI's own issue-view command when the MCP server is not connected. Fetch every attached spec and linked external requirement too; attachments are the authoritative spec, and an author's docstring summarising them is not a substitute.

**Hard rule — refuse blind reviews.** If a ticket references a spec attachment or external requirements document you cannot retrieve, **STOP**. Do not post review notes. Report back to the user: which document you could not fetch, what you tried, and what access is needed. An overlay MAY declare specific sources out of scope (a partner portal behind SSO); honour those exceptions. For anything else, a review with missing spec context is not a review — it is guessing, and guessing attached to the user's account damages the author's trust.

**The reviewer does the verification, not the author (Non-Negotiable).** Every comment goes out under the user's name, so one that boils down to "I'm unsure, please confirm" makes the user look like they do not know their own codebase. If a draft comment names a file, function, schema, enum, or downstream caller reachable from the local checkout, open it and read it before posting. "Worth checking `foo.py`" is not a review comment — it is the reviewer outsourcing their job. Either the file says the code is wrong (post a verified finding) or it says the code is fine (post nothing).

The rest of steps 0 through 0h — the attachment-fetching recipes and annotation reading, reviewing all commits individually, discussing before posting, the full investigation ladder behind the rule above, not policing another author's title format, the overlay's auto-close policy, cross-service verification, and choosing PR over chat as the venue — are in [`skills/review/references/giving-review-investigation.md`](giving-review-investigation.md).

**Step 1 — Structured Review Checklist:**

1. **Correctness** — does the code do what the ticket requires? Are all acceptance criteria met? When a change tightens a public contract or a shared predicate (e.g., serializer field becomes required, API parameter becomes mandatory), trace all callers — the change affects every flow that uses that interface, not just the one the ticket describes.
2. **Completeness** — are there missing production code changes that the tests assume? Do test expectation changes have matching implementation changes?
3. **Style** — follows project conventions?
4. **Tests** — adequate coverage of new behavior?
5. **Safety** — no security issues, no data loss risks? For shared mutable state (a registry/cache file, a row touched by concurrent processes), the **whole read→decide→write must be inside one lock/transaction** — a flock (or DB lock) that guards only the write still allows a lost-update / double-claim TOCTOU when two processes both read the old value, decide independently, and write in turn. A docstring or BLUEPRINT claim that writes "cannot lose a read-modify-write update" is false unless the read is inside the same critical section as the write; flag the mismatch.
6. **Migrations** — reversible? data-safe? performance-safe?
7. **Scope** — are unrelated changes bundled in? Flag only if genuinely unrelated; small related fixes alongside the main change are normal practice.
8. **PR metadata** — title and description comply with the overlay's commit message format? If the overlay provides `validate_pr()`, run it programmatically rather than checking by eye.

**Step 2 — Review Tone & Formatting:**

Follow the [Google Engineering Practices — Code Review Standard](https://google.github.io/eng-practices/review/reviewer/standard.html): approve if the CL improves overall code health, even if it isn't perfect. Don't block on style preferences or theoretical improvements. The bar is "does this improve the codebase?" — not "is this how I would have written it?" That governs the **verdict**, not the record: a style preference is not a reason to `hold`, and it is still worth recording as a low-severity finding in the verdict envelope.

Comments are posted under the user's name. They must sound like a **real human colleague** wrote them — not an AI, not a linter, not a manager.

**Verification belongs to the reviewer, not the author:**

Before posting a concern, open the relevant file and verify it yourself. Comments like "worth checking" or "please confirm" push verification work onto the author when the reviewer has the same codebase access. Grep enums, read migrations, check sibling repos — silence when the code is correct.

Speculative questions ("is this correct?", "could this cause issues?") without evidence waste the author's time. If unsure, investigate first — a concern backed by evidence is useful; a guess is noise.

**Voice & attitude:**

- **Be the best colleague.** Helpful, curious, humble. Happy to teach, never to humiliate. You're a peer who genuinely wants the code (and the author) to succeed.
- **Never parent.** Don't lecture, don't explain things the author obviously knows. If you're providing context, frame it as "in case it helps" or "I think this might…" — not "you should be aware that…".
- **Be collegial.** Phrase observations as questions or suggestions, not orders. "Would it make sense to…?" beats "You must…".
- **Assume good intent.** A reverted line is more likely an accidental rebase artifact than carelessness. Frame it that way.
- **Acknowledge what's good.** If the approach is sound, say so briefly before raising issues.
- **Scale severity to impact — and to the venue.** A missing production code change that breaks tests is critical; a minor style nit is not. On a merged or already-approved MR nothing is left to block: raise findings on the ticket that will carry the fix, and post anything left on the MR as a `Nit:`.
- Separate tickets/PRs are not needed for minor scope additions. A small related fix alongside the main change is normal — only raise scope if genuinely orthogonal work is smuggled in.

**Formatting rules:**

- **Concise, bullet-form, no prose (directive #4).** A review is findings, not an essay. Lead with the finding; skip the preamble and the summary of what the diff does (the author wrote it). One point per bullet. No "Overall this looks great, however…" wind-up, no restating the PR description. Be RIGHT but concise — trim words, never the evidence that makes a finding actionable. The shape/bloat gates below enforce this structurally on colleague MRs.
- **Single terse inline finding on a colleague MR.** On a colleague's MR (the MR's author is not your identity), each posted finding is one inline comment anchored on the motivating file:line — use `mcp__teatree__review_post_comment` first, with `t3 review post-comment <repo> <mr> "<finding> — <pointer>" --file <path> --line <N>` as the CLI fallback (the anchor flag is `--file`, never `--path`): the finding plus one concrete pointer, targeting at most ~300 characters. The posted body is either a plain finding or starts with the literal `Nit:`; never prefix it with HIGH/MED/LOW or blocker/major/minor taxonomy. No LGTM notes, and no MR-level verdict note beside inline findings; the evidence chain goes to the owner in chat. Own-MR reviews are exempt. Full text: `skills/review/references/posting-review-comments.md`.
- **A review comment is about the diff, not the tracker.** Keep project chatter out of the comment body — no `@handle` stakeholder mentions, no Slack-thread timestamps, no "ping the author / sync with the team / discuss in standup" coordination. State the finding on the code. A *bare* `tracked at #1234` non-blocker pointer is fine (it adds genuine context); it is the chatter directive wrapped around the id that bloats. Enforced structurally by the comment-bloat gate in `src/teatree/cli/review/bloat_gate.py` (souliane/teatree#2663): a chatter-laden body is refused before any GitLab API call. `--allow-bloat` is the per-call escape for a genuinely load-bearing reference. The note-length dimension stays with the shape gate above; this gate is orthogonal.
- **Prefix trivial nits.** When a comment is nitpicking (style, naming, minor preference), prefix it with bare `Nit:`. Post a nontrivial non-blocking finding as a plain finding; both can accompany approval.
- **Backticks for code.** Always wrap code symbols, class names, method names, variable names, file paths, and CLI commands in backticks (`` ` ``).
- **Use suggestion blocks for concrete code changes.** When you have a specific replacement in mind, use the platform's suggestion feature (` ```suggestion ` fenced block on both GitLab and GitHub) so the author can accept with one click. GitLab supports `:-N+M` to expand the range. Combine explanation text **before** the suggestion block.
- **Readable structure for longer comments.** Use empty lines to separate distinct sections (problem, suggestion, example). Within a section, use line breaks between sentences (without empty lines) to keep things scannable. Short comments stay on one line — don't over-structure a one-liner.
- **No walls of text.** If a comment needs more than ~5 lines, break it up visually. Paragraphs, not monoliths.

**Author-Marked TODO/FIXME — Never a Blocker (Non-Negotiable):**

A `// TODO`, `# TODO`, `/* TODO */`, `// FIXME`, `# FIXME`, `// XXX`, `# XXX`, `// HACK`, `# HACK` marker on an added line — or the phrases "not in this MR", "follow-up", "deferred", "implement later", "out of scope" — is the **author explicitly documenting that the work is deferred**. NEVER post a blocker-shaped (REQUEST_CHANGES) comment anchored to (or within ±3 lines of) such a marker. The strongest verdict allowed is a non-blocker comment, and only when it adds genuinely new context (e.g. "tracked at [#NNN]") — not re-stating what the author already said.

`mcp__teatree__review_post_comment` (and its fallback `t3 review post-comment`) enforce this deterministically via `src/teatree/cli/review/todo_gate.py` (souliane/teatree#1186): a blocker-shaped body anchored on a TODO-adjacent line is REFUSED with a clear error before any GitLab API call. If you genuinely believe the TODO must be addressed in THIS MR (rare — the author knows their scope), STOP and surface to the user — never post on their identity.

Failure mode this prevents: re-asking a colleague to do work they have explicitly deferred makes the reviewer (and the user, whose identity posts on-behalf) look unable to read code.

**Scope — this is a reviewer rule, not a licence to leave one.** It governs how you treat *another author's* deferral on *their* MR: their scope decision is theirs, and re-litigating it in a blocker is the failure above. It says nothing about the work you author yourself, where `AGENTS.md` First Principle 8 forbids leaving a `TODO`/"follow-up"/"out of scope" marker at all — self-review catches it before the diff leaves your hands. The two never collide, because they never apply to the same author.

**Step 3 — Post Draft Review Comments (babysit tier):**

The babysit-tier draft flow — the pre-flight read of existing discussions, the `mcp__teatree__review_post_comment` call and its `t3 review post-comment` / `--body-file` / `--live` fallback recipes, the anchoring pre-flight and post-flight checks, the collapsed-diff workaround, and the publish/delete subcommands — is in [`skills/review/references/posting-review-comments.md`](posting-review-comments.md).
