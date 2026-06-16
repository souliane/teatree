---
name: backlog-sweep
description: Periodic evidence-gated triage of the issue backlog — for each open issue, classify it against current `main` as superseded / stale / regressive / still-valid, then propose close-with-citation. The retire counterpart to teatree-plan's prioritize, reusing its GitHub Projects board layer and one-decision-per-question walkthrough. Dry-run first; close only on user approval (or auto-close ONLY the high-confidence "superseded by merged PR #X" class), posting a one-line reason on every close. Use when the user says "sweep backlog", "backlog sweep", "triage issues", "prune the backlog", "retire stale issues", or "clean up the issue tracker".
eval_exempt: evidence-gated backlog-triage walkthrough — the retire counterpart to teatree-plan's prioritize; it reuses teatree-plan's AskUserQuestion one-decision-per-question flow (pinned in the rules skill scenarios) and closes only on user approval, so the issue close/cite chokepoint's own deterministic tests grade the actual behaviour
compatibility: macOS/Linux, git, gh CLI, GitHub Projects v2 board.
requires:
  - teatree-plan
  - rules
  - platforms
triggers:
  priority: 90
  keywords:
    - '\b(backlog[- ]sweep|sweep (the )?backlog|triage (the )?(issue )?backlog|prune (the )?(issue )?backlog|retire (stale )?issues|clean up the (issue )?(tracker|backlog))\b'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# t3:backlog-sweep — Evidence-Gated Backlog Triage

The backlog accumulates issues that are already done, no longer valid, or would
take the codebase *backwards* if implemented now (they predate a since-adopted
design). Nothing prunes them, so the backlog's signal degrades. This skill is the
**retire** counterpart to `t3:teatree-plan`'s **prioritize**: same board layer,
same one-decision-per-question walkthrough — but instead of ordering open work it
proposes *closing* work that no longer earns a slot.

It is a deliberately separate skill from `t3:retro` / dreaming (#2419). Dreaming
distills transcripts into memory rules — additive, unattended, low blast radius.
Backlog-sweep reads the *issue tracker*, judges each issue against *current
`main`*, and **closes** things — side-effectful, evidence-required, and
human-gated. Those are different risk profiles and are kept apart on purpose.

## Non-Negotiables

1. **Evidence per verdict.** No issue is proposed for close without a concrete
   citation — a merged PR number, a removed code path, or the named design
   decision it now contradicts. A bare "looks stale" is not a verdict.
2. **Dry-run before any close.** The first output is always a read-only
   classification list. Closing happens only after the user approves, or for the
   single auto-close class in rule 3.
3. **Approval-gated closes, with one narrow exception.** Closing an issue is a
   colleague-visible write under the user's identity, so it follows the same
   discipline as every on-behalf action — `t3:rules` § "Publishing Actions Are
   Mode-Conditional". The only class that may close without a per-issue
   confirmation is **high-confidence "superseded by merged PR #X"**: a still-open
   issue whose exact ask is demonstrably shipped by a *merged* PR, with the PR
   citation posted on the issue. Stale, regressive, and ambiguous-superseded
   verdicts always wait for explicit approval.
4. **No silent closes.** Every close posts a one-line reason plus a link before
   the issue is closed. An operator reading the issue later sees *why* it went.
5. **Load the architecture state before judging "regressive".** The
   regressive / still-valid call needs current architecture in context, not
   issue-text alone — read `BLUEPRINT.md` and the recent structural decisions
   (e.g. is the issue pre- or post-#2385 / #2399?) before deciding an issue would
   move the codebase backwards. Name the conflicting decision in the verdict.
6. **No AI signature** on any close comment or relabel (per `t3:rules`).

## Classification

For each open issue, assign exactly one verdict with its evidence:

| Verdict | Test | Evidence to cite | Action |
|---------|------|------------------|--------|
| **Superseded** | The ask is already shipped | the merged PR / issue that did it | propose close (auto-close only if the PR is *merged* and the match is exact — rule 3) |
| **Stale / invalid** | No longer reproduces, or the referenced code path is gone | the removed file/symbol, or a repro that now passes | propose close (approval-gated) |
| **Regressive** | Implementing it now would contradict a since-adopted design | the conflicting decision, named (e.g. "pre-#2385 single-tach-node assumption") | propose close or re-scope (approval-gated) |
| **Still valid** | None of the above | — | keep; optionally relabel / re-prioritize via `t3:teatree-plan` |

Bias toward **keep** when uncertain — a wrong close destroys signal; a kept issue
just gets swept again next cadence.

## Command Reference

```bash
# Read one issue in full (body, comments, labels, state) before judging it.
gh issue view <N> --repo <owner>/<repo> --json title,body,comments,labels,state

# List the candidate set (open issues), oldest first.
gh issue list --repo <owner>/<repo> --state open \
  --json number,title,labels,updatedAt --limit 300

# Check whether a merged PR already shipped the ask (superseded evidence).
gh pr list --repo <owner>/<repo> --state merged --search "<keywords>" \
  --json number,title,url,mergedAt

# Close WITH a reason + citation (no silent close — rule 4). `--reason` records
# the GitHub close-reason; `--comment` posts the one-line why + link first.
gh issue close <N> --repo <owner>/<repo> \
  --reason "not planned" \
  --comment "Superseded by #<PR> (merged <date>): <one-line why>. Reopen if this misses a case."

# Relabel a still-valid issue instead of closing it.
gh issue edit <N> --repo <owner>/<repo> --add-label "<label>" --remove-label "<label>"
```

Use `--reason completed` only for issues a merged PR actually delivered; use
`--reason "not planned"` for stale / regressive / re-scoped closes.

## Workflow

### 1. Sync the board, then read the backlog

Reuse `t3:teatree-plan` § 1 — every open issue across the overlay's repos must be
on the GitHub Projects v2 board (no orphans). Then pull the open-issue set with
the command above. The board is the queue; this sweep walks it.

### 2. Load the architecture state

Read `BLUEPRINT.md` and skim the recent structural decisions referenced there
(the per-section docs under `docs/blueprint/`, plus recently merged
architecture issues). This is what lets a "regressive" verdict cite a *named*
decision rather than a guess (Non-Negotiable 5).

### 3. Classify each issue against current `main`

For every open issue, read it in full and assign one verdict from the table with
its evidence. For the superseded check, search merged PRs for the issue's ask. For
stale, confirm the referenced code path against the live tree. For regressive,
test the ask against the architecture state from step 2.

### 4. Produce the dry-run list

Present a read-only table — **no closes yet** (Non-Negotiable 2):

| # | Title | Verdict | Evidence | Proposed action |
|---|-------|---------|----------|-----------------|
| #1838 | … | Superseded | merged #2204 | auto-close (exact, merged) |
| #1672 | … | Regressive | contradicts #2385 sub-layering | close (needs approval) |
| #97 | … | Still valid | — | keep |

### 5. Walk the closes one decision at a time

Reusing `t3:teatree-plan`'s one-question-at-a-time discipline, walk the
non-auto-close proposals with `AskUserQuestion` — one issue per question, never a
bulk "close all these?" dump. The auto-close class (high-confidence superseded by
a merged PR) may proceed without a per-issue prompt, but still posts the citation
comment first (Non-Negotiable 4).

### 6. Close with citations, keep the rest

For each approved (or auto-close) issue, post the one-line reason + link, then
close with the matching `--reason`. Relabel or re-prioritize the still-valid set
via `t3:teatree-plan`. Summarize: N closed (with links), M kept, K re-scoped.

## Scheduling via the loop (once trustworthy)

The issue (#2419) gates the cadence on trust: run the sweep interactively until
its verdicts prove reliable, *then* promote it to a low-frequency loop cadence.
When that point comes, wire it the way `t3:scanning-news` is wired — a global,
cadence-only scanner (mirror `teatree.loop.scanners.scanning_news`) that queues a
`backlog_sweep` task at a low rate (e.g. weekly), with two safety properties baked
in from day one:

- **Default-OFF.** A `backlog_sweep_disabled`-style kill switch defaulting the
  scanner off, like every other destructive-capable loop behaviour.
- **Ask-gate in the directive.** The queued task carries an ASK-GATE marker (the
  `t3:scanning-news` pattern) so the dispatched sweep records proposals and
  surfaces the batch for approval — it never mass-closes unattended. Only the
  high-confidence merged-PR-superseded class auto-closes.

Until that wiring lands, this skill runs on demand (the triggers above) — which
is the correct posture while the verdicts are still being trusted.

## Rules

- Never close an issue without a cited reason posted on it first.
- Never bulk-close — walk approvals one decision at a time (the teatree-plan
  pattern), the merged-PR-superseded class being the one auto-close exception.
- Keep when uncertain; a kept issue is just swept again next time.
- Load the architecture state before any "regressive" verdict, and name the
  conflicting decision.
- No AI signature on close comments or relabels.

## Related skills

- `t3:teatree-plan` — the prioritize counterpart; shares the board sync + the
  one-decision-per-question walkthrough this skill reuses.
- `t3:scanning-news` — the cadence/ask-gate pattern to copy when this skill is
  promoted to a loop scanner.
- `t3:retro` — distinct domain (additive memory rules from transcripts); this
  skill is deliberately *not* folded into it (#2419).

---

*If this skill was truncated during context compression, re-read it from disk
before continuing the sweep.*
