# BLUEPRINT Appendix — Domain Models

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §4. Sections §4.1–§4.5 retain their numeric anchors so consumer cross-references like `BLUEPRINT §4`, `§4.1`, `§4.3` resolve here.

## 4. Domain Models

Five core lifecycle models in `teatree.core.models/` (split into domain-specific modules), all using `django-fsm` for state machines: **Ticket**, **Worktree**, **Session**, **Task**, **TaskAttempt**. Nine supporting rows live alongside them — `ReviewAssignment` (reaction-driven review audit row keyed `unique(overlay, mr_url, user_id)` — states `pending | approved`; `approve_for_mr(mr_url)` bulk-advances all linked rows when an approve reaction lands, capturing the full reaction → review → approval cycle; #1047), `PullRequest` (denormalized PR cache populated by sync), `TicketTransition` (audit trail of FSM moves), `WorktreeEnvOverride` (user-declared env entries layered onto the env cache), `DailyDigestThread` + `DailyDigestMessage` (one rolling Slack DM thread per digest day — rolls at 08:00 local time, configured `TIME_ZONE`, hour overridable via `TEATREE_DAILY_DIGEST_ROLL_HOUR`, #654 phase 8; `teatree.core.daily_digest.DailyDigest` opens the day's thread on first post (new `DailyDigestThread` row + Slack root message), threads every later message under it, and `close_with_recap()` posts the end-of-day recap and stamps `closed_at`; `DailyDigestMessage.idempotency_key` is unique so a retried post is a no-op. Standalone over the `MessagingBackend` — not routed through `Replier`/`ReplyDispatch` since digest posts have no originating `IncomingEvent`. `MessagingBackend` gained `open_dm`; all implementers (`SlackBotBackend`, `NoopMessagingBackend`) updated), `IncomingEvent` (canonical ingestion record for external webhook traffic — Slack, GitLab, GitHub, Notion, CI; receivers under `POST /hooks/<platform>/` verify the platform-specific authentication (Slack `X-Slack-Signature` HMAC + replay window, GitLab `X-Gitlab-Token` shared secret, GitHub `X-Hub-Signature-256` HMAC) and persist via the shared `IngestionRecord` helper. Post-auth, each receiver consults a process-local per-source token bucket (`teatree.core.views._rate_limit.webhook_rate_limiter`, capacity/refill from `TEATREE_WEBHOOK_RATE_*` settings) and returns `429` once a source's bucket is empty, so a misconfigured-platform retry storm cannot fill the DB. The bucket is per-process — under a multi-worker WSGI server the effective ceiling is `capacity × workers`; teatree assumes the single-process dev/loop topology. It is a DB-bloat guard (bounded trickle, not a precise quota); unauthenticated floods already 401 before any DB write and are intentionally not bucketed. Unique `idempotency_key` makes retries safe; a `processed_at` clock is advanced by the consumer queue), `IntentClassification` (pattern-based verdict on an `IncomingEvent` produced by `teatree.core.intent_classifier.classify_event()` — six intents `task | question | approval | status_update | escalation | noise`, one-to-one with the event, idempotent re-runs), and `ReplyDispatch` (audit row for every outbound message published through a `Replier` — `pending | sent | failed | dead_letter`, unique `idempotency_key` collapsing retries; production `SlackReplier`/`GitLabReplier`/`GitHubReplier` subclass a shared `_BaseReplier` whose idempotent `_send` records `sent` on success or `failed` + `error_message` on any backend exception, with `_deliver` the single per-platform hook; `replier_for(source, *, bot/gitlab/github)` picks the production subclass or falls back to `NoopReplier` when the matching backend is not injected; the row persists `body` + `retry_count`/`next_retry_at` so `teatree.core.reply_retry.sweep_failed_dispatches(resolver=…)` can retry `due_for_retry()` rows via `Replier.redeliver`, backing off `base_delay·2**retry_count` and at `max_retries` marking `dead_letter` + DMing the originating actor — the alert row's `action_name="dead_letter_alert"` is excluded from the sweep so a broken DM channel cannot storm). `core/models/errors.py` and `core/models/types.py` carry shared exceptions and TypedDicts (no DB tables). The pure-function router `teatree.core.event_router.route_event(event, classification)` turns each classified event into a `RoutedAction` (`schedule_task | schedule_merge | alert_user | record_only | drop`) for the loop/agent layer to execute. The `IncomingEventsScanner` (registered alongside `PendingTasksScanner` in `build_default_jobs`) drains the unprocessed queue on every tick — classify → route → execute → mark processed — with a per-event try/except so one corrupt row doesn't block the rest, and four new `_STATUSLINE_ZONE_BY_KIND` entries (`incoming_event.{alert,task_needed,merge_needed,recorded}`) so the emitted signals are visible. The loop dispatcher (`teatree.loop.dispatch`) routes an `incoming_event.task_needed` signal whose phase normalizes (`teatree.core.phases.normalize_phase`) to `answering` to the `t3:answerer` agent plus a statusline mirror (the reviewer dual-dispatch shape, #670); it resolves `require_human_approval_to_answer` once through the standard active-overlay → global → default chain (mirroring `require_human_approval_to_merge` — no env-var layer for this setting) and stamps it into the agent payload as an advisory convenience mirror; the answerer skill re-resolves the setting at task start (`skills/answerer/SKILL.md` § Autonomy Gate) and is the source of truth, so the stamp is a hint, not authoritative. `coding`-phase `task_needed` signals keep their prior statusline-only behaviour (auto ticket creation from inbound chat is a separate decision pass). `SCHEDULE_MERGE` actions first call `OverlayBase.can_auto_merge(target_ref, thread_ref) → MergeGuard` (#654) — the default implementation is permissive (`allowed=True`); overlays that need approval gates or freeze-window checks override it. Three outcomes: `guard.allowed` → `incoming_event.merge_needed`; `not allowed and guard.escalate` → `incoming_event.merge_escalation`; `not allowed` → `incoming_event.merge_blocked`. All three carry the same merge refs in their payload (`event_id`, `target_ref`, `thread_ref`; the two blocking outcomes also carry `reason`) and all three signal kinds are registered in `_STATUSLINE_ZONE_BY_KIND` as `"action_needed"`. `GitLabApprovalsScanner` (#936) is the poll-driven complement to this webhook-driven path — for deployments where Slack Connect blocks the OperCodeReviewBot from joining `#the-review-crew` or the GitLab webhook is not wired up, the scanner polls `CodeHostBackend.get_mr_approvals` per tick and emits the same `incoming_event.merge_{needed,blocked,escalation}` signal through the same `can_auto_merge` guard, so the §17.4 keystone merge transition is the single point of merge-decision regardless of which transport surfaced the approval.

**Transitions own their work.** Every FSM transition composes the runners needed to make its new state true — git, PR I/O, retro writing, cleanup — and enqueues long work to an `@task` worker via `transaction.on_commit`. Transition bodies stay pure (state change + metadata + enqueue); the worker does the I/O, takes a row lock with `select_for_update()`, re-checks the source state for idempotency, and on success calls the next transition to advance the ticket. The single rule: to move the ticket, call the transition; the transition does the rest.

Rationale: at-least-once delivery is safe because workers guard with row-locked state checks; crash recovery is `django-tasks`' job, not ours; tests use `ImmediateBackend` to run workers synchronously. `post_transition` signals remain reserved for lossy cross-cutting side effects (audit log, Slack reactions) — never for the main work of the transition.

### 4.1 Ticket — Core delivery entity

The central entity. One ticket per unit of work (maps to an issue/task in the tracker).

**States:** `not_started` → `scoped` → `started` → `coded` → `tested` → `reviewed` → `shipped` → `in_review` → `merged` → `retrospected` → `delivered`

**Fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `issue_url` | URLField(500) | Link to tracker issue (blank for manual tickets) |
| `overlay` | CharField(255) | Overlay name (entry point name from `teatree.overlays`) |
| `variant` | CharField(100) | Tenant/variant identifier (e.g., "acme") |
| `repos` | JSONField(list) | Repository names involved |
| `state` | FSMField | Current lifecycle state |
| `extra` | JSONField(dict) | Extensible metadata (PRs, labels, test results) |
| `context` | TextField | Append-only durable knowledge store — timestamped notes the agent reuses across sessions (`t3 <overlay> ticket context show\|add\|edit`; rendered collapsed in the `workspace ticket` intake) |

**Transitions:**

| Method | Source → Target | Side effects |
|--------|----------------|--------------|
| `scope(issue_url=, variant=, repos=)` | not_started → scoped | Sets issue_url, variant, repos |
| `start()` | scoped → started | Enqueues `execute_provision` worker. Worker runs `WorktreeProvisioner` and calls `schedule_coding()` on success. |
| `code()` | started → coded | Clean-worktree preflight (see §4 note); calls `schedule_testing()` |
| `test(passed=True)` | coded → tested | Clean-worktree preflight; stores `tests_passed` in extra; calls `schedule_review()` |
| `review()` | tested → reviewed | Condition: reviewing task completed. Clean-worktree preflight. Calls `schedule_shipping()` only if `has_shippable_diff()` returns True (otherwise stamps `extra["shipping_skipped"]` for triage — guards meta-tickets from spurious shipping tasks). |
| `reconcile_reviewed()` | **any non-terminal state** (all states except SHIPPED/MERGED/DELIVERED/IGNORED) → reviewed | **Phase-driven / state-complete** gate catch-up (#694, #798, #799, #808). No reviewing-task condition — the shipping gate verifies the required phases across the union of the ticket's sessions (`Ticket.aggregate_phase_records()`, the single source of truth) before calling this, so `ship()` is legal and `pr create` never raises a raw `TransitionNotAllowed`. No side effects. Invoked via `_ship_fsm.reconcile_fsm_for_ship()` (extracted from `pr.py` by concern, #748) from **both** the gate-passed path **and** the `--skip-validation` path (#748): `--skip-validation` is the user-authorized attestation substitute (the gate-fixer bootstrap exception, /t3:ship §5 #2), so the FSM must follow the authorization — otherwise `ship()` is structurally impossible from a non-REVIEWED state. **#808 made the source state-complete:** it was previously an enumerated allow-list (#798 added pre-REVIEWED states; #799 added `in_review`; `retrospected` and any future unlisted non-terminal state was still rejected), which kept re-introducing the `{'allowed': False, 'missing': []}` denial — the gate aggregated `missing: []` while the FSM couldn't reach `REVIEWED` from the lingering state (e.g. a ticket re-provisioned for a new workstream whose FSM sat at `retrospected`). The source is now **derived** from the terminal set (`Ticket._RECONCILE_SOURCE_STATES` = all states minus `_TERMINAL_STATES` = SHIPPED/MERGED/DELIVERED/IGNORED), with a test asserting the partition is exhaustive, so a newly added non-terminal state can never silently re-break the gate. Terminal states stay non-recoverable: SHIPPED/MERGED/DELIVERED are genuine post-ship success, IGNORED is abandoned. `_ship_fsm.reconcile_fsm_for_ship()` still no-ops at REVIEWED + the terminal set (`_SHIP_RECONCILE_NOOP_STATES`); a defence-in-depth `suppress(TransitionNotAllowed)` keeps the #694 "never a raw raise" invariant. |
| `ship()` | reviewed → shipped | Clean-worktree preflight. Enqueues `execute_ship` worker. Worker runs `ShipExecutor` and calls `request_review()` on success. |
| `request_review()` | shipped → in_review | — |
| `mark_merged()` | in_review → merged | Enqueues `execute_teardown` worker. Worker runs `WorktreeTeardown` (best-effort cleanup of git worktrees, branches, per-worktree DBs, overlay hooks). Errors do NOT block the FSM — `retrospect()` can advance the ticket regardless. |
| `retrospect()` | merged → retrospected | Enqueues `execute_retrospect` worker. Worker runs `RetroExecutor` and calls `mark_delivered()` on success. |
| `mark_delivered()` | retrospected → delivered | — |
| `rework()` | coded/tested/reviewed → started | Clears tests_passed, cancels pending tasks |

**Worker enqueue pattern (BLUEPRINT §4 invariant):** transitions that own long I/O follow one rule — body stays pure (state change + metadata only), then `transaction.on_commit(lambda: execute_X.enqueue(self.pk))`. The state change and the queued work land atomically. Workers take a row lock (`select_for_update()`), re-check the source state, run the runner, and on success call the next transition. At-least-once delivery is safe because the state guard makes redelivery a no-op. See `teatree/core/runners/` for the runner classes and `teatree/core/tasks.py` for the workers.

**Clean-worktree preflight (#884).** `code()`, `test()`, `review()`, and `ship()` call `Ticket._refuse_if_worktree_dirty(phase)` at the top of the transition body, before any scheduling side effect. If any of the ticket's worktrees has uncommitted **tracked** changes, the transition is refused: a loud `DirtyWorktreeError` (an `InvalidTransitionError` subclass — a `ValueError`, *not* django-fsm `TransitionNotAllowed`) is raised naming the dirty worktree. Every production caller wraps the transition body in an *outer* `transaction.atomic()` (the loop: `Task.complete()` → `_advance_ticket` → `_apply_phase_transition`; ship: `_ship_exec._do_ship_transition`), so the raise rolls that whole atomic back — the FSM state change is undone, the ticket stays put, and the task reverts to its pre-`complete()` CLAIMED state (no force-reopen: a cross-transaction durable write cannot survive the caller's rollback, so attempting one would be a false durability claim). **Held-task recovery is the lease-reaper safety net**, not a first-committed reopen: the worker that called the transition stops heartbeating after the exception, the task's lease expires, and `TaskManager.reclaim_orphaned_claims` returns the CLAIMED task to PENDING on the next loop tick so the agent re-runs it and finishes the commit. The ship path surfaces the refusal as the structured `ShippingGateFailure` contract (`_do_ship_transition` catches `InvalidTransitionError` alongside `TransitionNotAllowed`), never as an exception escaping `pr create`. **No auto-stash:** teatree worktrees share one `.git`, so a stash is repo-global and could clobber an unrelated branch's work; refuse-and-let-the-reaper-recover is the owner-resolved default. Untracked-only files do **not** block (the tracked-vs-untracked distinction, mirroring `cli.update._tracked_dirty_paths`); an unresolvable or non-git worktree path fails open (treated as not-dirty) so a legitimately-clean ticket never stalls. The check reuses the existing `git.status_porcelain` helper (`_worktree_tracked_dirty_path`, path resolution mirroring `_worktree_has_commits_ahead`).

**Synchronous ship atomicity (`pr create --sync`, #838, #860).** The inline path (`_ship_sync`) runs the `ship()` FSM transition **and** the inline `execute_ship` inside a *single* `transaction.atomic()` block. A `ShipExecutor.run()` exception — a `git push` precondition failure surfaces as a `CommandFailedError` — then rolls the `ship()` advance back, so the ship is all-or-nothing: either pushed + PR opened + FSM advanced, or the FSM is left untouched (safely re-runnable from `REVIEWED`). The exception is caught and returned as a structured `ShipExecuted` (`ok=False`, real cause in `detail`); pre-#838 it propagated unhandled, committing a partial `SHIPPED` (no push/PR) and crashing the `manage.py` subprocess so the CLI wrapper surfaced only an opaque `rc=1` with the real cause lost. `ShipExecutor.run()` also has *non-raising* precondition exits (`no code host configured`, `no worktree on ticket`, `branch … already merged into base`) that return `RunnerResult(ok=False)`; `execute_ship` then returns a normal `{"ok": False}` dict — no exception. #838 only treated an exception as the rollback trigger, so pre-#860 those structured failures still committed the same partial `SHIPPED`. #860 closes that residual path: a failing `execute_ship` result is re-raised inside the atomic block as `_ShipExecutionError` carrying the real `detail`, so both the raised and the `ok=False` paths share one rollback + structured-surfacing path. This is the synchronous analogue of the worker enqueue pattern's "state change and queued work land atomically" invariant — the async path achieves it via `on_commit`, the sync path via the shared transaction. The block's `on_commit` enqueue fires only on commit (success); `execute_ship`'s own state guard makes a later worker pickup a no-op.

**Auto-scheduling:** each phase transition leads to the next-phase task in a fresh session (bias-free evaluation). `start()` schedules coding indirectly — the provision worker calls `schedule_coding()` once worktrees exist. The remaining auto-schedule edges are direct:

- `start()` → enqueues provision → on success → headless coding task
- `code()` → headless testing task
- `test()` → headless reviewing task
- `review()` → shipping task (execution target gated by `T3_AUTO_SHIP`), gated on `has_shippable_diff()`

`schedule_shipping()` defaults to `ExecutionTarget.INTERACTIVE` so the user must explicitly approve the push. Set `T3_AUTO_SHIP=true` in the environment to make shipping headless.

`Ticket.has_shippable_diff()` returns True iff at least one `Worktree` has commits ahead of its base branch (resolved via `origin/<default>` or local `main` fallback). When False, `review()` advances state but skips `schedule_shipping()` — typical for meta-tracker tickets whose work shipped via sibling PRs. Manual `schedule_shipping()` callers (CLI, tests) remain permissive and bypass the gate.

**`extra` structure** (authoritative schema: `TicketExtra` TypedDict in `core/models/types.py`, validated by `validated_ticket_extra()`):

```python
{
    "tests_passed": bool,
    "pr_urls": ["..."],
    "prs": {
        "<pr_id>": {
            "url": str, "title": str, "branch": str, "draft": bool,
            "repo": str, "iid": int,
            "pipeline_status": str, "pipeline_url": str,
            "review_requested": bool, "reviewer_names": [str],
            "head_sha": str, "last_reviewed_sha": str,
        }
    },
    "pr_title_override": str,
    "branch": str,
    "description": str,
    "provision": {"...": "..."},
    "ignored_from": str,
    "shipping_skipped": str,
    "visual_qa": {"targets": [...], "pages_checked": int, "errors": int, ...},
    "issue_title": str,
    "labels": [str],
    "tracker_status": str,
    "auto_started": bool,
}
```

**Property:** `ticket_number` extracts numeric ID from `issue_url` tail via regex, falls back to `pk`.

### 4.2 Worktree — Per-repo lifecycle (FK → Ticket)

One worktree per repository per ticket.

**States:** `created` → `provisioned` → `services_up` → `ready`

**Fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `ticket` | FK(Ticket) | Parent ticket |
| `overlay` | CharField(255) | Overlay name (entry point name from `teatree.overlays`) |
| `repo_path` | CharField(500) | Repo identifier (e.g. `org/repo` or short slug) — NOT a filesystem path. The on-disk worktree path lives in `extra['worktree_path']` and is exposed as `Worktree.worktree_path`. |
| `branch` | CharField(255) | Git branch name |
| `state` | FSMField | Current lifecycle state |
| `db_name` | CharField(255) | Database name |
| `extra` | JSONField(dict) | Extensible metadata |

**Transitions:**

| Method | Source → Target | Side effects |
|--------|----------------|--------------|
| `provision()` | created → provisioned | Builds db_name |
| `start_services(services=[])` | provisioned → services_up | Stores service list in extra |
| `verify()` | services_up → ready | Builds URL map in extra |
| `db_refresh()` | provisioned/services_up/ready → provisioned | Stores timestamp |
| `teardown()` | * → created | Clears db_name, extra |

**Readiness gate:** ``worktree start``, ``worktree verify``, ``worktree ready``, and ``workspace start`` run ``overlay.get_readiness_probes(worktree)`` after their primary work and exit 1 if any probe fails. Probes are runtime checks against started services (HTTP endpoints, dependency round-trips, content invariants on seeded data); ``HealthCheck`` covers post-provision file/symlink/env invariants instead. See ``teatree.core.readiness``.

**Port allocation (Non-Negotiable — see §16):** Ports are NEVER stored in the database or the `.t3-env.cache` file. The compose override declares container ports with **no host binding** (`ports: ["<container_port>"]`), so Docker auto-maps to a free host port at compose-up time. After `compose up`, `WorktreeStartRunner` queries the running project via `docker compose port <service> <container_port>` and stores the result on `Worktree.extra["ports"]` for downstream callers (URL printing, `worktree status`, E2E discovery). The running containers are the single source of truth.

Canonical container ports (from `teatree.utils.ports.CONTAINER_PORTS`; consumed by `COMPOSE_SERVICE_MAP` to query host ports back):

- `backend`: 8000 (Django runserver in `web` container)
- `frontend`: 80 (nginx-served Angular dist)
- `postgres`: 5432 (shared host postgres; per-worktree isolation via DB names, not ports)
- `redis`: not per-worktree. Overlays opting in via `uses_redis()` share `teatree-redis` on `localhost:6379`; per-ticket isolation comes from `Ticket.redis_db_index` → `REDIS_DB_INDEX` env var; slot count from `teatree.redis_db_count` in `~/.teatree.toml`, default 16.

**Database naming:** `wt_{ticket_number}_{variant}` (variant suffix omitted if empty).

### 4.3 Session — Quality gate tracker (FK → Ticket)

Tracks which workflow phases an agent visited within a conversation, to enforce ordering. The phase records across **all of a ticket's sessions** are the **single source of truth** for the shipping gate (#694): `ticket.state` is reconciled *from* their union (`Ticket.aggregate_phase_records()`), never the reverse. FSM-advancing `visit-phase` forks a fresh session by design, so the required phases are legitimately scattered — the gate consumes the cross-session union, not the latest session alone. Both the loop path (`Task.complete()` records the visited phase via `_record_phase_visit()`) and the CLI path (`lifecycle visit-phase`) write canonical phase tokens here, so the gate and the FSM cannot disagree. **`Ticket.ensure_session()` (#748)** guarantees a loop/coordinator-built ticket — one created via `get_or_create` in the dispatch path, not through `workspace ticket` — still has a durable session, so the gate reconciles real attested work instead of fail-closing on "no session"; it is idempotent and reuses the *earliest* existing session so attestation is never split across a fresh empty one. It is called from the orchestrator dispatch path and the `workspace ticket` command so those entry points converge; the `tasks create` path already materialises a session via its own pre-existing lazy-session logic (`tasks.py` — `Session.objects.filter(...).first() or Session.objects.create(...)`), so it converges independently without needing `ensure_session()`. The read+create is wrapped in `transaction.atomic()` with the ticket row `select_for_update`-locked, so two concurrent loop ticks for the same `issue_url` (the dispatch path has no surrounding transaction) cannot both miss and double-create — the loser blocks, re-reads under the lock, and reuses. **Reaper fix (#748):** `workspace ticket`'s failed-provision rollback (`ticket.delete()`) is guarded — because `Session.ticket` is `on_delete=CASCADE`, deleting a `get_or_create`-shared ticket that a concurrent `lifecycle visit-phase` populated with phase-attestation sessions would cascade-reap that genuine work; the rollback now only discards a ticket whose `aggregate_phase_records()` is empty.

**Phase vocabulary (`teatree.core.phases`).** Skills emit short verbs (`scope`, `code`, `test`, `review`, `ship`, `retro`); older code and `_REQUIRED_PHASES` use gerunds. `normalize_phase()` collapses every spelling to one canonical token (the form stored in `visited_phases`/`_REQUIRED_PHASES`); `phase_transition()` maps a phase to its `Ticket` FSM transition. `lifecycle visit-phase` and `pr create` both resolve the ticket via the shared `Ticket.objects.resolve()` (pk / issue number / issue URL), so callers can pass the forge issue number without hitting a silent `DoesNotExist`. **Cross-DB guard (`teatree.core.db_anchor`, #779).** Both commands call `assert_lifecycle_db_is_canonical(ticket)` before any phase write / gate read. The trip condition is the *live Django connection* being bound to a real per-worktree isolated `db.sqlite3` under `paths.worktree_isolation_root()` — exactly the `uv run manage.py`-from-a-worktree case whose phase write (maker `testing`/`retro`) or gate read (reviewer `reviewing`, `pr create`) never reaches the canonical DB the shipping gate consults. It then raises `WrongWorktreeDBError` naming the isolated DB in use, the canonical DB, the ticket's worktree, and the correct `t3 <ov>` command, instead of silently splitting attestation from the DB the gate reads (the symmetric defect behind #764/#628/#769/#777/#778). `t3 <ov>` proxies through the main clone (canonical DB) and never trips; `:memory:` test DBs are never under the isolation root so the guard is inert under the test runner without a test-only branch — same doctrine as `paths.CanonicalDBFromWorktreeError`.

**Fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `ticket` | FK(Ticket) | Parent ticket |
| `overlay` | CharField(255) | Overlay name (entry point name from `teatree.overlays`) |
| `visited_phases` | JSONField(list) | Phases visited in order |
| `started_at` | DateTimeField | Auto-set |
| `ended_at` | DateTimeField | Set on manual handoff |
| `agent_id` | CharField(255) | Agent identifier |

**Quality gates (hardcoded):**

```python
_REQUIRED_PHASES = {
    "reviewing": ["testing"],
    "shipping": ["testing", "reviewing"],
    "requesting_review": ["shipping"],
}
```

**#837 — retro is orchestrator-only.** The shipping gate no longer requires a per-ticket `retro` visit. Retro is an orchestrator-level periodic synthesis over durable signal (task metadata / snapshots), not a per-ticket sub-agent step; gating shipping on it pushed retro to the least-effective level (a box-tick by the sub-agent that just did the work). `retro` remains a recordable phase (`teatree.core.phases`) for audit — it is simply no longer in the `shipping` required set.

`check_gate(phase, force=False)` raises `QualityGateError` if required phases haven't been visited *on this session*; `force=True` bypasses. `check_gate_across_ticket(phase)` runs the same missing-phase logic over the **union** of the ticket's sessions (`Ticket.aggregate_phase_records()`) — this is what the shipping gate uses. The gate verifies only that the required phases (`testing`, `reviewing`) were recorded for the work. Independence in code review is a property of the **execution context** — the `reviewing` phase is earned by a freshly-spawned `t3:reviewer` sub-agent that has not seen the implementation conversation, and that spawn boundary *is* the independence guarantee, by construction (same-session spawn is fine). There is no `agent_id` comparison: a stored-identity maker≠checker inference added no real independence over the structural spawn boundary and was net-negative (false-denied legitimate same-session work), so it was removed (#833). `phase_visits` is retained as an audit trail of who recorded each phase; it is not consumed for gate enforcement. **`Session.recording_identity(explicit="")`** resolves a guaranteed-non-empty attribution (`explicit` → `Session.agent_id` → `session-<pk>` fallback); both the CLI path (`lifecycle visit-phase --agent-id`) and the loop path (`Task._record_phase_visit`) route through it so neither can stamp a blank again. **`Session.visit_phase` is atomic (#755):** its read-modify-write of the `visited_phases`/`phase_visits` JSON columns runs in `transaction.atomic()` with the row `select_for_update`-locked and re-read from the locked row, so a live maker session and an independent reviewer writing the same `Session` pk concurrently cannot lose-update (clobber) each other's phase (the #748 / `/t3:review` Safety-6 unlocked-RMW class; tracked as #761, fixed here).

**SQLite write serialization makes `select_for_update()` real on prod (#804).** Django's SQLite backend silently *ignores* `select_for_update()` — it is a documented no-op (SQLite has no row-level locks). So the locked-RMW pattern that `Session.visit_phase`, `Task.claim` (§4.4) and ~12 sibling sites rely on for mutual exclusion would, by itself, serialize *nothing* on the production engine (prod and the test DB are both SQLite). The compensating primitive lives at the connection level: `settings.DATABASES["default"]["OPTIONS"]` (the named `SQLITE_WRITE_SERIALIZATION_OPTIONS` constant) sets `transaction_mode="IMMEDIATE"` (Django 5.1+), so every `transaction.atomic()` block opens with `BEGIN IMMEDIATE` and the first writer takes SQLite's reserved write lock at transaction *start* — concurrent writers block on `busy_timeout` (`timeout=30s`) and retry instead of racing, restoring exactly the invariant the `select_for_update()` calls assume. `journal_mode=WAL` lets readers run concurrently with the single writer. The contention is exercised by a real two-writer, file-backed-SQLite regression (`tests/test_sqlite_write_serialization.py`) — the ordinary `:memory:` test DB is per-connection and single-threaded, so it structurally cannot catch this; that test double-claims/`database is locked`s without the OPTIONS and yields exactly one winner with them.

### 4.4 Task — Agent work unit (FK → Ticket, Session)

Represents a unit of work for an agent (headless or interactive).

**States:** `pending` → `claimed` → `completed` / `failed`

**Fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `ticket` | FK(Ticket) | Parent ticket |
| `session` | FK(Session) | Parent session |
| `parent_task` | FK(self, null) | For interactive followups |
| `phase` | CharField(64) | Workflow phase (reviewing, shipping, etc.) |
| `execution_target` | CharField(32) | "headless" or "interactive" |
| `execution_reason` | TextField | Why this task exists |
| `status` | FSMField | pending/claimed/completed/failed |
| `claimed_at` | DateTimeField | When claimed |
| `claimed_by` | CharField(255) | Who claimed it |
| `lease_expires_at` | DateTimeField | Lease expiry for timeout recovery |
| `heartbeat_at` | DateTimeField | Last heartbeat |
| `result_artifact_path` | CharField(500) | Path to result artifact |

**Claiming:** `claim(claimed_by, lease_seconds=300)` uses `select_for_update()` for atomic distributed locking. Raises `InvalidTransitionError` if already claimed with a valid lease.

**Completion flow:** `complete()` → clears claim → calls `_advance_ticket()`, **all inside a single `transaction.atomic()` (#883)**. Pre-#883 the task `save()` and the FSM transition were two separate write boundaries: a process death between them left the task COMPLETED but the ticket on its old state, and because the task was no longer CLAIMED neither `reclaim_orphaned_claims` nor `reap_stale_claims` could rescue it — the loop stalled forever on a half-advanced ticket. One transaction closes that window: either both writes land or neither does. `_advance_ticket()` records the phase visit then delegates the FSM dispatch to `_apply_phase_transition()` — the **single** place that maps a completed phase to an FSM transition, **shared** by the live `complete()` chain and the `TaskQuerySet.replay_orphaned_transitions()` boot/tick recovery sweep (the boot-time safety net for any row that slipped through before the atomic fix shipped, or via a future un-wrapped seam — sibling of `reclaim_orphaned_claims`, run from the same `_reap_stale_task_claims()` hook *before* the claim sweeps). Because replay reuses that exact guarded path it is idempotent and **cannot skip a lifecycle gate**: a COMPLETED `shipping` task on a ticket that never went through code→test→review finds no matching `phase + state` guard and no-ops, so a ticket can never reach a state it did not earn. `_apply_phase_transition()` **normalizes `self.phase` via `normalize_phase()` once** before the FSM dispatch (#750), mirroring `_record_phase_visit()` — a task whose phase is a short verb (`review`/`code`/…, the vocabulary skills emit and `tasks create` stores verbatim) advances the FSM, not just records the session visit; raw comparison previously left `ticket.state` silently desynced from `visited_phases` (one root cause `reconcile_reviewed()` papered over). **Manual recovery CLI (#1031):** `t3 <overlay> tasks complete <id> [--note "<reason>"]` is the operator-driven terminal-*success* counterpart to `tasks cancel` (which routes to `fail()`). It drives a CLAIMED task through this same `complete()` chain — so the ticket auto-advances and the loop stops re-emitting the task — for work whose obligation was satisfied out-of-band (e.g. a reviewing task whose PR landed elsewhere). It is idempotent (already-COMPLETED → no-op exit 0), rejects any non-CLAIMED state with a clear error, and records the optional `--note` as a `TaskAttempt` (`exit_code=0`, `result={"complete_note": …}`) for the audit trail. The phase-keyed branches below match on the **normalized** token:

- If last attempt has `needs_user_input: true`: creates interactive followup task (same phase, parent_task linked, session carries the `agent_session_id` for resume)
- If phase is "scoping" and ticket is SCOPED: calls `ticket.start()` (→ schedules coding)
- If phase is "coding" and ticket is STARTED: calls `ticket.code()` (→ schedules testing)
- If phase is "testing" and ticket is CODED: calls `ticket.test(passed=True)` (→ schedules reviewing)
- If phase is "reviewing" and ticket is TESTED: calls `ticket.review()` (→ schedules shipping)
- If phase is "shipping" and ticket is REVIEWED: calls `ticket.ship()`

Each guard is `phase + state` so repeat calls (parallel child tasks, **or `replay_orphaned_transitions()` re-running an already-applied transition**) find the state mismatch and safely no-op after the first advance.

**Phase task consumption:** Each FSM transition body calls `_consume_pending_phase_tasks(phase)` for the phase it closes. On the task-driven path the task was already marked COMPLETED before the transition fires, so the call is a zero-row no-op. On direct-call paths (e.g. `pr.py` invoking `ticket.ship()` from a CLI command) the previously auto-scheduled phase task is still PENDING/CLAIMED — the call marks it COMPLETED so the dispatcher does not later claim it as a zombie session. Both this consume side (`TaskQuerySet.pending_in_phase`, #769) and the FSM read-side conditions (`TaskQuerySet.completed_in_phase`, #757) match the phase via the shared `phase_spellings()` SSOT, so a short-verb task (`tasks create <id> review`, stored unnormalized as `review`) is matched the same as the canonical `reviewing` — a raw `phase=phase` filter previously missed it.

**Session resume:** Both headless and interactive runners walk the `parent_task` chain to find a previous `agent_session_id`. When found, the CLI is invoked with `--resume <session_id>` to preserve full conversation context across execution mode switches.

**Convenience:** `complete_with_attempt()` creates a TaskAttempt and calls complete/fail based on exit_code.

**Routing:** `route_to_headless(reason=)` and `route_to_interactive(reason=)` change execution_target and reset to PENDING.

### 4.5 TaskAttempt — Execution history (FK → Task)

Records each execution attempt for audit trail.

**Fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `task` | FK(Task) | Parent task |
| `started_at` | DateTimeField | Auto-set |
| `ended_at` | DateTimeField | When execution finished |
| `execution_target` | CharField(32) | headless/interactive |
| `error` | TextField | Error message if failed |
| `exit_code` | IntegerField | 0=success, non-zero=failure |
| `artifact_path` | CharField(500) | Path to output artifact |
| `result` | JSONField(dict) | Structured result (see §5) |
| `launch_url` | URLField(500) | Reserved for interactive task launch URLs |
| `agent_session_id` | CharField(255) | Agent session ID for continuity |

---
