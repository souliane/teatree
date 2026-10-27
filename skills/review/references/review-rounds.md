# Review rounds on one MR

The rules behind `/t3:review` § "Receiving Code Review" for a multi-round review of one MR.

- **Fix the defect class each round.** Grep every instance and consumer, then report a consumer table with each disposition; fixing only cited lines does not close the finding.
- **Carry review context forward.** Give every returning reviewer the prior findings and decisions, including after a full-read fallback; verify a delta without reopening settled calls unless new evidence invalidates them.
- **Measure review rounds.** `RoundMetric.rounds_per_ticket("review")` reports the count; a new head with a recorded ancestor verdict gets the orchestrator's delta brief with prior findings, decisions, rubric grades and PR-side commits since that head. Re-grade rows touched by the delta or lacking a cited file; carried passing rows keep their original grader and rationale. A named full-review fallback applies when lineage, base or git evidence is unsuitable.
- **Keep venue limits separate from findings.** A venue that cannot read CI, run needed tests or open the plan returns `review_venue_limit` without a verdict. A code or checks blocker can support a hold.
