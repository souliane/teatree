# Reporting — completion reports, terse output, and references

The full text of the `/t3:rules` sections on what a report says and how it names things: leading with the assigned-work status, terse TTS-ready turns, clickable references, inline titles, and id-namespace disambiguation. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict.

## Lead a Completion Report With the Assigned-Work Status

When reporting back on assigned work, the reader's first need is an unambiguous answer to **"is the assigned work done, and where is it?"** — deliverable status, branch/PR/HEAD, gate results. Out-of-scope observations, systemic findings, or follow-up recommendations surfaced along the way must be **clearly separated and subordinate**: a labelled trailing section, never positioned so they displace, precede, or read as a substitute for the deliverable status. A correct systemic analysis that buries the "done?" answer reads as "did the analysis instead of the work" — the coordinator concludes nothing shipped and spends a round-trip re-asking for what was already finished. Separate the two concerns physically; lead with the in-scope status every time.

**On a STANDING verified-green goal, LEAD with the blunt binary — a status report is a checkpoint, not the deliverable (do X, never Y).** When the work is a standing "make X verified-green" goal (the eval suite, the e2e suite) and X is NOT yet green with achievable work remaining, a status report must OPEN with the binary truth on each suite — **"evals green? NO. e2e green? NO."** — BEFORE any wins, and must keep the goal **explicitly open**. The recurring, critical drift this forbids: the agent does a chunk of work, foregrounds the wins (merged-PR counts, per-lane greens, "good progress"), surfaces a blocker, and ends the turn on a positive-framed status that READS as-if-done — so the goal stays unmet and the user has to re-prod for weeks. Surfacing a blocker is a checkpoint, not completion. The honest report is one of exactly two shapes, both leading with the binary: **keep driving** the next achievable fix, or **surface-and-hold** (name the specific blocker AND state the goal stays open). It is never a win-led wrap-up.

```text
# do X — LEAD with the binary on each suite, then wins, and keep the goal open:
#   "Eval suite green? NO — 3 scenarios still red. E2E green? NO — 2 specs still red.
#    Goal unmet, stays open. Wins: 3 PRs merged, 5 lanes green. Next: triage the first red."
# never Y — a win-led report that ends the turn as-if-done while the goal is unmet:
#   "Merged 3 PRs, 5 lanes green — good progress. Solid checkpoint, picking the rest up next time."
```

When the next driving action is to run the AI/trajectory eval suite, the canonical command is `t3 eval run`. Do not add the `teatree` group to it: that group owns deterministic tests (`t3 teatree run tests`), not evals. An invalid command does not keep the goal moving.

This must not be gameable by an `AskUserQuestion`-to-defer or a positive-framed partial report that ends the turn: while the goal is unmet and work remains, the only honest stop is actually-green OR a user-acknowledged external ceiling. Pinned by `standing_green_goal_keeps_driving_never_stops_done` (the keep-driving ACTION) and `verified_green_status_report_leads_binary_never_stops_as_done` (the report-LEAD text) in `evals/scenarios/rules.yaml`.

## Keep Turn Output Terse and TTS-Ready

Every turn response must be short enough to speak aloud without losing the listener. The whole turn output — not just a summary — should fit TTS comfortably.

**Required:**

- Lead with the answer or the action taken. The first sentence is the payload; context and reasoning follow only if necessary.
- One sentence per point. No long prose paragraphs.
- No decorative markdown (headers, horizontal rules, nested bullet trees, bold-for-structure) when speaking. Plain sentences work for speech; heading hierarchies do not.
- Suppress routine status noise. "N signals, N actions" and "still running" progress reports are not actionable → omit them unless something changed that the user must act on.
- Background work: report on completion or decision only, not on each in-progress tick.
- The only proactive user-DMs are mergeable customer MRs, blockers, and genuine asks — never routine status. A "everything green, still running" tick is not a DM; pinned by `evals/scenarios/slack_only_human_needed.yaml`.

**Anti-patterns:**

- A multi-paragraph narrative of what was done, what was found, and what comes next — when the answer is "done, here is the PR link".
- A section-headed summary where every item is restated twice (once as a heading, once as prose).
- A tick report that says "everything is fine" in 8 lines when silence would be correct.

**TTS cap:** If `t3 speak` is active (`[teatree.speak] local = "all"`), the per-turn text passed to `clean_for_speech` is capped at 600 characters. Write turns that fit without truncation by default — the cap is a hard backstop, not a target. A turn that requires aggressive truncation before it fits TTS was too verbose to start.

## Clickable References

Every PR, ticket, issue, or note reference — in markdown files, platform comments, **and** agent responses — must be a clickable markdown link.

- `[!5657](https://example.com/org/repo/-/merge_requests/5657)` — not `!5657`
- `[PROJ-1234](https://example.com/org/repo/-/issues/1234)` — not `PROJ-1234`

This applies everywhere: MR/PR descriptions, inline comments, test evidence, chat messages, and responses to the user. When you are handed the id **and** its URL, emit the markdown link — do X, never Y:

- **do:** `MR [!7551](https://git.example.com/acme/app/-/merge_requests/7551) is ready for review.`
- **never:** `MR !7551 is ready for review.` (a bare id the reader cannot click)

## Render the Title Inline, Never a Bare/Link-Only Id (Non-Negotiable)

Every surface that _lists_ a ticket/MR/PR/issue id must render the human-readable title inline — `#N (short ≤6-word title)` (or `[#N (short title)](url)` where a link applies) — so the reader knows _what_ `#N` is without opening it. A bare `#N`, or a clickable number next to no title, is the anti-pattern: the reader cannot tell one row from another. The title and the URL are two halves of one contract — the clickable-link rule above resolves the URL; this rule supplies the title.

- The single chokepoint is `teatree.core.ref_render.render_ref(label, *, title, url)` — every id-listing surface (loop-tick statusline, `/checking`, `/todos`, notify/standup recaps) formats through it so they read identically. Do not hand-roll the `#N (title)` shape per call site.
- A row whose ticket has no known title degrades to the plain id (still clickable when a URL applies), never an empty `()`.
- This is the _listing_ rule; the namespace-disambiguation rule below governs _which_ id token (`TODO-<n>` vs `<repo>#<n>`) the `label` is. They compose: a todo line is `task TODO-<id> (ticket #<n> (<title>) …)`.

## ID Namespace Disambiguation (Non-Negotiable)

Id references must be namespace-qualified — they are never bare. A harness/teatree **task id** and a forge **issue/ticket/PR id** are different namespaces that both number from ~1, so a bare `#<n>` standing next to another bare `#<n>` is undecidable: an agent cannot tell whether `task #5` next to `ticket #5` are the same thing or two unrelated objects, and may resolve a task id against the issue tracker and act on the wrong object.

- **Harness/teatree task ids** render as `TODO-<n>` (e.g. `TODO-7`) — never `task #<n>` or bare `#<n>`. This is `Task` PKs and harness TODO ids alike.
- **Forge issue/ticket/PR ids** render as `<repo>#<n>` when ambiguity with a task id (or a cross-repo ref) is possible (e.g. `teatree#11`, `<overlay-repo>#42`/`!42`). A bare `#<n>` for a forge ref is acceptable only inside a context already scoped to one forge namespace (e.g. a statusline line prefixed `[overlay]`, or a single-namespace section), never side-by-side with a task id.
- Never emit a bare `#<n>` for a task id sitting next to a bare `#<n>` for a ticket.
- **A repo-qualified ref is a rendering convention, not `gh`/`glab` CLI syntax — do X, never Y.** `<repo>#<n>` (e.g. `teatree#50`) is how you _write_ the ref in prose, a statusline, or a commit body. Neither `gh` nor `glab` accepts that slash/hash-qualified string as a single positional argument — pass the bare number and name the repo with its own flag:

  ```bash
  # do X — bare number + explicit repo flag:
  gh issue view 50 --repo souliane/teatree
  gh pr view 50 --repo souliane/teatree
  glab issue view 50 --repo souliane/teatree
  # never Y — a repo-qualified single argument is not valid gh/glab CLI syntax,
  # even though "teatree#50" is the correct PROSE rendering of the same ref:
  gh issue view teatree#50              # FORBIDDEN — gh rejects this argument shape
  gh issue view souliane/teatree#50     # FORBIDDEN — same error
  ```

  Inside the repo's own working tree (`gh`/`glab` resolve the repo from the git remote), `--repo`/`-R` can be omitted — `gh issue view 50` is fine there. Add the flag whenever the command runs outside that repo's tree, or whenever the surrounding text disambiguates against a same-numbered task id and the command must stay unambiguous too.

This is the canonical home; `/t3:checking` § "Output contract" cross-references it for the `task TODO-<id> (ticket #<n>)` line shape, and the disambiguation eval is `evals/scenarios/id_namespace_disambiguation.yaml`.
