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

- verify-by-re-read: the slot claim does NOT re-read a single row — it re-queries the taken-set each
  iteration and uses the unique constraint as the CAS token, catching `IntegrityError` and reselecting
  on a collision. The notify claim (`BotPing.claim_delivery`) re-reads the dedup row under
  `select_for_update` inside one `transaction.atomic` and commits the SENDING claim there; delivery
  (the Slack post) happens AFTER that atomic block, outside the lock, then the row is finalized to
  SENT/FAILED.
- idempotency: notify_user stays idempotent on SENT; allocate_redis_slot stays idempotent on an
  already-allocated ticket.
- fallback-transport: a stale SENDING claim (owner crashed before finalize) is recoverable in BOTH the
  primary claim and the fallback (`BotPing.is_stale_sending` is the shared staleness SSOT), so one crash
  mid-delivery cannot permanently block a reused day-granular key; a fresh SENDING still blocks.
- heartbeat / sub-agent return contract: unchanged.

## 8. Identity and key normalization

n/a — no bare-vs-qualified identity. `redis_db_index` is an int slot; `idempotency_key` is already the
canonical key (unique constraint).

## 9. Behavior preservation / capability deletion

Both functions are tightened, not narrowed. `allocate_redis_slot` keeps: idempotent already-allocated
return, lowest-free selection, `RedisSlotsExhaustedError` on exhaustion, configurable count.
`notify_user` keeps: SENT no-op, FAILED/NOOP recoverable retry (#1306), never-raise on DatabaseError,
all hard-failure paths. The SENDING claim status is NOT a new permanent block: a stale SENDING (crashed
owner) is recoverable in both `claim_delivery` and the fallback, preserving the old code's self-healing
property for reused day-granular keys (`loops_tick_errors:{utc_day}`). The fallback's NOOP-not-recoverable
behavior is preserved (NOOP means no backend; fallback can't help). No must-block test inverted; no
privacy/security matcher touched.
