# The verdict envelope — rubric grades

## The Envelope Also Carries the Rubric Grades (Non-Negotiable)

The rubric done-gate is unconditional, and the cold reviewer is its ONLY automatic producer — you are the independent verifier it requires (grader ≠ maker) and the one actor that read the tree. So the verdict envelope carries `rubric_grades` beside `findings`, and `agents/review_envelope_recorder.record_returned_review_envelope` stamps them:

```json
"rubric_grades": [{"ordinal": 0, "status": "pass", "rationale": "<the test that proves it>"}]
```

Grade **every** criterion the brief's `TICKET RUBRIC` block lists. A verdict leaving one ungraded is refused and records **nothing** — not the verdict, not the grades — because recording it would retire the head's review claim while the merge stayed refused, leaving the head unmergeable with nobody left to re-arm it. A `pass` cites the test that proves the criterion (any kind, free-form prose); a `fail` needs no citation and no bypass overrides it. No `TICKET RUBRIC` block means the reviewed PR has no rubric and no grades are owed.

`t3 <overlay> ticket rubric-grade` stays the **operator's** manual seam. Do not reach for it as the reviewer: a grade written there lands outside the transaction that records your verdict, so a refused grade could no longer roll the verdict back.

**Record the review's own evidence, or the gates that read it can never be armed.** Three
producers belong to this phase, and each writes the row a gate looks for — a gate whose evidence
nobody is instructed to produce can only ever be armed into blocking every ticket, which is how
twelve of them sat dark for up to 85 days:

```bash
# the integration-review evidence `require_integration_review` reads
t3 <overlay> review record-evidence <ticket-id> --head-sha "$(git rev-parse HEAD)"

# the review-context `require_review_context` reads
t3 <overlay> lifecycle record-review-context <ticket-id> --head-sha "$(git rev-parse HEAD)"

# a verdict recorded WITHOUT --ticket-id does not satisfy
# `require_reviewed_state_for_review_request` — pass it every time
t3 <overlay> review record --ticket-id <ticket-id> ...
```
