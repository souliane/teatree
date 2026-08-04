---
name: sweeping-tickets
description: Evidence-gated ticket/issue consolidation and triage — classify every open issue against current `main`, then consolidate by merging related tickets INTO AN EXISTING ticket — never by minting a new umbrella row and never by discarding ideas — and close only what is demonstrably shipped or now folded into its host. Always asks the operator for the maximum number of tickets to keep before triaging — never assumes a number. Dry-run first; close only on user approval (or auto-close ONLY the high-confidence "shipped by merged PR #X" class), posting a one-line reason on every close. Use when the user says "sweep tickets", "sweeping tickets", "triage issues", "consolidate the tracker", "merge tickets into epics", "prune the tracker", or "clean up the issue tracker".
eval_exempt: evidence-gated ticket-consolidation walkthrough — its one-decision-per-question discipline is pinned in scenarios under the rules skill, and its evidence-gated close/consolidate discipline is pinned by the stale_open_issue_gate scenarios; no standalone agent trajectory beyond those to grade
compatibility: macOS/Linux, git, gh CLI.
requires:
  - rules
  - platforms
metadata:
  version: 0.0.1
  subagent_safe: false
---

# t3:sweeping-tickets — Evidence-Gated Ticket Consolidation & Triage

The tracker accumulates issues that are already shipped, overlap with other
open issues, or would take the codebase *backwards* if implemented now (they
predate a since-adopted design). Nothing consolidates them, so the tracker's
signal degrades — the operator can no longer tell what is actually next. This
skill reads the open issue set, judges each issue against *current `main`*,
and **consolidates**: it folds related work into the best-fitting **existing
ticket** so related pieces get implemented together, and it closes only what is
genuinely done or now redundant with its host.

**Never mint a new umbrella row.** The merge target is always a ticket that
already exists. A fresh epic is a container with no history, no discussion and
no prior context: folding N real tickets into one invents a row strictly thinner
than what it replaced, while the originals — with their evidence and their
threads — all become closed and secondary. The count drops and the substance
degrades, which reads as progress and is not. Reusing an existing ticket keeps
the thread that already holds the history, and merging the others' content into
it leaves the survivor RICHER than it was.

**Consolidation is never deletion.** The reduction in open-issue count comes
from *merging* tickets into their host — never from discarding an idea because
it didn't make the cut. If a ticket's substance isn't shipped yet, it survives
in the host ticket even after the standalone closes — and as its actual
substance (problem statement, evidence, acceptance criteria), not a bare
checklist line.

This sweep is the cure; the prevention is the same doctrine one step earlier, at
filing time — search the open backlog, extend the host issue that already covers
the ask, one issue per root cause. That rule is canonical in `AGENTS.md`
§ "Issue Creation" and is not restated here. A tracker that needs this sweep
often is a tracker where the filing-time reuse rule is being skipped.

It is a deliberately separate skill from `t3:retro` / dreaming (#2419).
Dreaming distills transcripts into memory rules — additive, unattended, low
blast radius. Sweeping-tickets reads the *issue tracker*, judges each issue
against *current `main`*, and **closes/consolidates** things — side-effectful,
evidence-required, and human-gated. Those are different risk profiles and are
kept apart on purpose.

## Non-Negotiables

1. **Ask the maximum count first, every run.** Before classifying anything,
   ask the operator via `AskUserQuestion` for the maximum number of open
   tickets/epics they want left when the sweep is done. Never assume a
   number — a low cap (e.g. 10) is very aggressive and must be the operator's
   explicit choice, not a default this skill picks for them.
2. **Consolidate by merging into an EXISTING ticket, never by creating one.**
   Related tickets are merged into whichever open ticket already covers the
   most of their scope — prefer the oldest / most-discussed when several fit,
   because that is where the history lives. Creating a new epic/umbrella row to
   hold them is forbidden. A ticket that isn't shipped yet is never just closed
   and forgotten — its substance moves into the host ticket first. If nothing
   genuinely fits, it STAYS STANDALONE; that is a legitimate outcome and counts
   toward the operator's cap, never a reason to invent a container.
3. **Evidence per verdict.** No issue is proposed for close or fold without a
   concrete citation — a merged PR number, a removed code path, the named
   epic it now belongs to, or the named design decision it contradicts. A
   bare "looks stale" or "looks related" is not a verdict.
4. **Dry-run before any close.** The first output is always a read-only
   classification list. Closing or folding happens only after the user
   approves, or for the single auto-close class in rule 5.
5. **Approval-gated closes, with one narrow exception.** Closing an issue is
   a colleague-visible write under the user's identity, so it follows the
   same discipline as every on-behalf action — `t3:rules` § "Publishing
   Actions Are Mode-Conditional". The only class that may close without a
   per-issue confirmation is **high-confidence "shipped by merged PR #X"**: a
   still-open issue whose exact ask is demonstrably shipped by a *merged*
   PR, with the PR citation posted on the issue. Every fold-into-epic close
   and every regressive close always waits for explicit approval.
6. **Close-as-completed only if the work is actually implemented.** Use
   `--reason completed` only when a merged PR actually delivers the
   ticket's ask — cite it. If the ticket is NOT implemented, it is never
   closed as done: fold its substance into the owning epic's checklist,
   then close the standalone with `--reason "not planned"` and a comment
   stating plainly that it is closed to keep the tracker at the epic
   level — not because the work is done — and that its checklist item
   stays readable and the ticket itself stays reopenable if the epic ever
   needs to split back out.

   **A code read is not the check (Non-Negotiable).** Closing an issue as
   fixed requires *executing the check the issue describes*. Reading the
   diff and seeing the fix present is not that check, and neither is the
   fixer's own passing test — that test was written to pass, so it proves
   the author's model of the bug, not the behaviour the issue reports.
   Round-trip and inverse pairs (export→import, encode→decode,
   serialise→parse) can never be verified from one side: an export that
   looks right closes nothing until something imports it. When the check
   cannot be run cheaply, take one of the two honest outcomes — leave the
   issue OPEN as *believed* fixed with the belief and its basis stated, or
   close it naming exactly what was exercised and what was not. Never let
   "the code looks correct" render as "verified fixed".
7. **No silent closes.** Every close posts a one-line reason plus a link (to
   the shipping PR, or to the owning epic) before the issue is closed. An
   operator reading the issue later sees *why* it went and *where its
   substance lives now*.
8. **The GitHub Projects board is retired — don't sync one.** This sweep
   never reads from, writes to, or reorders a Projects v2 board. The tracker
   is the repo's open issues plus the tracking epics; there is no separate
   queue to keep in sync.
9. **Load the architecture state before judging "regressive".** The
   regressive / still-valid call needs current architecture in context, not
   issue-text alone — read the project's architecture/design reference (ask
   the operator which doc is canonical for this project if it isn't obvious
   — e.g. a BLUEPRINT.md, an ARCHITECTURE.md, a design doc linked from the
   README) and the recent structural decisions it points at before deciding
   an issue would move the codebase backwards. Name the conflicting decision
   in the verdict. Never hardcode one project's doc path as a fixed input —
   ask or discover it per run.
10. **No AI signature** on any close comment, fold comment, or relabel (per
    `t3:rules`).

## Classification

For each open issue, assign exactly one verdict with its evidence:

| Verdict | Test | Evidence to cite | Action |
|---------|------|------------------|--------|
| **Shipped** | The ask is already implemented on `main` **and the check the issue describes has been run** (rule 6) | the merged PR / issue that did it, plus what the check exercised | close `--reason completed` (auto-close only if the PR is *merged* and the match is exact — rule 5) |
| **Consolidate into an existing ticket** | Related to another OPEN ticket that already covers most of its scope | the existing ticket it now belongs to (never a new one) | merge its substance into that ticket's body, then close the standalone `--reason "not planned"` (approval-gated — rule 6) |
| **Regressive** | Implementing it now would contradict a since-adopted design | the conflicting decision, named (e.g. "pre-#2385 single-tach-node assumption") | record as a "won't do" note on the host ticket, or close `--reason "not planned"` with the citation (approval-gated) |
| **Still standalone** | Genuinely distinct scope, no existing ticket fits, and the operator's cap has room | — | keep open; it counts toward the operator's max. Never create a container to absorb it |

Bias toward **keep** (as a standalone, or merged into an existing ticket that stays open)
when uncertain — a wrong close destroys signal; a kept issue just gets swept
again next cadence.

## Command Reference

```bash
# Read one issue in full (body, comments, labels, state) before judging it.
# Prefer the MCP tool (structured JSON, no text parsing):
#   mcp__teatree__github_issue(issue_url) + mcp__teatree__github_issue_comments(issue_url)
#   (gitlab_* for GitLab). CLI fallback below when the MCP server isn't connected.
gh issue view <N> --repo <owner>/<repo> --json title,body,comments,labels,state

# List the candidate set (open issues), oldest first.
gh issue list --repo <owner>/<repo> --state open \
  --json number,title,labels,updatedAt --limit 300

# Find the MERGE TARGET among tickets that already exist. Never create one:
# read the open set and pick whichever ticket already covers most of the
# candidate's scope, preferring the oldest / most-discussed when several fit.
gh issue list --repo <owner>/<repo> --state open \
  --json number,title,body,url,comments,createdAt --limit 300

# Check whether a merged PR already shipped the ask (shipped evidence).
gh pr list --repo <owner>/<repo> --state merged --search "<keywords>" \
  --json number,title,url,mergedAt

# Append a ticket's substance to an epic's checklist. Read the current body
# first (never blind-overwrite an issue body — the same read-before-write
# discipline as `t3:rules` § "Read Before Overwriting a Tracked Config/Dotfile"),
# then write the merged body back.
gh issue edit <EPIC_N> --repo <owner>/<repo> --body-file <updated-body.md>

# Close WITH a reason + citation (no silent close — rule 7). `--reason` records
# the GitHub close-reason; `--comment` posts the one-line why + link first.
gh issue close <N> --repo <owner>/<repo> \
  --reason completed \
  --comment "Shipped by #<PR> (merged <date>): <one-line why>. Reopen if this misses a case."

gh issue close <N> --repo <owner>/<repo> \
  --reason "not planned" \
  --comment "Folded into epic #<EPIC_N> (<one-line why>) — closed to keep the tracker at the \
epic level, not because the work is done. See #<EPIC_N> for the live checklist item; reopen \
this if it ever needs to split back out."

# Relabel a still-standalone issue instead of closing it.
gh issue edit <N> --repo <owner>/<repo> --add-label "<label>" --remove-label "<label>"
```

Use `--reason completed` only for issues a merged PR actually delivered; use
`--reason "not planned"` for every fold-into-epic and regressive close.

## Workflow

### 1. Ask the operator's cap (Non-Negotiable 1)

Before touching the tracker, ask via `AskUserQuestion`: "What's the maximum
number of open tickets/epics you want left when this sweep is done?" Present
a few reference points (the current open count, a conservative option like
"just fold the obvious duplicates", an aggressive option like "10 total") but
let the operator pick — never default to a number yourself.

### 2. Read the backlog

Pull the open-issue set with the command above. The repo's open issues are
the whole queue now — there is no board to sync first (Non-Negotiable 8).

### 3. Load the architecture state

Read the project's architecture/design reference (ask which doc is canonical
if it isn't obvious) and skim the recent structural decisions it points at.
This is what lets a "regressive" verdict cite a *named* decision rather than
a guess (Non-Negotiable 9).

### 4. Classify each issue against current `main`

For every open issue, read it in full and assign one verdict from the table
with its evidence. For "shipped", search merged PRs for the issue's ask. For
"consolidate", find the EXISTING ticket it belongs to — never propose a new
one. For "regressive", test the ask against the architecture state from step 3.

### 5. Choose merge targets from the tickets that already exist

Assign each "consolidate" and "regressive" issue to the OPEN ticket that
already covers most of its scope, preferring the oldest / most-discussed when
several fit — that is where the history lives. **Creating a new epic or
umbrella row is not an option here.** An issue nothing fits stays standalone
and counts toward the cap.

The total open count after the sweep must respect the cap from step 1. If it
does not, merge further into existing hosts, or say plainly that the cap cannot
be met without inventing containers and let the operator choose — never close
the gap by minting rows.

### 6. Produce the dry-run list

Present a read-only table — **no closes or folds yet** (Non-Negotiable 4):

| # | Title | Verdict | Evidence | Proposed action |
|---|-------|---------|----------|-----------------|
| #1838 | … | Shipped | merged #2204 | auto-close (exact, merged) |
| #1672 | … | Consolidate | merge into existing #1900 | merge + close (needs approval) |
| #97 | … | Still standalone | — | keep |

### 7. Walk the closes and folds one decision at a time

Walk the non-auto-close proposals with `AskUserQuestion` — one issue per
question, never a bulk "close all these?" dump. The auto-close class
(high-confidence shipped-by-merged-PR) may proceed without a per-issue
prompt, but still posts the citation comment first (Non-Negotiable 7).

### 8. Fold, close, and keep

For each approved (or auto-close) issue: if it's shipped, close with the PR
citation. If it's a fold, append its substance to the owning epic's checklist
*first*, then close the standalone with the consolidation reason. Keep the
still-standalone set as-is (optionally relabel). Summarize: N closed-shipped
(with PR links), M folded into K epics (with epic links), P kept standalone.

## Scheduling via the loop (once trustworthy)

The issue (#2419) gates the cadence on trust: run the sweep interactively
until its verdicts prove reliable, *then* promote it to a low-frequency loop
cadence. When that point comes, wire it the way `t3:scanning-news` is wired —
a global, cadence-only scanner (mirror `teatree.loop.scanners.scanning_news`)
that queues a `backlog_sweep` task (the loop-internal name for this periodic
sweep; it dispatches the `sweeping-tickets` skill) at a low rate (e.g.
weekly), with two safety properties baked in from day one:

- **Default-OFF.** A kill switch defaulting the scanner off, like every other
  destructive-capable loop behaviour.
- **Ask-gate in the directive.** The queued task carries an ASK-GATE marker
  (the `t3:scanning-news` pattern) so the dispatched sweep records proposals
  and surfaces the batch for approval — it never mass-closes or mass-folds
  unattended. Only the high-confidence shipped-by-merged-PR class
  auto-closes. The queued task does not presume a cap either — it still asks
  (Non-Negotiable 1) rather than assuming a number for an unattended run.

Until that wiring lands, this skill runs on demand (`/t3:sweeping-tickets`) —
which is the correct posture while the verdicts are still being trusted.

## Rules

- Never close an issue without a cited reason posted on it first.
- Never bulk-close or bulk-fold — walk approvals one decision at a time, the
  shipped-by-merged-PR class being the one auto-close exception.
- Always ask the operator's max count first — never assume a number.
- Consolidate by merging into epics, never by discarding ideas.
- Keep when uncertain; a kept issue is just swept again next time.
- Load the architecture state before any "regressive" verdict, and name the
  conflicting decision.
- Never hardcode a project's design-doc path — ask or discover it per run.
- No AI signature on close comments, fold comments, or relabels.

## Related skills

- `t3:scanning-news` — the cadence/ask-gate pattern to copy when this skill
  is promoted to a loop scanner.
- `t3:retro` — distinct domain (additive memory rules from transcripts); this
  skill is deliberately *not* folded into it (#2419).

---

*If this skill was truncated during context compression, re-read it from disk
before continuing the sweep.*
