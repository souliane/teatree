# Architecture pre-check — souliane/teatree#128 (generic chokepoint registry)

## 1. BLUEPRINT § alignment

Extends the §17.1-invariant-2 flywheel (enforcement-as-structure) and the
quality-catalog machinery (`antipatterns.yaml`/`regression_rules.yaml`): one
declarative registry {protected symbol -> sole allowed module} + one generic
AST checker for call-site authorization. Distinct from tach (import graph) and
semgrep (intra-body shapes).

## 2. FSM phase boundaries

n/a — no `Ticket.State`/`Worktree.State` transition touched.

## 3. Extension-point contracts

n/a — no `OverlayBase`/scanner/hook-router/`*Backend` Protocol surface changed.
The registry references existing symbols (`subprocess.*`, `post_routed`,
`react_routed`, `react`, `post_message`) but adds no new contract.

## 4. Component boundaries

- Registry data: `src/teatree/quality/chokepoints.yaml` (sibling of
  `antipatterns.yaml`/`regression_rules.yaml`).
- Loader: `src/teatree/quality/chokepoints.py` (mirrors `regression_catalog.py`
  — yaml + frozen dataclass + load-time validation, stdlib only).
- Generic checker: `scripts/hooks/check_chokepoints.py` (generalizes
  `check_subprocess_ban.py`'s AST visitor; registry-driven).
- Conformance test: `tests/quality/test_chokepoints.py` (mirrors
  `test_catalog.py`).

## 5. Dependency direction

`teatree.quality` is `layer=foundation`, `depends_on=["teatree.utils"]`. The
loader imports only stdlib + `yaml` — adds no tach edge. The reachability-ledger
assertions that touch `teatree.backends.slack_bot` live in the TEST file (tests
are not tach-constrained). `uv run tach check` stays green.

## 6. Test surface

`tests/quality/test_chokepoints.py`:

- schema invariants (ids unique/kebab, `match_kind` enum, non-empty
  `allowed_modules`/`protected_attrs`);
- reachability ledger (every `allowed_module` resolves to a real file; every
  `protected_attr` is a real attribute on its declaring class/module);
- green-on-tree (zero violations on real `src/teatree/` — the blocking gate);
- anti-vacuous (synthetic `subprocess.run` / `x.post_routed()` outside allowed
  -> rc 1; inside -> rc 0; `def post_routed` -> rc 0; annotation/except -> rc 0);
- loader validation (bad enum / dup id / non-kebab / empty allowed / non-mapping
  entry / non-sequence list / malformed YAML / missing-field rejected);
- self-maintenance Tier-2 (subprocess entry's `protected_attrs` superset of the
  historical `{run,Popen,check_output,check_call,call}`).

## 7. Resilience invariants

n/a — pure static-analysis gate, no external write, no DB row, no sub-agent.

## 8. Identity and key normalization

The canonical key is the fully-qualified dotted module path
(`teatree.utils.run`). `module_path_for(rel_path)` canonicalizes a scanned file
UP to its dotted path; `allowed_modules` are stored as dotted paths and compared
by identity — no `split`/`strip`-to-match seam.

## 9. Behavior preservation / capability deletion

Deletes `check_subprocess_ban.py` (+ test + pre-commit block) and
`tests/teatree_core/test_on_behalf_egress_import_guard.py`. The import-guard had
TWO invariants, BOTH preserved as registry entries:

- invariant 1 (`react_routed`/`post_routed` only inside `on_behalf_egress`)
  -> entry `on-behalf-routed-egress` (method-kind, allowed=`teatree.core.on_behalf_egress`);
- invariant 2 (`react`/`post_message` only at documented bot->user/self-ack
  sinks, with the receiver-is-egress carve-out) -> entry
  `on-behalf-colleague-primitives` (method-kind + `exempt_receivers:
  [egress, OnBehalfSlackEgress]`, allowed = the documented sink modules).
`exempt_receivers` is an optional refinement of the existing `method` kind, NOT
a third `match_kind` — the DSL stays two-valued. No must-block test inverted to
must-not-block. `os.system` deliberately NOT added (zero hits; the deleted ban
excluded it — flagged in the commit body).
DEFER (tracked follow-ups, would break green): httpx/requests (~15 modules),
gh/glab forge argv (different matcher), secrets `read_pass` (~9 modules),
merge-keystone (`merge_ticket_pr`/`record_merge_and_advance` are bare-name
function calls — neither `module_attr` nor `method`; registering needs a third
match_kind = scope creep). KEEP SEPARATE (term/diff scans, not call sites):
`check_no_overlay_leak.py`, banned-terms, privacy-push-scan.

---

## Architecture pre-check — fix(core): redis-slot + notify_user TOCTOU (#1886, merged into this branch)

### 1. BLUEPRINT § alignment

§6.6 "The DB is the arbiter for shared state" (ac-django transactions ref): read-modify-write on shared
state must hold an atomic guard for the whole RMW, or use an atomic conditional UPDATE / caught
IntegrityError. Both fixes mirror the existing CAS doctrine documented on `claim_next_pending` /
`LoopLease.acquire` (managers.py / loop_lease_manager.py).

### 2. FSM phase boundaries

n/a — no `Ticket.State` / `Worktree.State` transition is added or moved.

### 3. Extension-point contracts

n/a — no `OverlayBase` hook, scanner registration, or `*Backend` Protocol surface changes.
`allocate_redis_slot` consumers: the `worktree` provision command; `notify_user` is the single
notification egress.

### 4. Component boundaries

- `allocate_redis_slot` stays on `TicketQuerySet` (`src/teatree/core/managers.py`) — collection-level
  claim logic belongs on the manager, beside the sibling CAS claims.
- `notify_user` idempotency claim stays in `src/teatree/core/notify.py` — same chokepoint.

### 5. Dependency direction

No new imports beyond `IntegrityError` (already imported in notify.py) and `transaction` (already
imported in managers.py). No backwards edge. `uv run tach check` confirms.

### 6. Test surface

- `tests/teatree_core/test_redis_slots_concurrent.py` — two real threads on a file-backed SQLite
  registered with prod `SQLITE_WRITE_SERIALIZATION_OPTIONS` race to claim the lowest free slot; assert
  distinct slots, no `IntegrityError` escapes, the loser reselects. RED on current code (uncaught
  IntegrityError / duplicate slot).
- `tests/teatree_core/test_notify.py` — `TestNotifyUserConcurrentDedup`: two threads fire the same
  idempotency_key with a recoverable prior FAILED row; assert exactly one delivery + exactly one SENT
  BotPing row. RED on current filter→delete→deliver→create.

### 7. Resilience invariants

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

### 8. Identity and key normalization

n/a — no bare-vs-qualified identity. `redis_db_index` is an int slot; `idempotency_key` is already the
canonical key (unique constraint).

### 9. Behavior preservation / capability deletion

Both functions are tightened, not narrowed. `allocate_redis_slot` keeps: idempotent already-allocated
return, lowest-free selection, `RedisSlotsExhaustedError` on exhaustion, configurable count.
`notify_user` keeps: SENT no-op, FAILED/NOOP recoverable retry (#1306), never-raise on DatabaseError,
all hard-failure paths. The SENDING claim status is NOT a new permanent block: a stale SENDING (crashed
owner) is recoverable in both `claim_delivery` and the fallback, preserving the old code's self-healing
property for reused day-granular keys (`loops_tick_errors:{utc_day}`). The fallback's NOOP-not-recoverable
behavior is preserved (NOOP means no backend; fallback can't help). No must-block test inverted; no
privacy/security matcher touched.
