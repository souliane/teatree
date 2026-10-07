# BLUEPRINT Appendix — Self-review HOLD disposition (#5076)

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §4 ("A self-review HOLD stops the ticket in TESTED"). The rule it implements: an author's self-review findings are fixed before anything ships. They are never posted on the PR and never DM'd.

## Where the verdict lives

- A self-review runs before any pull request exists, so no `ReviewVerdict` row can hold it. The returned `review_verdict` stays on the reviewing task's `TaskAttempt.result`, and `core.models.self_review.SelfReview` reads it back.
- The newest error-free attempt that carries a verdict governs. An attempt that also set `needs_user_input` concluded nothing, so it is not a verdict.
- Only self-reviews recorded after the ticket last left a shipped state count. That boundary is the newest `TicketTransition` edge out of `PR_OPENED`, `REVIEW_REQUESTED`, `MERGED`, `RETRO_RECORDED` or `DELIVERED`, so a follow-up reopened from a delivered ticket is not held by the previous delivery's HOLD.

## The disposition

- A HOLD on a `TESTED` ticket does not fire `review()`. The ticket stays `TESTED`, and `queue_self_review_rework` mints one coding task parented on the review. Its reason carries the reviewed SHA and every finding whole: severity, `file:line`, summary and failure scenario. The reason is capped at 16,000 characters, with a pointer to the reviewing task when the cap binds.
- The parent link makes the disposition idempotent, so the replay sweep never mints a second rework.
- Only that rework discharges the HOLD: its completion fires `address_self_review` (`TESTED`/`SELF_REVIEWED` → `CODED`), so the fix is re-tested and re-reviewed. Any other coding completion, or a shipping completion, on a held ticket queues the rework instead. That queue is keyed on reworks newer than the triggering completion, so a rework failed or cancelled earlier is replaced once and a replay never mints again.
- A failed rework is unfinished work, not a superseded row. `Ticket.has_completed_phase` and `phase_output_reached` answer "coding is not over" while `owes_self_review_rework()`, so the transient-requeue sweep reopens or escalates it instead of retiring it as done.
- `anti_vacuity` is merge_safe evidence; a HOLD owes none, and the reviewer brief says so.

## The bound

When the ticket already has `max_phase_iterations()` other HOLD self-reviews in this delivery cycle, no rework is queued. It counts what a lap spends: one reviewing task per lap, counted by that task's newest verdict. Attempt rows and merge_safe reviews never count, and a re-review of an unchanged head counts like any other, so a rework that commits nothing cannot loop. One INTERNAL `DeferredQuestion` (marker `self-review-hold-cap:<pk>`) records it with the findings. The question is sticky across answered and dismissed rows, so it is never re-raised and never pages the owner.

## Gates and overrides

- `pr create`'s shipping gate refuses a pre-ship ticket whose current-cycle self-review is a HOLD.
- The operator overrides pass a HOLD by design: `ticket transition review` / `reconcile_reviewed`, and `pr create --skip-validation`.

## What a HOLD means once a PR is open

The disposition keys on FSM state, not on PR rows. The no-orphan push hook opens a PR before `ship()`, so a pre-ship ticket can have one. While the ticket is pre-ship, a HOLD still routes its findings to the rework. Once the ticket ships, the PR-keyed review governs and the self-review HOLD refuses nothing.

## Recovery verb

`t3 <overlay> ticket rework-hold <id> [--dry-run] [--json]` re-queues the rework for a ticket parked in `tested` or `self_reviewed` past a HOLD.

- It supersedes the ticket's active tasks and lists each one, dry run included.
- A re-run returns the rework already in flight.
- It refuses, writing nothing:
  - a reviewer ticket;
  - any other state;
  - a latest review that is not a HOLD;
  - a CLAIMED task it would fail;
  - an open PR, whether recorded or reported by the forge for one of the ticket's branches. It also refuses when the forge cannot be asked, including when a branch's checkout is not on disk.
- The open-PR refusal applies to the verb alone, because the verb fails active tasks, and an open PR usually has delivery work in flight.
