# Architecture pre-check — souliane/teatree#1880

## 1. BLUEPRINT § alignment

§17.1 invariant 2 (resilience: verify-by-re-read / idempotency / retry-safety on external writes)
and §5.6 (loop topology — the reactive Slack-answer cycle). Claim: a loop side-effect (Slack
react/reply) must land before its receipt is finalized, so a failed side-effect rolls the claim
back and retries, never leaves a lying receipt.

## 2. FSM phase boundaries

n/a — no `Ticket.State` / `Worktree.State` transition. The change is on the per-row CAS receipt
columns of `PendingChatInjection` (`eyes_reacted_at`, `loop_replied_at`), not the session FSM.

## 3. Extension-point contracts

No `OverlayBase` / scanner-registration / `*Backend` Protocol surface changes. `MessagingBackend.react`
is consumed unchanged. The fix is internal to `cycle.py` + two new rollback CAS methods on the
`PendingChatInjection` model.

## 4. Component boundaries

- `src/teatree/core/models/pending_chat_injection.py` — the rollback CAS methods (`unmark_eyes_reacted`,
  `unmark_loop_replied`) live on the model (Fat Model: the single-use CAS lives with the data, beside
  `mark_eyes_reacted` / `mark_loop_replied`).
- `src/teatree/loop/slack_answer/cycle.py` — the claim→side-effect→release-on-failure sequencing
  (loop body orchestration).

## 5. Dependency direction

No new imports. `cycle.py` already imports the model; the model gains no imports. No backwards edge.

## 6. Test surface

`tests/teatree_loop/slack_answer/test_cycle_internals.py`:

- eyes: `backend.react` raises → `eyes_reacted_at` is rolled back to NULL (row retries) — RED on pre-fix.
- eyes: success path stamps `eyes_reacted_at` exactly once.
- eyes: two concurrent attempts react exactly once (CAS exclusivity preserved).
- ack: `backend.react` raises → `loop_replied_at`/`answer_kind` rolled back for the whole unit.
- ack: success path stamps once.
`tests/teatree_core/test_pending_chat_injection_model.py`: the rollback CAS methods round-trip.

## 7. Resilience invariants

- verify-by-re-read: success path keeps the existing `verify_reply_visible` readback (SIMPLE) and now
  the eyes/ack receipt is only durable after the side-effect returns without raising.
- fallback-transport: n/a (the durable retry IS the fallback — the row stays in `loop_unreplied()`).
- idempotency: the CAS claim is the idempotency lock — a re-run / concurrent tick matches 0 rows.
  Rollback only fires on the claimant's own failure, so it cannot release another tick's claim.
- heartbeat: n/a (bounded per-cycle batch).
- sub-agent return contract: n/a.

## 8. Identity and key normalization

n/a — no bare-vs-qualified identity. Rows are keyed by pk in the conditional UPDATEs.

## 9. Behavior preservation / capability deletion

Purely additive sequencing change. Exactly-once (the CAS claim) is preserved unchanged; the only new
behavior is rollback-on-failure. No matcher narrowed, no must-block test inverted. `_handle_simple`
already had the correct post→verify→stamp order and is untouched. The WHEN semantics
(react-eyes-once, ack-only-when-classified-ACK) and dedup are unchanged.
