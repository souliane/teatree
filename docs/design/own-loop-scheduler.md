# Self-owned singleton loop-runner — replace the native `/loop` cron driver with a Django-native background worker

> **Status: Adopted (#2876) — driver scope; transport + cached-resume owned by
> [#2565](https://github.com/souliane/teatree/issues/2565).** This ADR's *driver*
> half is implemented in #2876: the self-owned singleton `t3 loop-runner` daemon
> that owns the tick cadence, shipped default-OFF behind `loop_runner_enabled`
> (§8 cutover). §9 records the resolved decisions. The runtime / model-binding
> half — the provider-agnostic harness and the OpenAI-compatible router — and
> cached-resume fidelity are **not** decided here: they are owned by the companion
> epic #2565 and its ADR
> [`autonomous-lane-redesign.md`](autonomous-lane-redesign.md), which this doc
> consumes in lockstep and does not re-decide.
>
> **Scope:** the loop *driver* only — what owns the tick cadence and how a tick
> is dispatched. The DB `Loop` config, the leases, the enabled+due verdict, the
> silent tick, the scanners, and the statusline are unchanged, and the existing
> Claude-SDK dispatch path (`headless.py`) is untouched — the beat decides only
> WHEN a tick fires, never WHAT it does. Interactive Claude Code stays unchanged.

Related: BLUEPRINT.md §5.6 (Loop Topology) and [loop-topology.md](../blueprint/loop-topology.md);
§17.4 (orchestrator-decides / loop-executes, [factory-architecture.md](../blueprint/factory-architecture.md));
issues #2650, #1796, #1913, #786, #58, #1192.

---

## 1. Premise — what changes and what does not

Today the loop is **session-bound and tick-driven**. A long-lived Claude Code
session registers one native Claude `/loop` cron per enabled `Loop` row (#2650);
each cron fires `t3 loops tick --loop <name>` on that row's DB cadence
(`src/teatree/cli/loops.py:49-82`). With zero sessions open the loop is dormant —
documented as **"no OS daemon — accepted, not a defect"**
(`src/teatree/cli/loop.py:60`; BLUEPRINT.md §5.6 Invariant 1).

Two things follow from that baseline that this design removes:

1. **A hard dependency on a Claude session as the clock.** The cadence is driven
   by Claude's `/loop` scheduler, so no session ⇒ no ticks.
2. **A hard dependency on the Claude Agent SDK as the dispatch transport.** The
   headless path sends prompts through the SDK (`await client.query(...)`,
   `src/teatree/agents/headless.py:550`).

This design replaces **only the driver**: a self-owned singleton `t3` worker owns
the cadence (independent of any Claude session), drives it Django-natively
(no OS cron / launchd / systemd), and dispatches prompt-backed work through a
provider-agnostic harness. Everything a tick *does* — the `Loop` rows, per-loop
`loop:<name>` leases, the unified enabled+due verdict, silent-tick-on-no-signal,
the scanners — is carried over unchanged. It is a like-for-like re-implementation
of the clock and the transport, not a behaviour change.

There is already a foundation for this: `t3 loops run --interval`
(`src/teatree/cli/loops.py:84-110`) is a continuous "runner, not a loop itself"
— a `while True: call_command("loops_tick"); sleep(interval)`. The worker is that
runner, hardened into a singleton daemon and moved onto a Django-tasks cadence.

---

## 2. Singleton enforcement

The worker MUST be a singleton — never two runners racing on scanner state, the
statusline file, or per-row dispatch dedup. Reuse the codebase's canonical
"only one" primitive: the flock guard at `src/teatree/utils/singleton.py:86-115`.

```python
from teatree.utils.singleton import singleton

with singleton("loop-runner"):
    run_forever()          # owns the cadence until the process dies
```

- **Mechanism:** a non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on a pidfile
  under `DATA_DIR` (`singleton.py:102`). Kernel-enforced, cross-platform
  (Linux + macOS), and crash-safe: the kernel releases the lock when the holder
  dies, so there is **no stale-pid window to reclaim** (`singleton.py:13-19`).
- **On contention:** a second `t3 loop-runner` raises `AlreadyRunningError(name,
  pid, path)` immediately and exits non-zero (`singleton.py:103-106`). The pid in
  the file is diagnostic only (surfaced by `t3 doctor` / `read_pid`,
  `singleton.py:56-75`) — the `flock` is the lock, not the pid.
- **On a stale pidfile / dead holder:** nothing to do. Because the lock is an
  open-fd lease, a dead holder's lock is already gone; the next acquirer wins on
  its first attempt. `read_pid` unlinks a malformed/dead pidfile as a diagnostic
  courtesy but never touches an actively-held one (`singleton.py:65-74`).

This is the exact guard the module docstring already names for `t3 loop tick`
and `t3 <overlay> worker` (`singleton.py:1-20`), so the runner is not a new
mechanism — it is one more caller of the established one. The `t3 mcp serve`
command (`src/teatree/cli/mcp.py:21-28`) is a stdio server spawned per Claude
session and does not itself hold this lock; the reusable single-instance
primitive the ticket points at is the flock guard above, and that is what the
runner adopts.

**Why flock and not the DB `LoopLease`.** The DB lease
(`src/teatree/core/loop_lease_manager.py:119-256`) is PID-anchored and TTL-based;
it is the right tool for *per-loop* mutual exclusion inside a tick (§4) and stays
exactly as-is. But the *process*-level "only one runner on this box" question is
answered more cheaply and with zero stale-state by the kernel flock. The two are
layered, not redundant: flock guards the OS process; `LoopLease` guards each
`loop:<name>` tick.

---

## 3. Django-tasks scheduling seam

The cadence is driven **inside the singleton worker**, cross-platform, with no OS
scheduler. The project already depends on Django's native Tasks framework —
`django-tasks>=0.9` + `django-tasks-db>=0.12` (`pyproject.toml:38-39`), backend
configured at `src/teatree/settings.py:149-153`
(`TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}`), with
six `@task` functions already in `src/teatree/core/tasks.py` (including
`execute_headless_task(task_id, phase)`).

The worker is a thin, always-on beat loop that leans on that framework:

```
loop-runner (singleton process)
  └─ every BEAT_SECONDS (a coarse floor, e.g. 30–60s — NOT a per-loop cadence):
       for row in Loop.objects.enabled():
           if verdict_admits(row, now):        # §4 — unchanged
               execute_loop.enqueue(row.name)   # @task, DB-backed queue
       django_tasks worker drains the queue → runs each tick
```

Two moving parts, both Django-native and OS-agnostic:

| Part | Role | Backed by |
|---|---|---|
| **Beat** | Wakes on a coarse interval, asks the DB which rows are enabled+due, enqueues one task per admitted row. | `time.monotonic()` sleep loop inside the singleton (same shape as `t3 loops run`, `loops.py:106-110`). |
| **Task queue** | Runs each admitted loop's tick out-of-band, at-least-once, with idempotency. | `django_tasks_db.DatabaseBackend` — a table in the same SQLite file, zero ops. |

The beat interval is only "how often the runner re-checks" — **per-loop cadence
stays in the `Loop` rows** (`delay_seconds` / `daily_at`, `loop.py:84-192`) and
is evaluated by the unchanged verdict (§4). The beat never itself decides a
loop is due; it only asks. This keeps the DB the single cadence ledger
(loop-topology.md: "`Loop.last_run_at` is now the SINGLE cadence ledger").

**Silent tick stays ~zero cost.** When no row is admitted the beat enqueues
nothing and sleeps again; when an admitted tick finds no dispatchable work,
`run_tick` already returns after render without a model call (the `if jobs:`
gate at `src/teatree/loop/tick.py:158-171`, silent render path at
`src/teatree/loop/phases/render.py:56-66`). The runner adds no model call of its
own — the beat is pure Python DB reads, cheap enough to run continuously. Where
the beat needs a Django-free read (e.g. a fast liveness probe from a hook), the
cold reader `src/teatree/config/cold_reader.py:109-135` is the sanctioned
stdlib-only path.

---

## 4. Per-loop lease / cadence / unified-verdict carryover

None of the tick-admission logic changes. The worker calls exactly the code the
native `/loop` cron calls today; only *who invokes it* moves from Claude's
scheduler to the beat.

| Semantic | Today | Under the runner | Location (unchanged) |
|---|---|---|---|
| Per-loop mutex | `loop:<name>` `LoopLease` claim | same claim, from the task | `loop_lease_manager.py:119-256`, `per_loop_owner_slot` |
| Cadence | `Loop.delay_seconds` / `daily_at`, anchored on `last_run_at` | same | `core/models/loop.py:84-192`, `is_due` at `:150-157` |
| Enabled+due verdict | `row.enabled AND row.is_due(now) AND config.is_enabled(row)` | same three gates | `src/teatree/loops/master.py:116-140` |
| Enable SSOT | `loop_enabled(name)` = `Loop.enabled AND not LoopState-held` | same | `src/teatree/loop/loop_state_db.py:41-65` |
| Double-drive guard | `mark_run_if_unchanged` CAS on `last_run_at` | same | `loops/master.py`, `LoopManager.mark_run_if_unchanged` |
| `enabled` / `held` / pause | `Loop.enabled` + `LoopState` tier (#1913) | same | `loop_state_db.py` |

The worker's `execute_loop.enqueue(row.name)` resolves to the same
`t3 loops tick --loop <name>` code path (`loops_tick` management command),
which claims `loop:<name>` and runs the unified `run_tick` pipeline. Because
`mark_run_if_unchanged` is a compare-and-swap on `last_run_at`, an at-least-once
double delivery from `django-tasks` is a no-op on the second run — the
idempotency invariant (#1192) is already satisfied by the existing cadence CAS,
not by the queue.

---

## 5. Dispatch seam — subprocess vs provider-agnostic harness

A loop is either script/CLI-backed or prompt-backed; the two are mutually
exclusive by DB constraint `loop_prompt_xor_script`
(`src/teatree/core/models/loop.py:113`). The branch already exists at
`src/teatree/loops/master.py:59-74` (`_resolve_dispatch_loop`): a `script` row
resolves to a Python `MiniLoop`, a `prompt` row resolves to a stored `Prompt`.
The runner keeps that branch and defines one seam over the two kinds:

```
DispatchSeam.run(loop_row, tick_context)
  ├─ script/CLI loop   → subprocess (plain process, NO model)
  │                       src/teatree/loops/<name>/loop.py via MiniLoop.build_jobs()
  └─ prompt loop       → agent harness (provider-agnostic) with Prompt.render(**params)
                          replaces the SDK transport at headless.py:550
```

- **Script loops** run as a plain subprocess — deterministic Python I/O, no model
  call, no tokens. This is unchanged from today (the scanner stage is already
  pure Python; loop-topology.md "Why pure-Python scanners (not subagents)").
- **Prompt loops** (e.g. `arch_review`) render the DB `Prompt.body`
  (`src/teatree/core/models/prompt.py:46-122`, `render(**args)` at `:77-100`)
  and invoke an agent. This is where the transport is swapped: today the headless
  runner builds a prompt and calls `await client.query(prompt)` on a
  `ClaudeSDKClient` (`src/teatree/agents/headless.py:493-566`, imports at
  `:26-34`). Most of the dispatch machinery around it — task claim/CAS, lease
  heartbeat (`task.renew_lease`, `core/models/task.py:203-207`), the watchdog
  timeout, the `AgentResult` collection — is retained; only `_collect`'s inner
  transport call changes.

The seam interface is the existing `run_headless(task, phase,
overlay_skill_metadata)` boundary (`src/teatree/core/management/commands/tasks.py:480-484`).
Behind it, a `Harness` protocol takes `(rendered_prompt, options)` and returns
the same streamed `AgentResult` the SDK path returns today, so callers upstream
of the seam are unaffected.

---

## 6. Provider-agnostic harness + configurable OpenAI-compatible router

The prompt-loop transport is a **provider-agnostic agent harness (e.g. Pydantic
AI)** with the model bound through a **configurable OpenAI-compatible router
(BYOK)**. This is deliberately **not** the Claude Agent SDK and **not** the
Anthropic API called directly — an Anthropic model is at most one selectable
backend behind the router, never a hard dependency. The full decision (runtime
choice, two-layer model binding, fencing) lives in
[`autonomous-lane-redesign.md`](autonomous-lane-redesign.md); this doc records
only how the loop-runner consumes it.

- **Model is config-chosen and swappable.** Today the model id is resolved by
  `resolve_spawn_model(phase, skills, session_id, task_id)`
  (`src/teatree/agents/model_tiering.py:180-234`) from a phase→tier→model table
  (`:58-62`, `:100-109`). That resolver stays as the *policy* layer; its output
  is no longer a hard `claude-*` id passed to `ClaudeAgentOptions` but a router
  handle. The router is one more OpenAI-compatible endpoint — the concrete model
  (Anthropic, or any other) is chosen by config and can be swapped without code
  change.
- **BYOK.** The router takes the operator's own key; no subscription-OAuth
  coupling. Key + base-URL are config settings (read via the same
  overlay-then-global config chain the rest of teatree uses).
- **What stays Anthropic-shaped.** The `AgentResult` envelope
  (`src/teatree/agents/result_schema.py`) and the phase model are transport-
  independent, so the harness returns the same structured result regardless of
  backend.

---

## 7. Headless park → Slack → cached-resume

In headless mode there is no TTY and no `AskUserQuestion` — Slack (the DM
backend) is the only user channel. When a prompt loop needs input, approval, or
hits a blocker it must park durably, ask over Slack, and resume from where it
stopped without re-paying full context. Most of this loop already exists; the
transport swap forces one new piece (cached resume) because it removes the Claude
SDK session-resume that today makes continuation cheap.

**State machine** (existing wiring in `[brackets]`):

```
running
  │  agent returns AgentResult{needs_user_input: true, user_input_reason}
  │  [result_schema.py:35-55; check_evidence skips evidence at :159-160]
  ▼
PARKED ── record durable state ─────────────────────────────────────────────┐
  │  [Task.park_for_user_input → record_deferred_question,                    │
  │   task_handoff.py:20-33,52-69]                                            │
  │  DeferredQuestion{parked_task=task, run_id=agent_session_id, slack_ts=""} │
  │  [deferred_question.py:42-309; parked_task FK]                            │
  ▼                                                                           │
ASKED ── post to Slack/DM (on-behalf, #58) ─────────────────────────────────┤
  │  [deferred_question_poster scanner → drain_unmirrored_deferred_questions  │
  │   → notify_user, notify_question_drains.py:99-135; notify.py:54-144]      │
  │  stamps slack_ts/slack_channel back (verify-by-re-read)                   │
  ▼                                                                           │
(user replies in Slack)                                                       │
  │  [PendingChatInjection ← slack_dm_inbound scanner]                        │
  ▼                                                                           │
ANSWERED ── bind reply to the live question ────────────────────────────────┤
  │  [askuserquestion_reply scanner: live_for_reply → apply_answer,           │
  │   scanners/askuserquestion_reply.py:62-93]                                │
  ▼                                                                           │
RESUMING ── re-queue a HEADLESS continuation ───────────────────────────────┘
     [schedule_headless_resume(task, answer=…), task_handoff.py:72-103:
      child Task, parent_task=task, answer prepended to execution_reason]
     → back into §5 dispatch as a normal headless task
```

Also-covered blocker case: a repair-loop stall escalates the same way
(`_escalate_stall` → `DeferredQuestion.record`, `core/models/task_repair.py:58-73`).
The interactive lane is unaffected — `park_for_user_input` branches on
`agent_runtime` and schedules an in-session followup there (`task_handoff.py:30-31`).

**The one new piece — cached resume (#2886, done).** Today `schedule_headless_resume`
chains `parent_task` and `_get_resume_session_id` walks back to the captured
`TaskAttempt.agent_session_id` (`core/models/task_attempt.py:52-77`), so the
Claude SDK *resumes that session* (`--resume`) and the agent continues from the
decision point without re-sending context. **The provider-agnostic transport has
no Claude session to resume**, so this cheap continuation is re-homed in the
harness (`teatree.agents.pydantic_ai_resume`):

1. **Persist the parked thread.** On park (`needs_user_input`), the pydantic_ai
   harness's `list[ModelMessage]` is dumped JSON-safe
   (`ModelMessagesTypeAdapter.dump_python(..., mode="json")`) and stored durably
   under `Ticket.extra["pydantic_ai_threads"]`, keyed by the parked `Task`'s own
   pk — reusing the already-migrated `Ticket.extra` store (no migration), via the
   locked `Ticket.merge_extra` read-modify-write. Single-use: a resume pops its
   entry, so the map never accumulates stale threads.
2. **Rehydrate on resume.** `resolve_harness(task)` walks `task.parent_task`
   exactly like `_get_resume_session_id` (same ancestor, either backend) and
   seeds the new `PydanticAiHarnessSession` with the rehydrated history before
   `schedule_headless_resume`'s answer is appended.
3. **Prompt-cache the stable prefix.** Resending the rehydrated history *is* the
   whole mechanism — no manual `cache_control` markers are sent (prompt-cache
   semantics differ per provider behind OrcaRouter's OpenAI-compatible surface,
   and are opaque to teatree). A cache hit shows up as non-zero
   `cache_read_tokens` on the resuming `TaskAttempt`; a miss simply re-pays the
   context as ordinary input tokens. Fallback policy: **re-pay and log the cost,
   never refuse** — a missing, malformed, or already-consumed thread degrades to
   an empty history, never an exception.

Resilience invariants (#1192, `skills/architecture-design/SKILL.md:83-93`) are all
satisfied by existing pieces: **fallback-transport** = the durable
`DeferredQuestion` row IS the fallback when Slack is down (it retries un-mirrored
next tick); **verify-by-re-read** = the poster reads the delivered `BotPing`
coordinates back before stamping `slack_ts`; **idempotency** =
`schedule_headless_resume` returns the existing PENDING/CLAIMED child rather than
duplicating (`task_handoff.py:83-88`); **heartbeat** = `task.renew_lease`;
**sub-agent return contract** = the `AgentResult` envelope.

---

## 8. Migration / cutover — supersede "session-bound, no daemon" without a flag day

The current stance is explicit and documented as intentional: `t3 loop --help`
("no OS daemon — accepted, not a defect", `src/teatree/cli/loop.py:60`) and
BLUEPRINT §5.6 Invariant 1 ("0 sessions ⇒ nothing runs"). This design supersedes
that stance; the doc-alignment rule (root `CLAUDE.md`) means the BLUEPRINT and
help text change in the same PR that lands the behaviour. No flag day:

1. **Land the runner behind a default-OFF switch.** `t3 loop-runner` ships but
   the native `/loop` cron path stays the default driver. A DB-home setting
   (`loop_runner_enabled`, fail-OFF, mirroring the `teams_enabled` pattern in
   loop-topology.md) selects the driver. Both drivers call the identical
   `loops_tick` path, so they are behaviourally interchangeable and cannot run
   the same loop twice (the `loop:<name>` lease + `mark_run_if_unchanged` CAS
   already serialize across *any* caller).
2. **Swap the transport behind its own switch.** The harness/router (§6) lands
   as a selectable transport with the Claude SDK as one backend, so the transport
   move and the driver move are independent PRs, each with a pinning test.
3. **Dogfood.** Enable `loop_runner_enabled` on the in-repo dogfooding overlay
   only; keep native `/loop` for everyone else until the runner has soaked.
4. **Flip the default, then retire.** Once soaked, flip the default to the runner
   and update BLUEPRINT §5.6 + `loop --help` to describe the daemon model. The
   native `/loop` registration (`SessionStart` `CronCreate`) is removed last, in
   its own PR, once no path depends on it.

Because the two drivers are mutually safe (same lease, same CAS), a box can even
run the tail of the migration with both present without double-dispatch — the
cutover is a setting flip, not a stop-the-world.

---

## 9. Resolved decisions (#2876)

The six open questions in the draft are resolved below and implemented in #2876
(driver scope). Cached-resume and the transport (items 3–4) stay owned by #2565;
the rest ship here.

1. **Beat interval — `max(5, min(30, min_enabled_delay_seconds / 2))`.** The
   coarse beat is half the shortest ENABLED interval cadence, capped at a 30s
   ceiling and floored at 5s (no busy spin). Daily-only (`daily_at`) loops do
   **not** lower the beat, and with no enabled interval loop the beat sits at the
   30s ceiling. Implemented in `teatree.loops.runner.compute_beat_seconds`.
2. **Supervision — "at least one" with NO OS scheduler.** No
   cron/launchd/systemd. Three layers compose: the flock singleton gives
   *at-most-one*; the supervised daemon (`LoopRunnerDaemon.run`) respawns a
   crashed beat worker after a short backoff; and the `SessionStart` resurrector
   (`hooks/scripts/loop_runner_supervisor.py` → `resurrect_loop_runner`)
   re-launches the whole daemon whenever the flock is free. A fully-headless box
   starts `t3 loop-runner` once from a login profile — a dotfile, not a system
   scheduler. The beat enqueues onto a dedicated `loop-runner` django-tasks
   queue so a per-loop tick never blocks behind a heavy default-queue
   FSM/headless job.
3. **Cached-resume fidelity — owned by #2565.** Prompt-cache semantics differ by
   provider behind the OpenAI-compatible router, so the fallback policy — *re-pay
   context and log the cost, never refuse* — lands in #2565 alongside the
   transport that owns the cache marker. This driver ADR does not decide it.
4. **Model-binding ownership — lockstep with #2565.** §5–6 defer the
   runtime/router decision to `autonomous-lane-redesign.md` (#2565); they follow
   it rather than duplicating it. The existing Claude-SDK dispatch path stays the
   only transport this PR touches.
5. **`t3 loops run` fate — already removed by #2880.** The `--interval`
   foreground runner was excised by #2880 (the master-tick removal), so there is
   nothing left to fold. The foreground / test variant is now
   `t3 loop-runner --once` (a single beat + drain — no supervisor, no sleep, no
   respawn), and the supervised daemon is the singleton-wrapped default.
6. **Interactive vs headless coexistence — one owner (decision 6).** When
   `loop_runner_enabled` is ON, `SessionStart` emits ZERO `CronCreate`:
   `teatree.loops.claude_specs.enabled_loop_specs()` returns empty, so the daemon
   is the sole cadence owner and the two drivers never both fire. Default-OFF
   keeps the native `/loop` crons mirroring the enabled rows exactly as today.
   (Scoped boundary: the three always-on reactive `/loop <duration>` infra slots
   are *not* `CronCreate` and are out of scope here — they keep their own
   sub-minute registration; folding them onto the daemon is a later step.)
