# The verdict envelope — rubric grades

## The Envelope Also Carries the Rubric Grades (Non-Negotiable)

The rubric done-gate is unconditional, and the cold reviewer is its ONLY automatic producer — you are the independent verifier it requires (grader ≠ maker) and the one actor that read the tree. So the verdict envelope carries `rubric_grades` beside `findings`, and `agents/review_envelope_recorder.record_returned_review_envelope` stamps them:

```json
"rubric_grades": [{"ordinal": 0, "text_hash": "<hash from brief>", "status": "pass", "rationale": "<the test that proves it>"}]
```

Grade **every** criterion in `TICKET RUBRIC`, using its `text_hash`. Missing one refuses the verdict and all grades, preserving the review claim for retry. A `pass` cites any test in free-form prose; a `fail` needs no citation and no bypass overrides it. No block means no grades are owed.

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
