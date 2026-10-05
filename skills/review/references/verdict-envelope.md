# The verdict envelope — rubric grades

## The Envelope Also Carries the Rubric Grades (Non-Negotiable)

The rubric done-gate is unconditional, and the cold reviewer is its ONLY automatic producer — you are the independent verifier it requires (grader ≠ maker) and the one actor that read the tree. So the verdict envelope carries `rubric_grades` beside `findings`, and `agents/review_envelope_recorder.record_returned_review_envelope` stamps them:

```json
"rubric_grades": [{"ordinal": 0, "status": "pass", "rationale": "<the test that proves it>"}]
```

Grade **every** criterion the brief's `TICKET RUBRIC` block lists. A verdict leaving one ungraded is refused and records **nothing** — not the verdict, not the grades — because recording it would retire the head's review claim while the merge stayed refused, leaving the head unmergeable with nobody left to re-arm it. A `pass` cites the test that proves the criterion (any kind, free-form prose); a `fail` needs no citation and no bypass overrides it. No `TICKET RUBRIC` block means the reviewed PR has no rubric and no grades are owed.

`t3 <overlay> ticket rubric-grade` stays the **operator's** manual seam. Do not reach for it as the reviewer: a grade written there lands outside the transaction that records your verdict, so a refused grade could no longer roll the verdict back.

**Return the review's own evidence in the result envelope.** The recorder writes
`review_context` (the fetched work item, downloaded documents, and analysis),
`anti_vacuity` (AC coverage and RED proofs or `no_new_tests`), and, for a combined
changeset, `integration_review.repos` covering every ticket repo. It binds the
verdict to the PR's owning ticket, satisfying the reviewed-state gate. A
merge-safe result missing required evidence fails before the task completes.

For a manual review, the equivalent commands remain available:

```bash
# the integration-review gate reads
t3 <overlay> review record-evidence <ticket-id> --head-sha "$(git rev-parse HEAD)"

# the review-context gate reads
t3 <overlay> lifecycle record-review-context <ticket-id> --work-item <url> --documents <urls> --analysis <how-checked>

# a verdict recorded WITHOUT --ticket-id does not satisfy
# the reviewed-state gate — pass it every time
t3 <overlay> review record --ticket-id <ticket-id> ...
```
