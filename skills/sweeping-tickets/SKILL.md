---
name: sweeping-tickets
description: Evidence-gated ticket/issue grouping — classify every open issue against current `main`, then GROUP AGGRESSIVELY BY DEFAULT by folding related tickets INTO AN EXISTING ticket, never minting a new umbrella row and never discarding an idea. Closing is not the mechanism: a member's body moves into its host and is proved to have landed before its standalone row is retired, so the default path performs zero real closures. Always asks the operator for the maximum number of tickets to keep before triaging — never assumes a number. Dry-run first; retire a row only on user approval, posting a one-line reason first. Use when the user says "sweep tickets", "sweeping tickets", "triage issues", "consolidate the tracker", "group tickets", "prune the tracker", or "clean up the issue tracker".
eval_exempt: evidence-gated ticket-consolidation walkthrough — its one-decision-per-question discipline is pinned in scenarios under the rules skill, and its evidence-gated close/consolidate discipline is pinned by the stale_open_issue_gate scenarios; no standalone agent trajectory beyond those to grade
compatibility: macOS/Linux, git, gh CLI.
requires:
  - rules
  - platforms
metadata:
  version: 0.0.1
---

# t3:sweeping-tickets — Evidence-Gated Ticket Grouping

The tracker accumulates issues that are already shipped, overlap with other
open issues, or would take the codebase *backwards* if implemented now (they
predate a since-adopted design). Nothing groups them, so the tracker's
signal degrades — the operator can no longer tell what is actually next. This
skill reads the open issue set, judges each issue against *current `main`*,
and **groups**: it folds related work into the best-fitting **existing
ticket** so related pieces get implemented together.

**Grouping is the default posture, not an opt-in mode.** A run with no extra
flags groups aggressively — that is what the skill *is*, and the operator never
has to ask for it. Every ticket costs the same fixed overhead regardless of how
small it is (worktree provision, agent spin-up, planning, coding, review, PR, CI,
merge), so ten one-line tickets pay that ten times and one ticket carrying ten
small things pays it once for the same work. Backlog size is a direct multiplier
on delivery cost, not a bookkeeping detail.

**A host may carry several unrelated small things.** The one-concern-per-ticket
instinct is what produced the graveyard. When each concern is individually
trivial, bundling them is *correct* — especially when they land in the same
module, seam or test file, which amortises review and CI cost as well as agent
cost. Do not split a group because its members are conceptually unrelated; split
it when the bundle would need more than one PR.

**Never mint a new umbrella row.** The merge target is always a ticket that
already exists. A fresh epic is a container with no history, no discussion and
no prior context: folding N real tickets into one invents a row strictly thinner
than what it replaced, while the originals — with their evidence and their
threads — all become closed and secondary. The count drops and the substance
degrades, which reads as progress and is not. Reusing an existing ticket keeps
the thread that already holds the history, and merging the others' content into
it leaves the survivor RICHER than it was.

**Close nothing for real.** The reduction in open-issue count comes entirely
from *folding* tickets into their host — never from discarding an idea because
it didn't make the cut. Closing is the wrong lever: it discards intent, and the
ideas in those rows are not the problem, their packaging is. A folded ticket's
content moves into its host *first*, as its actual substance (problem statement,
evidence, acceptance criteria) rather than a bare checklist line; only then does
the standalone row go away. That is what "no real closures" means — the row
retires, the idea does not.

Do the fold mechanically so nothing is lost to a free-hand rewrite:

```bash
t3 <overlay> ticket fold --host-body host.md --member-body member.md \
  --member-ref '#4247' --member-title '<title>' --out merged.md
gh issue edit <HOST> --repo <owner>/<repo> --body-file merged.md
# re-read the host off the forge and PROVE the member's body landed:
gh issue view <HOST> --repo <owner>/<repo> --json body -q .body > host-now.md
t3 <overlay> ticket fold-check --host-body host-now.md --member-body member.md
```

`fold` copies the body verbatim and is idempotent per `--member-ref`, so a retried
sweep never stacks duplicates. `fold-check` exits non-zero when the host
summarised instead of moving the body — a member whose fold does not verify is
**never** retired.

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
2. **Group by folding into an EXISTING ticket, never by creating one.**
   Related tickets are folded into whichever open ticket already covers the
   most of their scope — prefer the oldest / most-discussed when several fit,
   because that is where the history lives. Creating a new epic/umbrella row to
   hold them is forbidden. If nothing genuinely fits, the ticket STAYS
   STANDALONE; that is a legitimate outcome and counts toward the operator's
   cap, never a reason to invent a container.
3. **Evidence per verdict.** No issue is proposed for a fold without a
   concrete citation — a merged PR number, a removed code path, the named
   host it now belongs to, or the named design decision it contradicts. A
   bare "looks stale" or "looks related" is not a verdict.
4. **Dry-run before any write.** The first output is always a read-only
   classification list. Folding and retiring happen only after the user
   approves.
5. **No row is retired without a VERIFIED fold (Non-Negotiable).** The default
   path performs **zero real closures**: every reduction moves the member's
   body into its host and proves it landed (`ticket fold` → `gh issue edit` →
   `ticket fold-check`) before the standalone goes. This holds for the
   already-shipped class too — a merged PR is a *citation on the fold*, not a
   licence to discard the row's content. Retiring the row is a colleague-visible
   write under the user's identity, so it also follows `t3:rules` § "Publishing
   Actions Are Mode-Conditional" and always waits for explicit approval.
6. **A close-reason states where the substance went, never "done".** Retire a
   folded standalone with `--reason "not planned"` and a comment naming the host
   — it is retired to keep the tracker at the host level, not because the work
   is finished, and it stays reopenable if the host ever needs to split back
   out. Use `--reason completed` only when a merged PR actually delivers the
   ticket's ask AND its content has been folded into a host anyway, so the
   evidence survives.

   **A code read is not the check (Non-Negotiable).** Closing an issue as
   fixed requires *executing the check the issue describes*. Reading the
   diff and seeing the fix present is not that check, and neither is the
   fixer's own passing test — that test was written to pass, so it proves
   the author's model of the bug, not the behaviour the issue reports.
   Round-trip and inverse pairs (export→import, encode→decode,
   serialise→parse) can never be verified from one side: an export that
   looks right closes nothing until something imports it. When the check
   cannot be run, the issue does **not** close as shipped — naming what was
   and was not exercised is a disclosure, and a disclosure is not a
   substitute for running the check. Leave it OPEN as *believed* fixed with
   the belief and its basis stated; the only other honest outcome is a retire
   under a different, named disposition (the fold `--reason "not planned"`
   above), never `--reason completed`. Never let "the code looks correct"
   render as "verified fixed".
7. **No silent retirements.** Every retirement posts a one-line reason plus a
   link (to the shipping PR, and to the host carrying its substance) before the
   issue is closed. An operator reading the issue later sees *why* it went and
   *where its substance lives now*.
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
10. **No AI signature** on any retirement comment, fold comment, or relabel (per
    `t3:rules`).

## Classification

For each open issue, assign exactly one verdict with its evidence. **Every
verdict except "still standalone" folds first** — the row is retired only after
`fold-check` passes, so no verdict discards content.

| Verdict | Test | Evidence to cite | Action |
|---------|------|------------------|--------|
| **Shipped** | The ask is already implemented on `main` **and the check the issue describes has been run** (rule 6) | the merged PR / issue that did it, plus what the check exercised | fold its body into the host that owns the seam, verify, then retire citing the merged PR |
| **Group into an existing ticket** | Related to another OPEN ticket that already covers most of its scope — or individually trivial and landing in the same module, seam or test file | the existing ticket it now belongs to (never a new one) | fold its body into that ticket, verify, then retire `--reason "not planned"` (approval-gated — rule 5) |
| **Regressive** | Implementing it now would contradict a since-adopted design | the conflicting decision, named (e.g. "pre-#2385 single-tach-node assumption") | fold it into the host as a "won't do" note carrying its reasoning, then retire `--reason "not planned"` with the citation (approval-gated) |
| **Still standalone** | Genuinely distinct scope, no existing ticket fits, and the operator's cap has room | — | keep open; it counts toward the operator's max. Never create a container to absorb it |

Bias toward **keep** (as a standalone, or folded into an existing ticket that stays open)
when uncertain — a wrong retirement destroys signal; a kept issue just gets swept
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

# Move a member's substance into its host, VERBATIM. Read both bodies first
# (never blind-overwrite an issue body — the same read-before-write discipline
# as `t3:rules` § "Read Before Overwriting a Tracked Config/Dotfile").
gh issue view <HOST_N> --repo <owner>/<repo> --json body -q .body > host.md
gh issue view <N> --repo <owner>/<repo> --json body -q .body > member.md
t3 <overlay> ticket fold --host-body host.md --member-body member.md \
  --member-ref '#<N>' --member-title '<member title>' --out merged.md
gh issue edit <HOST_N> --repo <owner>/<repo> --body-file merged.md

# PROVE the fold landed before retiring anything (rule 5) — non-zero on a loss.
gh issue view <HOST_N> --repo <owner>/<repo> --json body -q .body > host-now.md
t3 <overlay> ticket fold-check --host-body host-now.md --member-body member.md

# Only then retire the row, WITH a reason + citation (no silent retirement —
# rule 7). `--reason` records the GitHub close-reason; `--comment` posts the
# one-line why + link first.
gh issue close <N> --repo <owner>/<repo> \
  --reason "not planned" \
  --comment "Folded into #<HOST_N> (<one-line why>) — its body now lives there in full. \
Retired to keep the tracker at the host level, not because the work is done; reopen this \
if it ever needs to split back out."

gh issue close <N> --repo <owner>/<repo> \
  --reason completed \
  --comment "Shipped by #<PR> (merged <date>): <one-line why>. Body folded into #<HOST_N> \
so the evidence survives. Reopen if this misses a case."

# Relabel a still-standalone issue instead of retiring it.
gh issue edit <N> --repo <owner>/<repo> --add-label "<label>" --remove-label "<label>"
```

Use `--reason completed` only for issues a merged PR actually delivered; use
`--reason "not planned"` for every group and regressive retirement. Either way
the fold comes first.

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
"group", find the EXISTING ticket it belongs to — never propose a new one. For
"regressive", test the ask against the architecture state from step 3.

### 5. Choose hosts from the tickets that already exist

Assign each grouped and regressive issue to the OPEN ticket that already covers
most of its scope, preferring the oldest / most-discussed when several fit —
that is where the history lives. **Creating a new epic or umbrella row is not an
option here.** An issue nothing fits stays standalone and counts toward the cap.

Group hard: a shared module, seam or test file is enough, and a host may carry
several unrelated small things (see the header). Two more host-choice facts that
decide whether a group is reachable at all:

- **A host must be admissible.** A group whose host is unreachable by intake
  (wrong label, `needs-triage`) saves no delivery slot — pick a reachable host
  where the content allows, and name the label change where it does not.
- **A spent umbrella is never a host.** An epic whose members are all closed is
  an organizing row, not a container for new work.

The total open count after the sweep must respect the cap from step 1. If it
does not, group further into existing hosts, or say plainly that the cap cannot
be met without inventing containers and let the operator choose — never close
the gap by minting rows, and never by discarding one.

### 6. Produce the dry-run list

Present a read-only table — **no folds or retirements yet** (Non-Negotiable 4):

| # | Title | Verdict | Evidence | Host | Proposed action |
|---|-------|---------|----------|------|-----------------|
| #1838 | … | Shipped | merged #2204 | #1900 | fold + retire (needs approval) |
| #1672 | … | Group | same seam as #1900 | #1900 | fold + retire (needs approval) |
| #97 | … | Still standalone | — | — | keep |

### 7. Walk the folds one decision at a time

Walk the proposals with `AskUserQuestion` — one issue per question, never a bulk
"retire all these?" dump. Every proposal posts its citation comment before the
row goes (Non-Negotiable 7).

### 8. Fold, verify, retire, and keep

For each approved issue: fold its body into the host, re-read the host and run
`fold-check`, and only then retire the standalone with its reason and citations.
A fold that does not verify is left OPEN — never retired on a promise. Keep the
still-standalone set as-is (optionally relabel). Summarize: N folded into K hosts
(with links), P kept standalone, and any fold that failed verification.

## Scheduling via the loop

The sweep runs **daily** as a registered `Loop` row (`backlog_sweep`), so it is
versioned, exportable, and disableable exactly like every other loop:

```bash
t3 loops list                        # the sweep appears here with its cadence
t3 loops enable backlog_sweep        # the row IS the switch
t3 loops disable backlog_sweep
```

The row is the single switch: `backlog_sweep_disabled` ships open, and the row
seeds `enabled = false` so an operator turns it on deliberately. The scanner
(`teatree.loop.scanners.backlog_sweep`) queues one `backlog_sweep` task per
`backlog_sweep_cadence_hours` (default 24) and stamps two contracts onto it:

- **Group-first, close nothing for real.** Unconditional — the queued directive
  instructs aggressive grouping and zero real closures whether or not the
  ask-gate is on.
- **Ask-gate in the directive.** With `ask_before_backlog_sweep_closes` (default
  true) the queued task carries an ASK-GATE marker so the dispatched sweep
  records proposals and surfaces the batch for approval — it never mass-retires
  unattended, and every retirement routes through the gated
  `t3 <overlay> ticket bulk-close`. The queued task does not presume a cap
  either — it still asks (Non-Negotiable 1) rather than assuming a number for an
  unattended run.

Run it on demand any time with `/t3:sweeping-tickets`.

## Rules

- Never retire an issue without a verified fold and a cited reason posted first.
- Never bulk-retire or bulk-fold — walk approvals one decision at a time.
- Always ask the operator's max count first — never assume a number.
- Group aggressively by default; never discard an idea to hit the cap.
- Keep when uncertain; a kept issue is just swept again next time.
- Load the architecture state before any "regressive" verdict, and name the
  conflicting decision.
- Never hardcode a project's design-doc path — ask or discover it per run.
- No AI signature on retirement comments, fold comments, or relabels.

## Related skills

- `t3:scanning-news` — the sibling cadence/ask-gate loop scanner.
- `t3:retro` — distinct domain (additive memory rules from transcripts); this
  skill is deliberately *not* folded into it (#2419).

---

*If this skill was truncated during context compression, re-read it from disk
before continuing the sweep.*
