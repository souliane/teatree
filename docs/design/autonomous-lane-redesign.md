# Autonomous-lane redesign — keep the SQLite orchestration, move the runtime to Pydantic AI

> Status: **Draft / decision proposed — not yet adopted.** This is a design and
> decision document to be reviewed before any behaviour migration. It changes no
> runtime code. The one migration seam below is a later, separately-reviewed PR.

## Why this document exists

Teatree's autonomous lane (the `/loop`, the FSM, task leases and heartbeats,
scheduling, crash-resume, retries) is hand-rolled, and it does get fragile. An
earlier draft of this document concluded that the fix was to delete that
machinery and stand it on a heavier durable-execution engine. Working through the
evidence, I no longer think that is the right call, and this document records the
corrected position so the change of mind is legible rather than silent.

The short version: the orchestration layer stays. The one thing that moves is the
autonomous-lane **agent runtime**, which goes from a bespoke, Claude-coupled
runtime to **Pydantic AI**. Everything else — Django, SQLite, the loop, the FSM,
the leases, the heartbeats, the cron, the crash-resume, the retries — is kept.

## The root cause is implementation discipline, not the durable-state layer

The earlier draft assumed the fragility lived in the durable-workflow-state
layer, so swapping that layer would fix it. I went back and looked at the last
three systemic bugs I actually fixed, and the assumption does not hold:

| Bug | Was it a durable-state bug? |
|---|---|
| SQLite claim race — `select_for_update` was a no-op on the claim path, so two ticks could claim the same task | **Yes** — a genuine durable-state / concurrency bug. |
| False-positive commit gates — a quality gate reporting pass when it had not actually run | No. A logic bug in the gate, nothing to do with durability. |
| Unbounded timeout in the dream distiller — a step with no time bound could hang the pass | No. A missing timeout in one step, nothing to do with durability. |

Only **one of the three** was a durable-state bug. The other two were ordinary
implementation defects that a different durable engine would not have touched. So
the premise that swapping the durable engine fixes the fragility is wrong on its
own evidence — it would, at best, address one bug in three.

What the three have in common is not the durable layer. It is **discipline**:
changes that get half-built and abandoned mid-way, leaving a gate that does not
gate, a lock that does not lock, a step with no bound. That is the actual fragility
source, and no engine swap fixes it. Worse, a multi-step migration of the durable
engine is *itself* the half-finished-implementation failure mode it claims to
cure — a big rip-out is the single most likely thing to get abandoned half-done.

The honest conclusion: the maintenance pain is real, but it is paid down by
finishing changes and tightening gates and timeouts, not by replacing a layer
that is mostly working.

## Zero-ops SQLite is a virtue worth keeping

For a single maintainer, the current store has one property that is easy to
undervalue: **zero ops.** SQLite (WAL, `transaction_mode=IMMEDIATE`) with the
`django_tasks_db` DB-backed queue is a file, not a service. Nothing to stand up,
nothing to operate, nothing to back up beyond the file. Trading that away for a
database server — to fix a fragility that is mostly not in the store — is a bad
deal for a solo system at this load.

## What stays (the orchestration layer, kept whole)

The orchestration layer is kept as-is. None of these rows change in this redesign:

| Concern | Where it lives today (in `src/teatree/`) | Status |
|---|---|---|
| Loop / tick driver | `teatree.loop.tick.run_tick` + the scanner fan-out, one native `/loop` per enabled `Loop` row | **Kept** |
| Scheduling / cron | `Loop` rows (`delay_seconds` / `daily_at`), `Loop.last_run_at` as the cadence ledger | **Kept** |
| Singleton / concurrency control | `LoopLease` (conditional-UPDATE CAS), the `loop-owner` lease, per-loop `loop:<name>` leases, `LoopState` | **Kept** |
| State machines | `Ticket.State`, `Worktree.State` (`created → provisioned → services_up → ready`), the `ReviewLoop` FSM | **Kept** |
| Task leases + heartbeats | `Task.claim(lease_seconds=…)`, `lease_expires_at`, `heartbeat_at`, `Task.renew_lease()` | **Kept** |
| Crash-resume / recovery | `reap_stale_claims`, `reclaim_orphaned_claims`, `teatree.loop.tick_recovery`, `teatree.core.recovery_sweeps` | **Kept** |
| Queue | `django_tasks_db.DatabaseBackend` (django-tasks) | **Kept** |
| Retries | re-dispatch on a failed `TaskAttempt` | **Kept** |
| Store | **SQLite** (WAL, `IMMEDIATE`) | **Kept** |

The bespoke distributed-systems code here is real, and the SQLite claim race
proved it can have subtle bugs. But the answer to a subtle bug in a working layer
is to fix the bug and add a pinning test (which the claim-race fix did), not to
replace the layer.

## The one architectural change — autonomous-lane runtime → Pydantic AI

The single change this document proposes is to move the **autonomous-lane agent
runtime** off the bespoke, Claude-SDK-coupled runtime
(`teatree.agents.headless`, the `LoopWatchdog`, the in-house attempt/result
plumbing) and onto **Pydantic AI**, running *inside* the unchanged orchestration.

What Pydantic AI buys, stated plainly:

- **Model-agnostic.** Today the headless runtime is coupled to Claude. With a
  provider layer behind the harness, the model becomes a config choice rather than
  an architectural commitment — teatree can run the autonomous lane on the
  cheapest meta-provider instead of being locked to one vendor. The model is a
  setting, not the architecture.
- **Deterministic, programmatic control of the agent loop.** The harness gives
  explicit, testable control over the loop instead of a hand-rolled driver.
- **Dogfooding.** It lets the personal system run on the same kind of harness I
  reach for elsewhere, so the runtime is exercised in daily use.

Two things are explicitly preserved across this seam:

- **The Actor-Critic / iterative-verify pattern stays** wherever multi-step
  verification happens: a generator step produces a change, deterministic checks
  (tests, linters, a sandbox) and an **independent critic model** look for
  problems, and the generator corrects. This is **iterative verification with
  deterministic tripwires** — not a generative multi-model "fusion" that merges
  several model outputs. The pattern does not change; only the runtime under it
  does.
- **Claude Code stays the interactive cockpit, unchanged.** Only the autonomous
  (headless) runtime moves. The interactive lane is not touched.

Type-safe outputs continue to be **Pydantic v2 models** at the runtime boundary,
which is the natural fit once the runtime is Pydantic AI. If a given autonomous
run wants explicit per-run control flow, **pydantic-graph** is available as an
*optional* per-run structure — it is not a replacement for the orchestration FSM,
which stays where it is.

## Eval — keep the existing harness; pydantic-evals is a future maybe, not a decision

The earlier draft proposed swapping the eval harness to pydantic-evals. That is
**dropped as a committed decision.** Teatree's existing eval harness
(`teatree.eval`, including the `$0` offline transcript regrade) is kept.

pydantic-evals is recorded here only as a **possible future option, explicitly not
decided now.** It is the weakest and most deferrable of the changes considered: it
would trade a working thing for migration risk, with no problem in front of it
that the current harness fails to handle. If a concrete eval need ever appears
that the current harness genuinely cannot express, this can be reopened — but not
before, and not as part of this redesign.

## Corrections to an earlier position

An earlier draft of this document proposed adopting **DBOS** as a durable-execution
library and **deleting the bespoke durable-state layer**, moving the store to
**Postgres**. That position is **superseded.** Recording why, so the reversal is
legible:

1. **DBOS forces Postgres, which abandons zero-ops SQLite.** DBOS requires a
   Postgres database. For a solo maintainer, giving up the file-based, no-service
   SQLite store is a real operational cost paid to fix a problem that is mostly
   not in the store.
2. **The durable-state layer is not the root fragility — discipline is.** Of the
   last three systemic bugs, only one was a durable-state bug; the other two were
   ordinary half-finished-implementation defects (a gate that did not gate, a step
   with no timeout). Swapping the durable engine would not have touched two of the
   three.
3. **A multi-step rip-out is the exact failure mode to avoid.** The fragility is
   changes abandoned mid-way. A four-step engine migration is the single largest
   such change one could take on — it is the disease, not the cure.

DBOS / Postgres was considered and rejected for these reasons; this document does
not advocate for it. (Temporal and other heavier durable-execution engines were
considered in the same earlier pass and are out for the same reason: they add an
operated service to a system whose virtue is having none.)

Stating this as a correction rather than a fresh conclusion is deliberate: the
earlier position was reasonable on the framing then in front of me; the evidence
(the actual bug history, and what the engine swap would and would not fix) is what
moved it.

## Migration — one incremental seam behind the unchanged orchestration

Because the orchestration layer is kept, the migration is far smaller than the
earlier four-step plan. It is **one seam**: the autonomous-lane runtime moves to
Pydantic AI, *behind the unchanged loop / FSM / lease / cron layer.*

- The runtime is replaced incrementally, as **separately-reviewed PRs**, each with
  a **pinning test** that fixes current behaviour before the swap, so the change is
  observable and reversible.
- The orchestration code is not touched by these PRs — the runtime change sits
  inside the existing leases and recovery, which keeps the blast radius small.
- **This document changes no runtime code.** It stays a draft for independent
  review; nothing here is implemented.

Compared with the abandoned plan, this trades a big-bang engine migration for a
single, contained runtime seam — which is the whole point of the corrected
position: smaller changes, finished one at a time, are how this system gets more
reliable, not larger ones.

## What stays (summary)

- **Django + SQLite** — the orchestration store and domain layer, zero-ops, kept.
- **The loop / FSM / leases / heartbeats / cron / crash-resume / retries** — kept.
- **The Actor-Critic / iterative-verify pattern** — kept, now running on the new
  runtime.
- **The existing eval harness** — kept; pydantic-evals deferred as a maybe.
- **Claude Code** — kept as the interactive cockpit, unchanged.

## Open questions & assumptions

- **Assumption:** the interactive Claude Code lane and the autonomous lane can be
  cleanly separated at the runtime seam, so moving the autonomous runtime to
  Pydantic AI does not disturb the cockpit. The first migration PR depends on this
  holding and should prove it with a pinning test before anything is deleted.
- **Assumption:** the Actor-Critic verify pattern ports onto the Pydantic AI
  runtime without losing the deterministic tripwires it relies on. Worth a small
  spike inside the first seam PR.
- **Open question:** which meta-provider becomes the default for the autonomous
  lane once the model is a config choice. This is a settings decision, not an
  architectural one, and can be made after the runtime seam lands.

## Decision status

Proposed, pending independent review. Nothing here is implemented; no runtime code
changes with this document. If accepted, the migration proceeds as the single
runtime seam above, delivered as separately-reviewed PRs, each with its own
pinning test.
