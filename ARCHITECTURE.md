# Architecture pre-check — fix(core): redis-slot + notify_user TOCTOU

## 1. BLUEPRINT § alignment

§6.6 "The DB is the arbiter for shared state" (ac-django transactions ref): read-modify-write on shared
state must hold an atomic guard for the whole RMW, or use an atomic conditional UPDATE / caught
IntegrityError. Both fixes mirror the existing CAS doctrine documented on `claim_next_pending` /
`LoopLease.acquire` (managers.py / loop_lease_manager.py).

## 2. FSM phase boundaries

n/a — no `Ticket.State` / `Worktree.State` transition is added or moved.

## 3. Extension-point contracts

n/a — no `OverlayBase` hook, scanner registration, or `*Backend` Protocol surface changes.
`allocate_redis_slot` consumers: the `worktree` provision command; `notify_user` is the single
notification egress.

## 4. Component boundaries

- `allocate_redis_slot` stays on `TicketQuerySet` (`src/teatree/core/managers.py`) — collection-level
  claim logic belongs on the manager, beside the sibling CAS claims.
- `notify_user` idempotency claim stays in `src/teatree/core/notify.py` — same chokepoint.

## 5. Dependency direction

No new imports beyond `IntegrityError` (already imported in notify.py) and `transaction` (already
imported in managers.py). No backwards edge. `uv run tach check` confirms.

## 6. Test surface

- `tests/teatree_core/test_redis_slots_concurrent.py` — two real threads on a file-backed SQLite
  registered with prod `SQLITE_WRITE_SERIALIZATION_OPTIONS` race to claim the lowest free slot; assert
  distinct slots, no `IntegrityError` escapes, the loser reselects. RED on current code (uncaught
  IntegrityError / duplicate slot).
- `tests/teatree_core/test_notify.py` — `TestNotifyUserConcurrentDedup`: two threads fire the same
  idempotency_key with a recoverable prior FAILED row; assert exactly one delivery + exactly one SENT
  BotPing row. RED on current filter→delete→deliver→create.

## 7. Resilience invariants

- verify-by-re-read: the slot claim re-reads via `refresh_from_db`; the notify dedup re-reads the row
  under the same atomic block before delivering.
- idempotency: notify_user stays idempotent on SENT; allocate_redis_slot stays idempotent on an
  already-allocated ticket.
- fallback-transport / heartbeat / sub-agent return contract: unchanged.

## 8. Identity and key normalization

n/a — no bare-vs-qualified identity. `redis_db_index` is an int slot; `idempotency_key` is already the
canonical key (unique constraint).

## 9. Behavior preservation / capability deletion

Both functions are tightened, not narrowed. `allocate_redis_slot` keeps: idempotent already-allocated
return, lowest-free selection, `RedisSlotsExhaustedError` on exhaustion, configurable count.
`notify_user` keeps: SENT no-op, FAILED/NOOP recoverable retry (#1306), never-raise on DatabaseError,
all hard-failure paths. No must-block test inverted; no privacy/security matcher touched.
