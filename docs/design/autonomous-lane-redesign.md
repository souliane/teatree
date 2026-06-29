# Autonomous-lane redesign — stand on maintained engines, delete the bespoke durable-state layer

> Status: **Draft / decision proposed — not yet adopted.** This is a design and
> decision document to be reviewed before any behaviour migration. It changes no
> runtime code. Each migration step below is its own later, separately-reviewed PR.

## Why this document exists

Teatree's autonomous lane (the `/loop`, the FSM, task leases and heartbeats,
scheduling, crash-resume, retries) is hand-rolled. It works, and a lot of care
has gone into it, but it is also where most of the fragility and most of the
maintenance time lives — for a single maintainer. This document records a
proposal to **delete that bespoke machinery and stand on serious, maintained
durable-execution and agent-runtime engines instead.**

The goal is explicitly *less code to own*, not *a nicer wrapper around the same
machinery*. The failure mode this guards against is re-implementing the fragile
parts behind new in-house ports and calling it a redesign.

## Premise — the fragility is the durable-workflow-state layer

The brittle, time-consuming part of the autonomous lane is the **hand-rolled
durable-workflow-state layer**: the loop, the state machines, the task leases +
heartbeats, the scheduling, the crash-resume, and the retries. Concretely, in
the current code this is:

| Concern | Where it lives today (verified in `src/teatree/`) |
|---|---|
| Loop / tick driver | `teatree.loop.tick.run_tick` + the scanner fan-out, one native `/loop` per enabled `Loop` row |
| Scheduling / cron | `Loop` rows (`delay_seconds` / `daily_at`), `Loop.last_run_at` as the single cadence ledger, `CronCreate` registration |
| Singleton / concurrency control | `LoopLease` (conditional-UPDATE CAS), the `loop-owner` lease, per-loop `loop:<name>` leases, the `LoopState` enable/pause/disable control plane |
| State machines | `Ticket.State`, `Worktree.State` (`created → provisioned → services_up → ready`), the `ReviewLoop` FSM |
| Task leases + heartbeats | `Task.claim(lease_seconds=…)`, `lease_expires_at`, `heartbeat_at`, `Task.renew_lease()` |
| Crash-resume / recovery | `reap_stale_claims`, `reclaim_orphaned_claims`, `teatree.loop.tick_recovery`, `teatree.core.recovery_sweeps`, post-compaction snapshot recovery |
| Queue | `django_tasks_db.DatabaseBackend` (django-tasks) |
| Retries | re-dispatch on a failed `TaskAttempt` |

This is a meaningful amount of bespoke distributed-systems code — leases, CAS
mutexes, heartbeat reapers, crash recovery, idempotency — for one person to keep
correct. It is exactly the category of problem that mature durable-execution
engines exist to solve, and solve better than a hand-rolled version can.

### Honest correction to the stated premise — the substrate is SQLite today

An earlier framing of this proposal described DBOS as "a library over the
Postgres teatree already runs." That is not accurate and should not be repeated:
teatree's orchestration store is **SQLite** today, not Postgres
(`src/teatree/settings.py`: `django.db.backends.sqlite3`, WAL +
`transaction_mode=IMMEDIATE`), with a `django_tasks_db` DB-backed queue.

DBOS requires Postgres. So adopting it carries a real, named cost: **introduce a
Postgres database** for the durable-execution substrate. That cost should be on
the table honestly. What it is *not* is a new operated *service* — it is one
database plus an in-process library. The distinction matters for the central
decision below (DBOS vs Temporal), and it is the honest version of the argument,
not a softened one.

## Goal

Delete the bespoke loop / FSM / lease / heartbeat / cron / resume / retry layer
and replace it with maintained engines, reducing the maintenance surface a solo
maintainer carries. Keep the domain value (the skills and overlay logic) and the
interactive cockpit (Claude Code) untouched.

## Decision

| Concern | Choice | What it deletes |
|---|---|---|
| Durable orchestration — scheduler + state machine + leases + heartbeats + resume + retries | **DBOS** — durable execution as a *library* over a Postgres database | The bespoke loop / FSM / lease / heartbeat / cron / resume layer. DBOS provides native cron scheduled workflows, Postgres-backed queues with per-worker + global concurrency limits and rate limiting, and automatic crash-recovery of interrupted workflows. |
| Agent runtime | **Pydantic AI** — model-agnostic; ships an official DBOS durable-execution integration | The Claude-Code/Claude-SDK-coupled bespoke runtime (`teatree.agents.headless`, the `LoopWatchdog`, the in-house attempt/result plumbing). The interactive lane stays on Claude Code as the mature cockpit. |
| Headless human-in-the-loop — park-question / stop / resume | A **DBOS durable wait** — suspend the workflow, await an external signal, resume | The bespoke park-and-resume machinery (the headless `needs_user_input` / resume-state / `DeferredQuestion` plumbing for autonomous runs). |
| Eval | **pydantic-evals** — fits Pydantic AI, consumes OTel traces, deterministic scorers are free, `LLMJudge` built-in — with a thin transcript→dataset adapter | The bespoke eval harness (`teatree.eval`). Alternative noted: **inspect-ai**, if native `inspect score` stored-log regrade is wanted. |
| Per-run agent control flow | **pydantic-graph** (optional) | Hand-rolled per-run branching, where it exists. |

The shape of the bet: every row replaces in-house distributed-systems or
agent-plumbing code with a maintained engine that already does that job, and the
"what it deletes" column is the actual point — the win is measured in bespoke
code removed, not features added.

## The key non-obvious call — DBOS over Temporal

This is the one choice that needs defending, because the obvious answer is
"Temporal."

**Temporal is the most battle-tested durable-execution engine, and it is the
right tool for a large team running high-throughput workflows.** It is not being
dismissed on quality.

The problem is operational burden, which is the *exact thing this redesign is
trying to reduce*. Self-hosting Temporal means standing up and operating:

- the Temporal Server (its own set of long-running services),
- a persistence database,
- (historically) Elasticsearch for advanced visibility, and
- the discipline that long-lived workflows demand — deterministic workflow code
  and explicit versioning/patching so an in-flight workflow survives a code
  change.

For a solo maintainer, that *adds* a system to run and a class of bugs to learn.
Adopting it to reduce maintenance would be self-defeating.

**DBOS runs in-process as a library.** There is no separate orchestration
service to operate — it persists workflow and step state to a Postgres database
and recovers interrupted workflows automatically on restart. The cost it does
add is honestly stated above: one Postgres database (teatree is on SQLite
today). That is a database, not an operated service — a materially smaller
operational footprint than Temporal's server + persistence + visibility stack.

The usual argument *for* Temporal over DBOS is scale: DBOS is a library over a
single Postgres, so its throughput ceiling is that one database. **At this
system's load — one maintainer's autonomous lane — that ceiling is irrelevant.**
Optimising the choice for a scale this system will not reach, at the cost of the
operational simplicity it needs now, is the wrong trade.

**Escalation path if DBOS proves insufficient.** If DBOS hits a real stability
or scale wall, the escape is **Temporal Cloud** — the managed, zero-ops Temporal
— not self-hosted Temporal. That keeps the "no new operated service" property
intact even in the failure case.

### Honest risk

DBOS is the **youngest option with the smallest ecosystem** of the durable-
execution engines considered. That is a real risk and not worth glossing over.

It is mitigated by the shape of the adoption: DBOS is a **thin library**, so
lock-in is low and the cost of leaving is cheap — the workflows are ordinary
functions with decorators, not a framework that owns the whole program. Combined
with the Temporal-Cloud fallback, the downside of betting on the younger tool is
bounded: if it does not work out, the exit is a contained rewrite of the
workflow seams, not a re-architecture.

## Corrections to an earlier position

Two earlier positions are explicitly superseded by this proposal. They are
recorded here so the change of mind is legible rather than silent.

1. **"Keep the existing state machine."** Superseded. Given the fragility
   evidence and what durable-execution engines now provide as a library, the
   state-machine / lease / resume layer *is* the fragility, and it is exactly
   what DBOS deletes. Keeping it would keep the maintenance problem this redesign
   exists to remove.

2. **"Keep the custom eval harness."** Superseded. The harness's only remaining
   moat was **$0 offline transcript regrade** (`t3 eval run --backend transcript`
   re-grades a recorded run with no model spend). That capability is now matched
   by maintained tools — pydantic-evals re-scores from stored traces, and
   inspect-ai's `inspect score` re-grades a stored log without re-running the
   model. Once the only differentiator is matched, maintaining a bespoke harness
   is cost without benefit.

Stating these as corrections, not as fresh conclusions, is deliberate: the
earlier positions were reasonable on the evidence then available; the new
evidence (the fragility itself, and the maturity of the durable-execution and
eval tooling) is what moved them.

## Migration — strangler-fig, one reviewed PR at a time

The migration is incremental. Each step removes a slice of bespoke code, ships
as its own reviewed PR with a pinning test, and **keeps the Claude Code
interactive path working throughout**. No step is a big-bang rewrite.

1. **Durable state + scheduling onto DBOS first.** This is the highest-fragility
   slice and the clearest win — the loop, the leases, the heartbeats, the cron,
   the crash-resume. Doing it first retires the largest amount of bespoke
   distributed-systems code and de-risks the rest. (This is also where the
   Postgres dependency lands.)
2. **Move the agent runtime to Pydantic AI**, using its DBOS integration so the
   runtime work runs inside DBOS-durable workflows.
3. **Fold headless HITL into a DBOS durable wait** — replace the bespoke park /
   resume plumbing with suspend-await-signal-resume.
4. **Move eval to pydantic-evals** (with the thin transcript→dataset adapter),
   keeping inspect-ai as the documented alternative.

Ordering rationale: durable state first because it carries the most fragility
and unblocks the others; eval last because it is the most independent and the
least risky to defer.

## Revisit triggers

This decision should be reopened, not quietly worked around, if either of these
fires:

- **DBOS hits a real stability or scale wall** → escalate to **Temporal Cloud**
  (managed, zero-ops), preserving the "no new operated service" property.
- **pydantic-evals cannot express a needed eval** → adopt **inspect-ai** (native
  `inspect score` stored-log regrade) for the eval lane.

## What stays

The redesign is deliberately narrow. These are kept as-is because they are
mature or because they are the actual value:

- **Django** — the mature data / domain layer.
- **The skills + overlay domain logic** — the actual value of the system; none
  of this is touched.
- **Claude Code** — kept as the interactive cockpit. The interactive lane is not
  migrated; only the autonomous (headless) runtime moves to Pydantic AI.

## Open questions & assumptions

- **Assumption:** adopting DBOS means migrating teatree's orchestration store
  from SQLite to Postgres. The migration plan treats that as part of step 1; the
  cost/effort of the data move itself is not estimated here and should be sized
  before step 1 is approved.
- **Open question:** does DBOS's durable-wait primitive cover every shape of the
  current headless HITL contract (park, stop, resume from the captured point,
  idempotent re-entry), or only the common case? Step 3 should validate this
  against the existing `needs_user_input` / resume-state behaviour before
  deleting the bespoke path.
- **Open question:** the transcript→dataset adapter for pydantic-evals assumes
  the recorded-transcript format can be mapped to pydantic-evals `Case`s without
  losing the deterministic-scorer coverage the current corpus has. This needs a
  spike before step 4.
- **Assumption:** the interactive Claude Code lane and the autonomous lane can be
  cleanly separated at the runtime seam, so migrating the autonomous runtime to
  Pydantic AI does not disturb the cockpit. Step 2 depends on this holding.

## Decision status

Proposed, pending independent review. Nothing here is implemented; no runtime
code changes with this document. If the decision is accepted, the migration
proceeds as the four separately-reviewed PRs above, each with its own pinning
test.
