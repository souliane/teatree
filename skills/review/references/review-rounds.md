# Review rounds on one MR

The rules behind `/t3:review` § "Receiving Code Review" for a multi-round review of one MR.

- **Fix the defect class each round.** Grep every instance and consumer, then report a consumer table with each disposition; fixing only cited lines does not close the finding.
- **Carry review context forward.** Give every returning reviewer the prior findings and decisions, including after a full-read fallback; verify a delta without reopening settled calls unless new evidence invalidates them.
- **Cap review passes.** Three recorded, independent verdicts across the owning ticket and PR ledger exhaust the automatic review budget, including Codex. A copied HOLD or conflict-only carry does not spend a pass. The cap leaves the PR held and reports the ticket, PR and three reviewed SHAs to the owner. `RoundMetric.rounds_per_ticket("review")` measures rounds; it is not the cap. A new head with a recorded ancestor verdict gets the orchestrator's delta brief: prior findings, decisions and rubric grades, plus PR-side commits since that head. Re-grade rows touched by the delta or lacking citable prior evidence; a pass citing only tests also needs a fresh grade when production changes. Other carried passing rows keep their original grader and rationale. A named full-review fallback applies when lineage, base or git evidence is unsuitable.
- **Keep venue limits separate from findings.** A venue that cannot read CI, run needed tests or open the plan returns `review_venue_limit` without a verdict. A code or checks blocker can support a hold.
