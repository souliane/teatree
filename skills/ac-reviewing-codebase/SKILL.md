---
name: ac-reviewing-codebase
description: Periodic holistic architectural review — the third of teatree's three review tiers (design-time `architecture-design`, per-PR deterministic `check_antipatterns.py`, periodic holistic `ac-reviewing-codebase`). Walks the whole tree for judgement-tier anti-patterns and BLUEPRINT.md staleness that no single diff can catch, implements what it finds, and pushes one PR. Dispatched automatically by `ArchitecturalReviewScanner` on a time or merge-count cadence — not user-invoked.
eval_exempt: whole-tree periodic synthesis with no fixed input/output pair to grade per-turn; correctness is judged by the merged fixes it produces over time, mirroring retro (#837)
requires:
  - architecture-design
  - review
compatibility: macOS/Linux, any teatree-managed repo.
metadata:
  version: 0.0.1
---

# ac-reviewing-codebase — Periodic Holistic Architectural Review

## Why this exists, and how it differs from the other two tiers

Three review tiers cover different scopes and cadences (BLUEPRINT.md § 17.2):

| Tier | When | Scope | Mechanism |
|---|---|---|---|
| `architecture-design` | Before code is written | The change about to be made | Ten-check pre-flight, worktree-local |
| `check_antipatterns.py` | Every PR | The diff | Deterministic grep over `grep_hint` entries |
| **`ac-reviewing-codebase` (this skill)** | Periodic (time or merge-count cadence) | The **whole tree** | Judgement pass, one Task per run, ending in one pushed PR |

The first two tiers are per-change and catch what a single diff introduces. Neither can see **drift that accumulates across many small, individually-fine changes** — a module that crept past the health threshold one function at a time, a BLUEPRINT section that quietly went stale as the code it describes moved on, a pattern that was fine in isolation twice and is now a repo-wide anti-pattern the third time. That is this skill's job.

You are dispatched by `ArchitecturalReviewScanner` (`src/teatree/loop/scanners/architectural_review.py`) as a headless `architectural_review`-phase Task, firing after `architectural_review_cadence_hours` (default 168h) or `architectural_review_after_merge_count` (default 25) merges, whichever comes first. There is no user prompt to parse — the trigger IS the instruction. Anchor to `Ticket.issue_url == "architectural-review://<overlay>"`, the synthetic per-overlay tracking ticket the scanner creates.

## Environment

Your tool grant is whatever `teatree.core.modelkit.phase_tools.tools_for_phase("architectural_review")` returns at dispatch time — that accessor is the authority, never prose about it. The pass needs file reads, tree search, web fetch, shell, and write/edit for § 7. You start in the overlay's **main teatree clone** (the dispatch resolves your `cwd` there). Read the tree, run read-only `git` (`git log`, merge-count since the last review) and `t3 tool verify-gates` there directly.

The main-clone guard blocks every mutation of the shared clone, so **all writing happens in your own worktree**: `git worktree add -b review-fixes/<slug> ../ac-review origin/main`. Cut it as soon as the walk turns up its first confirmed finding — the implementation pass (§ 7) runs there, and a heavy cold read is cheaper there too. If you find yourself without a checkout, or without one of the tools listed above, that is a dispatch fault, not a question for the owner: STOP and return `needs_user_input` with the reason (it is classified INTERNAL, never DM'd).

## What to do

### 1. Walk the judgement-tier anti-pattern catalog

`docs/generated/antipattern-catalog.md` (generated from `src/teatree/quality/antipatterns.yaml`) lists every known anti-pattern. Entries marked `detection: greppable` are already caught mechanically by `scripts/hooks/check_antipatterns.py` on every PR — skip those, they are not your job. Entries marked `detection: judgement` need a human-grade (or agent-grade) eye across the whole tree, because there is no reliable regex for them:

- Canonicalization that is not idempotent
- Identity matching that depends on the filesystem
- Security or merge gate fails open on exception
- Liveness path hard-fails a transient and locks the factory out
- Gate classifies read-vs-write by verb instead of effective mutation
- Feature merged but not in force
- Gate performs the guarded side effect before concluding refusal
- Destructive op reachable without its guard
- Authorization resting on a self-declared identity string
- Test function with no assertion
- Test that writes its own baseline / snapshot
- Test mocks the behaviour it is supposed to exercise
- Guard green only where the defect cannot appear
- Business logic in a view or management command
- FloatField for currency
- Module past the health threshold
- File placed outside the package whose concern it shares
- Lower-level module importing a higher-level one
- Overlay re-wraps a platform API instead of using the extension point
- List/fetch reads only the first page
- One item's exception aborts the whole sweep
- Long I/O inside the control-plane write transaction
- Absent, unreadable or stale signal reported as a definite verdict
- Command reports success on a failure it printed
- Work can stall indefinitely with nothing raising an alarm
- Fallback chain that hides the primary failure
- Same fact in two co-equal stores with no authority
- Multi-line comment block narrating what the code already says
- Documentation prose that restates the code instead of capturing architecture

That list is a derived cache of the catalog, refreshed by `tests/teatree_skill_support/test_ac_reviewing_codebase_method.py`, which turns red when the catalog grows a judgement entry this skill does not name. Re-read `docs/generated/antipattern-catalog.md` at review time anyway — it carries the `anti_pattern` / `preferred_pattern` pair the names omit. For each judgement entry, sample across the tree (you do not need to read every file; prioritize modules that changed since the last review — see § 5) and check whether the entry's `preferred_pattern` is actually followed.

Group what § 6 confirms by **root cause**, not per entry scanned and not per instance: instances one PR would close together are one unit of work, and an instance an already-open ticket covers belongs to that ticket rather than a near-duplicate — search the open backlog before filing anything (`AGENTS.md` § "Issue Creation" is canonical for that reuse rule). Each unit then goes down one of two paths: implemented in this pass (§ 7, the default) or filed as a ticket (§ 8, the exception). A ticket references the catalog entry id so the fixer has the anti-pattern/preferred-pattern pair without re-deriving it; a commit references it for the same reason.

### 2. Hunt the three defect classes that hide the best

Three shapes have produced this repo's recurring incidents, across subsystems that share no code, and a reviewer who is not told to hunt them does not find them. They are ordinary catalog entries; they are called out here because they are the highest-yield thing to look for and because two of them need a method the catalog cannot carry.

- **`unknown-reported-as-verdict`** — "I cannot see whether X" rendered as "X is false". The single most productive thing to hunt in this codebase. For every negative a subsystem reports, ask what it would have printed had the read failed, and whether a caller could tell the two apart.
- **`vacuous-guard`** — a test or gate that is green because the only path it exercises is the one where the defect cannot appear. **The method: for any guard cited as evidence of safety, name the mutation that would turn it red — then make that mutation and watch it go red.** A guard no mutation turns red IS the finding; do not report the code it wraps as protected. A previous pass found roughly a dozen.
- **`shipped-inert`** — a feature merged but switched off, or a setting whose only safe value is held in place by a separate gate: code that reads as protection while not being in force. **The method: read the live value, never the default in the source.**

### 3. Re-check the standing shapes every pass

Nearly every finding of the first full-tree pass was one of a small number of shapes. They are cheap to re-check and they recur, so each pass starts from the accumulated set instead of rediscovering it:

`unknown-reported-as-verdict`, `vacuous-guard`, `shipped-inert`, `gate-fails-open-on-error` (its empty-input half as much as its exception half), `silent-success-on-failure`, `silent-freeze`, `long-io-holds-control-lock`, `destructive-op-outside-its-guard`, `self-declared-identity-authorization`, `silent-truncation-pagination`.

The set accumulates in `src/teatree/quality/antipatterns.yaml`, never in this skill's prose: a pass that confirms a recurring shape the catalog does not name adds the entry in its own PR (§ 7). Two co-equal lists of the same shapes would be `multi-store-no-arbiter`, which is itself on the list above.

### 4. Check BLUEPRINT.md tightness and staleness

Per BLUEPRINT.md's own `## Maintenance` section: "Keeping the file tight is a reviewer responsibility — flag bloat, prose that restates code instead of capturing architecture, and stale or duplicated sections — captured in `skills/review/SKILL.md` § 'Keep BLUEPRINT Tight' and in the periodic holistic review (this skill)." Load `skills/review/SKILL.md` § "Keep BLUEPRINT Tight" for the three-point checklist (restated-not-architectural prose, stale/duplicated sections, appendix-class detail in the top-level file) and apply it to the **whole file**, not a diff — this is the one place that checklist runs at full-tree scope instead of per-PR scope. Cross-check every section against the current code it describes; a section naming a mechanism that has since moved, been renamed, or been removed is a staleness finding. A staleness finding is the clearest case for § 7 — the current code is right there and the correction is a prose edit, so it lands in this pass's PR unless the prose is wrong about an invariant someone must decide.

### 5. Prioritize by what changed since the last review

You do not have unlimited budget to re-read the entire tree every cadence. Scope your attention using the same signal the scanner uses to decide *whether* to fire: `TicketTransition` rows into `_MERGED_STATES` (`merged`, `delivered`) since the last completed `architectural_review` Task's `Session.started_at`. Prioritize modules touched by those merges — that is where new drift is most likely to have landed. A full cold read of untouched, previously-clean areas is lower priority than re-checking what actually moved.

### 6. Verify every finding before it ships — the default is refuted

The most expensive output of a whole-tree pass is not a missed defect; it is a **plausible-but-wrong finding**, which costs the next reader a day of reading before it dissolves. Both prior passes produced them: stale line numbers, scenarios a caller the finder never read makes impossible, severities past what the code supports.

So verification is its own step, with a different reader:

1. **Every finding starts refuted.** It ships only if the verifier **positively confirms** it by re-reading the cited code — "could not refute it" is not confirmation.
2. **The verifier is a distinct reviewer** — one sub-agent per finding is the cheap form (dispatch it with `t3 <overlay> skill-preamble`, or it carries none of this). Hand it the claim and the citation and ask it to KILL the finding. Do not hand it the finder's reasoning: that reasoning is exactly what makes a wrong finding look sound.
3. **Severity is downgradable here, freely.** A "critical" that needs three unlikely coincidences is not critical. The verifier sets the final severity; the finder does not defend it.
4. **Anything not positively confirmed does not ship** — not as a lower-confidence finding, not as a "worth a look" note. It is dropped, and § "Report coverage" says how many were.

Maker≠checker already applies to the code under review. This applies it to the review's own output, one step before the merge gate applies it again.

### 7. Implement the confirmed findings and push one PR

**The run ends in a pushed PR, not a report.** This holds identically in the headless factory and in an attended session: either way the pass is not done until the branch is on the remote and a PR is open. A run that ends with only a findings list has not finished.

In the worktree you cut in § Environment:

1. Implement each confirmed unit of work from § 1 through § 4, plus any new catalog entry § 3 owes. Follow `skills/code/SKILL.md` — a failing test first wherever the behaviour is testable, observed RED before the fix; a BLUEPRINT/appendix staleness fix is a prose change with no test.
2. Commit per unit, so the history reads as one coherent change per root cause. Cite the catalog entry id (and the ticket, where one exists) in the commit body.
3. Run the affected-tests lane (`bash dev/test-affected.sh`) and `uv run ruff check`, both green, before pushing.
4. Push the branch — never `--no-verify`. The pre-push hook runs `t3 <overlay> pr ensure-pr`, which on a first push owes a durable `PendingPullRequest` instead of opening a PR against a remote ref that does not exist yet; re-run `t3 <overlay> pr ensure-pr` once the push lands so this pass discharges its own obligation rather than leaving it for the dispatch loop. Raw `gh pr create` stays forbidden (`skills/ship/SKILL.md` § "pr create is mandatory").
5. Report the branch, the pushed SHA, the PR url, and the per-finding disposition in your result envelope.

`t3 <overlay> pr create` is not this pass's path, and forcing it is not the fix. It IS the FSM ship transition, so it demands a `Worktree` row the § Environment `git worktree add` never creates, and its shipping gate requires the ticket to have visited `testing` and `reviewing` (`teatree.core.management.commands._ship.gates.check_shipping_gate`). A cadence-anchor ticket only visits `architectural_review`, which `teatree.core.modelkit.phases.normalize_phase` maps to itself — so recording a `reviewing` visit here would attest the cold review that § "Maker≠checker" deliberately puts *after* this PR exists. `pr create`'s own docstring names `pr ensure-pr` as the seam for a checkout that needs a PR without the ship transition.

One PR carries the whole batch. `/t3:rules` § "Fewest PRs for Related Work" applies — splitting needs the owner's up-front approval, so the default is one.

### 8. File a ticket only for what this pass cannot implement

Filing is the **exception**, not the default. A unit of work goes to a ticket instead of the PR only when implementing it needs a decision this pass cannot make alone:

- it turns on an **architectural decision** — two defensible designs, a contract change other overlays consume, a migration whose shape the owner picks;
- it is **contested** — the catalog's preferred pattern is arguable here, or the code is deliberately the way it is and the reason is not in reach.

Both are decidable from the read, before a line is written. Size is not a third reason: "too large to land coherently" is a judgement you cannot reach until you have already implemented the thing, so it would certify whatever you did — including nothing.

For those, file a normal GitHub issue through the standard pipeline (see `skills/platforms/SKILL.md` for the mechanics) with enough detail — file:line, the catalog entry id if applicable, expected vs actual — that a later session does not re-derive your reasoning, **plus the reason it was not implemented here**, or the next reader rediscovers the same blocker. Each ticket needs the owner's approval before it is created (`AGENTS.md` § "Issue Creation"); present that batch and let them decide. "I ran low on budget" is not one of the two reasons — say so plainly in the envelope instead, so the shortfall is visible as a shortfall.

## Method notes that decide whether an answer is right

Small, and each has produced a wrong answer here at least once:

- **Read paginated.** A list or fetch that takes the default page silently truncates. Concretely: this repo's PR check-runs return 30 of ~43 unpaginated, so a red PR reads green. Page any read you will reason about, and treat a result exactly the page size as unfinished — the pass commits `silent-truncation-pagination` as readily as the code it is auditing does.
- **Prefer parallel subsystem readers over one sequential pass.** Wall-clock aside, independent readers do not inherit each other's assumptions, and where two disagree about the same subsystem the disagreement is itself worth reading.
- **Probe from the place that matters.** A reading taken in the wrong container, worktree or cwd is an artifact, not a fact. One week produced two convincing false defects that way: a probe run inside a memory-capped container, and a probe run from a worktree auto-isolated onto a different database. Before reporting any environment-derived number, confirm the venue it was read from is the one the claim is about.

## Report coverage, and say what you did not cover

Two statements are what make a verdict auditable. Without them, a subsystem that was skipped and a subsystem that was read and found healthy produce the identical report line.

- **Every subsystem verdict states what you checked.** "Subsystem X is clean" on its own is unfalsifiable. Name the modules read, which shapes from § 2 and § 3 you looked for, and — for any guard you accepted as evidence of safety — the mutation you named for it. A verdict carrying none of that is not a verdict; do not emit one.
- **Say what the pass did not cover.** Sampling, a subsystem skipped, a budget exhausted, a tool or credential absent, findings dropped at § 6 — all of it goes in the envelope. Bounding your own coverage silently reads as "everything was covered", which is `unknown-reported-as-verdict` committed by the review itself.

## Maker≠checker holds at the merge gate, not at the keyboard

This pass reviews and then writes, so the reviewer is the maker of the resulting PR. That hazard is real: an author is the worst judge of their own diff, and the reasoning that produced a finding is exactly the reasoning that will look sound to whoever acted on it.

The mitigation is where the maker≠checker boundary actually sits — the **merge gate**. The PR this pass pushes is an ordinary PR: it needs an **independent cold review** before merge, by a reviewer who did not run this pass and reads the diff without its author's framing, and it merges only through the §17.4 CLEAR/merge keystone like every other PR. Nothing lands on this pass's own say-so.

So do not "restore" a rule forbidding this pass from writing code. Refusing to write never protected maker≠checker — the independent merge gate did, and it is untouched. Refusing to write bought only a backlog of filed-and-forgotten tickets, which is the deferral `AGENTS.md` First Principles 8-10 exist to prevent.

## What NOT to do

- Do not re-check `detection: greppable` catalog entries — `check_antipatterns.py` already covers those on every PR; duplicating that work here wastes the review budget.
- Do not end the run on a report. A findings list with no pushed branch is an unfinished pass, in the factory and in an attended session alike.
- Do not file a ticket for a finding you could have implemented. Filing is for the two cases in § 8, and each ticket says which one applies.
- Do not report a finding § 6 did not positively confirm. "Probably real" ships as nothing.
- Do not emit a subsystem verdict without saying what you read, and do not bound your own coverage without saying what you left out.
- Do not merge your own PR, and do not treat this pass's review as its cold review. The independent reviewer at the merge gate is the whole reason this pass is allowed to write.
- Do not re-litigate architecture-design's ten checks — those already ran when the reviewed code was written; this pass is about accumulated drift, not re-approving old decisions.
