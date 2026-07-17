---
name: ac-reviewing-codebase
description: Periodic holistic architectural review — the third of teatree's three review tiers (design-time `architecture-design`, per-PR deterministic `check_antipatterns.py`, periodic holistic `ac-reviewing-codebase`). Walks the whole tree for judgement-tier anti-patterns and BLUEPRINT.md staleness that no single diff can catch. Dispatched automatically by `ArchitecturalReviewScanner` on a time or merge-count cadence — not user-invoked.
eval_exempt: whole-tree periodic synthesis with no fixed input/output pair to grade per-turn; correctness is judged by the tickets/BLUEPRINT-fixes it produces over time, mirroring retro (#837)
requires:
  - architecture-design
  - review
compatibility: macOS/Linux, any teatree-managed repo.
metadata:
  version: 0.0.1
  subagent_safe: true
---

# ac-reviewing-codebase — Periodic Holistic Architectural Review

## Why this exists, and how it differs from the other two tiers

Three review tiers cover different scopes and cadences (BLUEPRINT.md § 17.2):

| Tier | When | Scope | Mechanism |
|---|---|---|---|
| `architecture-design` | Before code is written | The change about to be made | Ten-check pre-flight, worktree-local |
| `check_antipatterns.py` | Every PR | The diff | Deterministic grep over `grep_hint` entries |
| **`ac-reviewing-codebase` (this skill)** | Periodic (time or merge-count cadence) | The **whole tree** | Judgement pass, one Task per run |

The first two tiers are per-change and catch what a single diff introduces. Neither can see **drift that accumulates across many small, individually-fine changes** — a module that crept past the health threshold one function at a time, a BLUEPRINT section that quietly went stale as the code it describes moved on, a pattern that was fine in isolation twice and is now a repo-wide anti-pattern the third time. That is this skill's job.

You are dispatched by `ArchitecturalReviewScanner` (`src/teatree/loop/scanners/architectural_review.py`) as a headless `architectural_review`-phase Task, firing after `architectural_review_cadence_hours` (default 168h) or `architectural_review_after_merge_count` (default 25) merges, whichever comes first. There is no user prompt to parse — the trigger IS the instruction. Anchor to `Ticket.issue_url == "architectural-review://<overlay>"`, the synthetic per-overlay tracking ticket the scanner creates.

## What to do

### 1. Walk the judgement-tier anti-pattern catalog

`docs/generated/antipattern-catalog.md` (generated from `src/teatree/quality/antipatterns.yaml`) lists every known anti-pattern. Entries marked `detection: greppable` are already caught mechanically by `scripts/hooks/check_antipatterns.py` on every PR — skip those, they are not your job. Entries marked `detection: judgement` need a human-grade (or agent-grade) eye across the whole tree, because there is no reliable regex for them:

- Test function with no assertion
- Lower-level module importing a higher-level one (backwards dependency edge)
- Test that writes its own baseline / snapshot
- FloatField for currency
- Liveness path hard-fails a transient and locks the factory out
- Security or merge gate fails open on exception
- Gate classifies read-vs-write by verb instead of effective mutation
- One item's exception aborts the whole sweep (loop scanner, no fault isolation)
- Same fact in two co-equal stores with no authority
- Canonicalization that is not idempotent
- Identity matching that depends on the filesystem
- Module past the health threshold (god module)
- Business logic in a view or management command
- Overlay re-wraps a platform API instead of using the extension point
- Fallback chain that hides the primary failure
- List/fetch reads only the first page (silent truncation)

Re-read `docs/generated/antipattern-catalog.md` at review time rather than trusting the list above — the catalog is the source of truth and grows. For each judgement entry, sample across the tree (you do not need to read every file; prioritize modules that changed since the last review — see § 3) and check whether the anti-pattern's `preferred_pattern` is actually followed. File a ticket per confirmed instance (not per entry scanned) using the normal ticket pipeline — do not fix inline; this is a review pass, not a fix pass. Reference the catalog entry id in the ticket so the fixer has the anti-pattern/preferred-pattern pair without re-deriving it.

### 2. Check BLUEPRINT.md tightness and staleness

Per BLUEPRINT.md's own `## Maintenance` section: "Keeping the file tight is a reviewer responsibility — flag bloat, prose that restates code instead of capturing architecture, and stale or duplicated sections — captured in `skills/review/SKILL.md` § 'Keep BLUEPRINT Tight' and in the periodic holistic review (this skill)." Load `skills/review/SKILL.md` § "Keep BLUEPRINT Tight" for the three-point checklist (restated-not-architectural prose, stale/duplicated sections, appendix-class detail in the top-level file) and apply it to the **whole file**, not a diff — this is the one place that checklist runs at full-tree scope instead of per-PR scope. Cross-check every section against the current code it describes; a section naming a mechanism that has since moved, been renamed, or been removed is a staleness finding.

### 3. Prioritize by what changed since the last review

You do not have unlimited budget to re-read the entire tree every cadence. Scope your attention using the same signal the scanner uses to decide *whether* to fire: `TicketTransition` rows into `_MERGED_STATES` (`merged`, `delivered`) since the last completed `architectural_review` Task's `Session.started_at`. Prioritize modules touched by those merges — that is where new drift is most likely to have landed. A full cold read of untouched, previously-clean areas is lower priority than re-checking what actually moved.

### 4. File findings through the normal pipeline; never fix inline

This review produces tickets, not commits. For each confirmed finding (catalog anti-pattern instance or BLUEPRINT staleness), file a normal GitHub issue through the standard pipeline (see `skills/platforms/SKILL.md` for the mechanics) with enough detail — file:line, the catalog entry id if applicable, expected vs actual — that a later implementation session does not have to re-derive your reasoning. Do not batch everything into one mega-issue; one finding (or one tightly-related cluster) per ticket, same discipline as `t3:dogfooding-teatree`'s "dedupe aggressively, one root cause per ticket" rule.

## What NOT to do

- Do not re-check `detection: greppable` catalog entries — `check_antipatterns.py` already covers those on every PR; duplicating that work here wastes the review budget.
- Do not fix anything inline. This skill's output is tickets (and, for BLUEPRINT staleness, optionally a direct BLUEPRINT.md edit PR if the finding is purely a prose/staleness fix with no architectural judgment call — but any finding that touches an actual invariant or contested tradeoff goes through a ticket, not a unilateral edit).
- Do not re-litigate architecture-design's ten checks — those already ran when the reviewed code was written; this pass is about accumulated drift, not re-approving old decisions.
