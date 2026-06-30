# Autonomous-lane architecture — keep the SQLite orchestration, move the runtime to Pydantic AI, bind the model layer, and make one t3 master own integration

> **Status: Draft / decision proposed — not adopted.** This is an architecture
> decision record for teatree's autonomous lane. It changes **no runtime code**:
> every behaviour described as a migration step below is its own later,
> separately-reviewed PR, each carrying a pinning test that fixes current
> behaviour before the change. **Do not merge** this document ahead of independent
> review — the point of writing it down is to get the decision looked at before
> any code moves.
>
> **Scope:** teatree's autonomous lane only. Teatree-internal and self-contained;
> nothing here is an overlay or customer concern. The interactive Claude Code
> cockpit is explicitly out of scope and stays unchanged throughout.

This document supersedes an earlier runtime-only draft. It folds four threads
that were drifting apart — the durable-store question, the runtime move, the
model-binding layer, and cross-session integration — into one coherent picture,
because they share one premise and one set of constraints and reviewing them
separately kept losing that.

---

## 1. Premise — the root cause is implementation discipline, not the durable-state layer

Teatree's autonomous lane (the loops, the FSM, task leases and heartbeats,
scheduling, crash-resume, retries) is hand-rolled, and it does get fragile. An
earlier draft concluded the fix was to delete that machinery and stand it on a
heavier durable-execution engine. Working back through the actual evidence, I no
longer think that holds, and this section records the corrected position so the
change of mind is legible rather than silent.

I went and looked at the last three systemic bugs I actually fixed:

| Bug | Durable-state / concurrency bug? |
|---|---|
| **SQLite claim race** — `select_for_update` was a no-op on the claim path, so two loops could claim the same task | **Yes.** A genuine durable-state / concurrency bug. |
| **Commit gates over-firing** — a gate flagging a violation on turns that did **not** violate (false-positive over-blocking) | No. A logic bug in the gate's predicate. To be precise: the gate *ran*; it over-blocked. It did **not** "report pass without running." |
| **Unbounded timeout in the dream distiller** — a step with no time bound could hang the pass | No. A missing timeout on one step. |

Only **one of the three** touched the durable layer. The other two were ordinary
implementation defects a different durable engine would not have changed. So the
premise that swapping the durable engine fixes the fragility is wrong on its own
evidence — at best it addresses one bug in three, and it fixes neither the
majority of the breakage nor the real cause.

What the three share is not the store. It is **discipline**: changes half-built
and abandoned mid-way, leaving a gate that over-blocks, a lock that does not
lock, a step with no bound. That is the fragility source, and no engine swap
fixes it. Worse, a multi-step engine migration is *itself* the
half-finished-implementation failure mode it claims to cure — a big rip-out is
the single most likely change to get abandoned half-done.

**Zero-ops SQLite is a virtue worth keeping.** For a single maintainer the
current store has one easily-undervalued property: zero ops. SQLite (WAL,
`transaction_mode=IMMEDIATE`) with the `django_tasks_db` DB-backed queue is a
file, not a service — nothing to stand up, operate, or back up beyond the file.
Trading that for a database server, to fix a fragility that mostly is not in the
store, is a bad deal at this load.

> **Superseded position (recorded so the reversal is legible).** An earlier draft
> proposed adopting **DBOS** as a durable-execution library, deleting the bespoke
> durable-state layer, and moving the store to **Postgres**. That is rejected:
> (1) DBOS forces Postgres, abandoning the zero-ops SQLite store; (2) the
> durable layer is not the root fragility — discipline is, per the bug history
> above; (3) a multi-step rip-out is exactly the abandoned-mid-way failure mode
> to avoid. Temporal and other heavier durable-execution engines are out for the
> same reason — they add an operated service to a system whose virtue is having
> none. **No DBOS, no Postgres, no Temporal.** The earlier position was reasonable
> on its framing; the bug history is what moved it.

So the orchestration layer stays. The maintenance pain is real, but it is paid
down by finishing changes and tightening gates and timeouts, not by replacing a
layer that is mostly working.

---

## 2. The one runtime change — autonomous-lane runtime → Pydantic AI

The single runtime change is to move the **autonomous-lane agent runtime** off
the bespoke, Claude-SDK-coupled runtime (`teatree.agents.headless` and its
attempt/result plumbing) and onto **Pydantic AI**, running *inside* the unchanged
orchestration.

This change is **strategy- and cost-driven, not bug-driven** — and that is a
valid basis for a personal system. Stating it plainly so it is not dressed up as
a reliability fix:

- **Strategic.** Going more headless and reducing single-vendor (Anthropic)
  coupling. The autonomous lane should be able to run on a model chosen by config
  rather than wired into one vendor's SDK.
- **Cost — a bet, not a proven saving.** About **92% of measured autonomous-lane
  spend is cache-reads**, dominated by Claude Code's per-request overhead — the
  giant system prompt and the ~48 skills re-read on every call. The tempting read
  is "a lean headless runner sheds most of that." But be honest: a 92% cache-read
  share is *also* exactly what you would see if caching is working **well** (cache
  reads bill at roughly 0.1× input), so it is not by itself proof the spend is
  sheddable. Two things make the saving conditional, not guaranteed:
  - **The lane is largely subscription-absorbed today.** It runs as Claude Code
    sessions / sub-agents on the Max seat, so much of its usage is already covered
    by a flat seat. Moving it to a **metered** lean runner converts seat-covered
    usage into **real cash spend** — a saving only if the lean runner's far-smaller
    per-call context beats what the seat absorbs today.
  - **The lean runner is not context-free.** It still injects the skill bundle per
    subagent (`skill_injection.py`, a listed port in the table below), so the
    per-call reduction is real but bounded, not a clean slate.
  - **Open assumption to confirm first.** Whether the autonomous lane's *today*
    cost basis is subscription-absorbed or already metered must be **confirmed**
    before banking the saving. If it is subscription-absorbed today, the saving is
    conditional on the token reduction outweighing the lost seat absorption.
  Stated plainly: the cost case is a **bet** (lean-runner token reduction vs loss
  of subscription absorption), and the strategic reason above is what carries the
  move regardless of how that bet lands.

**Be honest about the coupling surface — this is not a one-file seam.** The
orchestration consumes an SDK-shaped contract, and the migration has to re-home
all of it. The real port surface:

| Today (`src/teatree/agents/`) | What it does | Why the port must carry it |
|---|---|---|
| `model_tiering.py` | Slot / honesty routing — picks the logical model slot per step | The orchestration decides the slot; the runtime must keep honouring it (see §3) |
| `result_schema.py` | The `needs_user_input` envelope | The blocked-subagent escalation path depends on this exact shape |
| `skill_injection.py` | The subagent skill preamble | Subagents must still receive their skill bundle headless |
| `prompt.py` + `skill_bundle.py` | Subagent prompt construction and the skill bundle assembled into it | The headless prompt the runtime sends is built here; the port must rebuild the same prompt |
| `outage_classifier.py` | Rate-limit / outage classification | Retry and backoff behaviour keys off this |
| `headless.py` + `attempt_recorder.py` + attempt/result plumbing | The attempt loop, attempt recording, results, heartbeats | The lease/heartbeat contract the orchestration relies on |
| `headless_usage.py` | Per-run token/usage accounting | The cost measurement (§2's 92% figure) and metered-lane budgeting read this |
| `handover.py` | Session handover plumbing | Cross-session handoff must keep working across the seam |

Naming these as the port surface is the point: the migration succeeds only when
each is re-homed behind Pydantic AI with a pinning test, not when a single entry
point is swapped. (`skill_injection.py` is also why the cost case in §2 is a bet,
not a certainty — the lean runner still injects a skill bundle per subagent.)

Two things are preserved across the seam:

- **The critic / iterative-verify pattern stays** wherever multi-step
  verification happens: a generator produces a change, deterministic tripwires
  (tests, linters, a sandbox) plus an independent critic model look for problems,
  and the generator corrects. Only the runtime under the pattern moves.
- **Claude Code stays the interactive cockpit, unchanged.** Only the autonomous
  (headless) runtime moves.

Type-safe outputs continue to be **Pydantic v2 models** at the runtime boundary —
the natural fit once the runtime is Pydantic AI. `pydantic-graph` is available as
an *optional* per-run structure where a run wants explicit control flow; it is
**not** a replacement for the orchestration FSM, which stays put.

---

## 3. The model layer — two layers, not collapsed

The model decision is **two layers**, and conflating them is the mistake to
avoid.

**Layer 1 — teatree decides the logical slot (domain-aware).** `model_tiering`
stays. Teatree knows things a generic router cannot: the critic / verification
step needs an honest model; bulk work needs a cheap one. That domain judgement is
teatree's and never leaves.

**Layer 2 — each slot's target is a binding.** A slot's target is settable to
**either**:

- a **concrete model** (e.g. `claude-opus`), or
- a **routing handle** that delegates the *per-request* model pick to a
  third-party black-box router.

The analogy is **opusplan**: the layer above treats one handle as a single model,
while multiple real models sit underneath and the indirection is hidden. "Smart
routing" is **opt-in per slot**, set via the binding. Teatree never stops
deciding the slot — it just becomes *possible* to point a slot's target at the
router instead of at a fixed model.

**Provider for the routing handle — OrcaRouter.** A purpose-built black-box
per-request router: zero-markup, OpenAI-compatible, ships an SDK, so it sits
behind Pydantic AI's OpenAI-compatible model class with a thin seam. It also
**proxies concrete models**, so the same gateway serves both `<gateway>:auto`
(let the router pick) and `<gateway>:<model>` (pin a concrete model through the
same path) — one integration, both binding kinds. **OpenRouter** is the mature
fallback (`openrouter/auto`, provider-exclusion, ZDR no-retention). **No Chinese
models**; the cheap tier is gpt-oss-120b / Gemini Flash / Haiku.

**Critic-path correctness.** A generic difficulty-router optimizes cost/quality,
not *honesty* — and the verification/critic slot needs honesty, not the cheapest
adequate answer. So that slot is **bound to a concrete honest model**. This is
just a binding *value* (Layer 2 pointing at a concrete model for that one slot),
not a separate mechanism — which is exactly why keeping the two layers distinct
matters. (Cross-ref: the `/t3:rules` honesty-escalation rule.)

**Two-lane cost picture.** Interactive Claude Code stays on the Max subscription —
that is the cost arbitrage, a flat seat for interactive work. The factory /
autonomous lane becomes the metered, routed one. The two lanes have different cost
models on purpose — but note this is exactly the move that turns the §2 cost case
into a **bet**: if the autonomous lane is subscription-absorbed today, putting it
on the metered lane trades seat-absorbed usage for cash, and only pays off if the
lean-runner token reduction outweighs that. The split is deliberate; the net
saving is conditional, per §2.

---

## 4. Eval — deferred

Teatree's existing eval harness (`teatree.eval`, including the offline transcript
regrade) is **kept**. `pydantic-evals` is recorded **only** as a possible future
option, **explicitly not a committed decision.** It is the weakest and most
deferrable change considered: it would trade a working thing for migration risk
with no problem in front of it the current harness fails to handle. If a concrete
eval need ever appears that the current harness genuinely cannot express, this
can be reopened — but not before, and not as part of this redesign.

---

## 5. Cross-session integration — the t3 master core

This is the substantive new architecture. Everything above keeps or moves a
runtime; this section adds a coordination invariant.

### The problem

Parallel producers — sub-agents inside any session, parallel interactive
sessions, or headless workers — race to merge. The results are merge conflicts,
duplicated work, and migration collisions. Nothing today serializes integration,
so two producers can both think they own the finish line.

### t3 master — holder of the global owner lease

**t3 master** is the session that holds the **t3 master lease** — the **global
owner lease**, renamed from the former `loop-owner` / `GLOBAL_OWNER_SLOT`. Holding
it *is* being t3 master, and it **reserves, indivisibly**:

- the **core loops** — the merge / integration loops (per the 2026-06-27
  directive the merge loop is a core loop); exactly one t3 master runs these,
- the **integration / merge authority**,
- the **`reserve_to_t3_master`** transition rights.

These three do **not** distribute: exactly one t3 master runs the core loops and
owns integration. (The old `loop-owner` lease and `loop_owner` command are the
*historical* name for this same global slot; this design renames and unifies them
into the t3 master lease — `loop-owner` appears only as the thing being renamed,
never as a live separate concept.)

**Non-core loops stay per-loop-distributable.** The shipped per-loop ownership
layer (#1834 — `PER_LOOP_OWNER_PREFIX = "loop:"`, `is_per_loop_owner_slot`,
`owned_per_loop_slots` across `src/teatree/core/loop_lease_manager.py`,
`src/teatree/loops/live.py`, `src/teatree/loop/loop_scoping.py`,
`src/teatree/loops/claude_specs.py`) lets **different sessions own different
loops** via `loop:<name>` slots. That layer is **preserved**: non-core loops can
be distributed across sessions so different sessions drive different non-core
loops for throughput.

So #1834 is a **superset** of what the t3 master reserves: the t3 master takes
only the **core subset** — the core (merge/integration) loops, the integration
authority, and the reserved transitions. **Everything else — the non-core loops —
remains per-loop-distributable** through the `loop:<name>` per-loop layer. Only
the **global** slot is renamed to the t3 master lease; the per-loop slots keep
their `loop:<name>` form.

**"Core loops"** in this document means the merge / integration loops reserved to
the t3 master. All other loops are non-core and use the per-loop layer.

### Topology — producers parallel, integration serialized

Producers run in parallel and produce branches / PRs. **They never merge.**
Integration — merge, conflict reconciliation, CI-watch-and-dispatch — is owned by
the single t3 master and serialized through it.

```
producer A ─┐
producer B ─┼─▶ ready-for-integration ─▶ [ t3 master ] ─serial─▶ main
producer C ─┘     (hand-off queue)        merge authority
```

### The `reserve_to_t3_master` guard

`reserve_to_t3_master` is the FSM guard field that marks integration-class
transitions (merge, and the like). The `t3_` prefix is deliberate — it must not
read like a git branch named "master".

**Enforcement.** A transition is refused when it is reserved **and** the actor
neither holds the t3 master lease (by session id) **nor** presents the t3 master
lease / fencing token explicitly (see "Authority vs work"). This is enforced at
the single `t3` CLI transition chokepoint — every FSM transition already goes
through `t3`, never raw `gh`/`glab`, so there is exactly one place to check. Note
this FSM-transition check gives serialization of *who may initiate*, not mutual
exclusion at the write; the fencing token (below) closes that at the git write.

**Refuse is not a dead-end.** A refused unit transitions to a
**ready-for-integration** hand-off state that the t3 master drains serially. The
producer is not stuck; it has handed off.

### Authority vs work

`reserve_to_t3_master` means only the t3 master may **initiate** the merge — not
that the master does the labour itself. The master **spawns a merge-worker
sub-agent** to do the work, so the master stays responsive.

**What enforces one-merge-at-a-time.** Seriality is the guarantee, so it needs a
named lock, not just "serialized" as an adjective. The merge-worker takes a
**single-holder merge-worker lease** (a singleton slot under the t3 master lease):
the master drains the ready-for-integration queue **one unit at a time**, blocking
on that lease per unit, and never dispatches a second merge-worker while one holds
it. The serial drain is the lock.

**Authority passed explicitly, not inherited (the session-id fallback).** The
plan-A path is that the master's own sub-agents **share its session id** and so
pass the reserved check; that assumption still needs a pinning test (see Risks).
But the design does **not hinge** on it. The master passes its **t3 master
lease / fencing token explicitly** to the spawned merge-worker, and the reserved
check accepts **that token** — not only the inherited session id. So if the
session-id pin ever fails, the master's own merge-worker is still authorised
through the explicit token rather than being refused → enqueued → looping. The
inherited session id is an optimisation; the explicit token is the authority of
record.

- A non-master session's agents are **refused → enqueue** to the hand-off.
- **The human is never blocked.** To force a merge, claim the t3 master lease
  (see "Lease handoff and split-brain" below for how a mid-drain steal quiesces
  the old master's in-flight work first).

### Lease handoff and split-brain — fencing at the git write

The ground-truth HEAD/merged checks below give **idempotency** (safe re-run of the
*same* merge), **not mutual exclusion** between *different* units during a lease
transition. With a TTL lease, two sessions can briefly both believe they are t3
master — a lease-expiry TOCTOU, or a human "claiming the lease to force a merge"
mid-drain while the old master's merge-worker is still pushing. The danger is the
old master finishing unit X while the new master starts unit Y on `main`.

The session-id check at the **FSM transition** does not close this — it fires once,
at the transition, and a lease can expire (or be stolen) *after* the check passes
but *before* the `git push`. So the fix is a **fencing / lease-generation token**:

- The t3 master lease carries a **monotonically increasing generation number**.
  Every **change of holder** (failover after expiry, or a human steal) increments
  it; routine renewal by the *same* holder keeps the generation, so the master
  never fences its own in-flight worker.
- The merge-worker stamps the generation it was dispatched under, and the
  generation is re-checked **at the git write itself** (the push / merge call),
  not only at the FSM transition. A write whose token is **stale** (a newer
  generation has been granted) is **fenced out** — refused at the push — even
  though the FSM check passed earlier.

**Lease TTL + failover.** The lease has a bounded TTL and is renewed by the live
t3 master; if the master dies, the lease expires after the TTL and another session
may claim it, taking a **higher generation**. Any straggler write from the dead
master's worker carries the old generation and is fenced.

**Human steal mid-drain.** When the human claims the lease mid-drain, the steal
**waits for the in-flight drain to quiesce** — it does not start integrating until
the current merge-worker finishes or is fenced. Concretely: the steal bumps the
generation, and the old master's still-running worker is **fenced out at its next
git write**, so it cannot land unit X under main concurrently with the new
master's unit Y. The human is never blocked from *taking* the lease; the old
worker is just prevented from *writing* under a superseded generation.

### Reconciliation — generic git, tech-stack-agnostic

Reconciliation is **generic git** (serial rebase / resolve) plus the existing
CI-green-before-merge gate. The **detection and integration** are
tech-stack-agnostic by design — this serves *all* overlays, not just teatree's
Django.

**Caveat — the ceiling is git-clean + CI-green, not "correct".** What this
guarantees is a clean merge that passes CI, **not** a semantically correct merge.
Two changes can git-merge clean and stay CI-green while still being semantically
in conflict where no test exercises the interaction. CI is the backstop the
system has, not a proof of correctness — do not read green as "correct".

**No proactive, stack-coupled conflict detectors.** Worked example: two Django
migrations numbered `0028_*` git-merge clean but break the linear-migration
graph. This example only surfaces *because* Django ships a linear-migration check;
the general guarantee is just git-clean + CI-green, with no equivalent backstop
for conflicts no check covers. The design does **not** add a migration-graph
detector. Instead, when the second unit is rebased onto the merged first,
**Django's own check fails in CI**; the t3 master sees red and dispatches a fix.
Note the asymmetry: **detection/integration is stack-agnostic, but the
remediation is not** — the fix the master dispatches for a `0028_*` collision is
itself Django-aware. Any stack-specific reconciliation, if ever genuinely needed,
lives behind an **overlay seam (opt-in)**, never in core.

**Do not predict "relatedness."** No attempt to guess which units will conflict.
Let conflicts surface at the single integrator and resolve them serially. The
serialization is what makes this safe without prediction.

### Crash-mid-merge recovery — idempotency, not durability

The one place to invest care. The integrate step's side effects live in
**git/GitHub, not the DB** — so no durability engine helps here; the DB does not
know whether the `git push` landed. The fix is **re-entrancy**: each integrate
action is preceded by a ground-truth check —

- *is `main` already at this HEAD?*
- *is this PR already merged?*

— so re-running after a crash is a no-op where the action already took effect.
This is why swapping the durable store would not have helped this case, and why
idempotent-against-ground-truth is the right and sufficient design.

### What this enables later — best-of-N winner-merge (future, optional)

Because the **t3 master** is the single integration authority, a hard ticket
could optionally be raced across N parallel producers, the N results judged by
the existing Actor-Critic, and only the winner merged by the t3 master.
`reserve_to_t3_master` already serializes the merge, so there is nearly nothing
to bolt on — the architecture supports this without new machinery. It is
explicitly **optional and N× cost — for hard tickets only, never the default.**
(Inspiration noted from a parallel-coding-agent tool that races agents and merges
the winning diff; teatree's version is the autonomous one.) Recorded as a future
option, not a committed feature.

---

## 6. Anti-duplication & engagement

### The "two sessions doing the same thing" root

Directed work — a human opens a session and starts hacking — often **never
registers a claim**, so the loop cannot see the unit is taken. And because
teatree engagement is **default-OFF (#256)**, a brand-new session may never touch
`t3` at all. So the loop's claim model has a blind spot exactly where humans
start work by hand.

### Fix — anchor the claim to the worktree

`t3 ... worktree provision` is the **sanctioned start**, and **provisioning is the
claim.** The one-worktree-per-ticket dedup already exists; this makes the worktree
the anchor the claim hangs off.

But "provisioning is the claim" only covers work that goes **through**
provisioning. The actual duplication root is the opposite case: a human opens a
session **by hand** in a managed repo and starts hacking on a branch that
**never registers a claim**. That hand-start is, by definition, **not** a t3-owned
unit — so a guard that "fires only when the cwd touches a t3-owned unit" does
nothing there, and the blind spot survives exactly where the duplication keeps
happening.

So the guard must **reach that root**, not just enforce "through t3" on units that
are already owned. The **thin, always-on minimal guard** fires in **two** cases:

- the cwd touches a **t3-owned unit** — a provisioned-worktree marker, or a
  managed repo on a branch **with** a backing claim → enforce the "through t3"
  invariant; and
- the cwd is a **teatree-managed repo on a branch with NO backing claim** — an
  **unclaimed hand-start** → **auto-claim** the branch (or prompt to claim it), so
  the unit becomes visible to the loop's dedup before a second session can pick up
  the same work.

It stays **cheap and scoped to managed repos** — it keys off the managed-repo
marker and a quick claim lookup, so **unrelated directories → it does nothing**
and it never imposes on work outside teatree's managed repos.

Crucially this is **not net-new machinery.** It reuses the existing
**unconditional `PreToolUse` gate pattern** — the MR-metadata, AI-signature, and
orchestrator-boundary safety gates already run on *every* session regardless of
engagement, gated only by their own kill-switch. The anti-duplication guard is
one more gate in that same family.

### What the guard does first

Its first action is the thin **ConfigSetting (DB) read** to decide whether
teatree auto-loads — via `teatree.config.cold_reader`, the canonical DB settings
store. This is consistent with the **2026-06-27 decision** to make ConfigSetting
the single runtime settings source (TOML is export-only). The `autoload` setting
**migrates DB-home** as part of this.

Everything else — the loops, the suggester, full engagement — stays **opt-in
behind the existing `autoload` setting.** The always-on guard is deliberately
minimal: it enforces the "through t3" invariant and reads one setting; it does
not turn the machinery on.

### One `engage()` routine

Auto-load (SessionStart) and manual (skill-load) must call the **same** routine.
Today there are **two parallel marker-writing paths** — the SessionStart
`_autoload_enabled` touch and the `handle_track_skill_usage` skill-load touch —
unified only by a shared read predicate `_teatree_engaged`. That is drift: two
writers, one reader. It is removed by extracting one shared **`engage(session)`**
that both call.

Principle: **auto-loading must do exactly what manual engagement does** — no two
parallel paths that can drift.

---

## 7. Naming — clean global cutover, no aliases

Restated so the cutover is unambiguous:

| Term | Meaning |
|---|---|
| **t3 master** | The session holding the t3 master (global owner) lease. |
| **t3 master lease** | The **global owner lease** whose holder *is* t3 master. Reserves **indivisibly** the *core subset*: the **core loops** (merge/integration loops), the integration/merge authority, and the `reserve_to_t3_master` transition rights. These do not distribute. Renames+unifies the former `loop-owner` / `GLOBAL_OWNER_SLOT`. Only the **global** slot is renamed; the per-loop slots are untouched. |
| **core loops** | The merge / integration loops reserved to the t3 master (the merge loop is a core loop per the 2026-06-27 directive). All other loops are **non-core**. |
| **`loop:<name>` (per-loop ownership, #1834)** | The preserved per-loop lease layer (`PER_LOOP_OWNER_PREFIX = "loop:"`). **Non-core** loops use it and **can be distributed across sessions** — different sessions drive different non-core loops for throughput. #1834 is a superset; the t3 master reserves only the core subset. |
| **`reserve_to_t3_master`** | The FSM guard field on integration-class transitions. `t3_` prefix is deliberate (not a git "master" branch). |
| **fencing / lease-generation token** | The monotonic generation stamped on the t3 master lease and re-checked **at the git write** (push/merge), not only at the FSM transition. A stale-generation write is fenced out, preventing split-brain across a lease handoff. |
| **merge-worker lease** | The single-holder slot under the t3 master lease that enforces one-merge-at-a-time; the master drains the hand-off queue serially behind it. |
| **`/t3:loops`** | How teatree drives its loops — one native Claude `/loop` per enabled `Loop` row, each on its own cadence. |

There is **no tick mechanism.** Teatree never drives itself "through a tick."
`t3 loop tick` exists but is **user-manual only** — a person can run it by hand;
the system does not use it to drive itself.

`loop-owner` survives only as the historical name being renamed. **Clean global
cutover, no deprecated aliases.**

---

## 8. Migration — incremental strangler-fig, each step its own reviewed PR

No big-bang. Each step is a **separately-reviewed PR with a pinning test**, and
the **Claude Code interactive path keeps working throughout.** The steps are
largely independent, which is the point:

1. **Engagement unification** — extract one `engage(session)`; both SessionStart
   auto-load and skill-load manual engagement call it; remove the two parallel
   marker-writing paths. (Orchestration-only; independent of the runtime swap.)
2. **Always-on anti-duplication guard** — add the minimal `PreToolUse` gate that
   enforces "through t3" on t3-owned cwds **and** auto-claims an unclaimed
   hand-start on a managed-repo branch (closing the duplication root), and reads
   `autoload` via `cold_reader`; migrate `autoload` DB-home. (Reuses the existing
   gate pattern; stays scoped to managed repos.)
3. **t3 master rename + unification** — rename the **global** `loop-owner` /
   `GLOBAL_OWNER_SLOT` lease to the t3 master lease (core loops + integration
   authority + reserved transitions reserved to it; the `loop:<name>` per-loop
   layer (#1834) for non-core loops stays distributable); introduce
   `reserve_to_t3_master` and its enforcement at the `t3` transition chokepoint;
   add the ready-for-integration hand-off, the single-holder merge-worker lease,
   and the fencing / lease-generation token checked at the git write; pass the
   token explicitly to the merge-worker and pin the sub-agent-inherits-session-id
   assumption as a secondary path. (Orchestration extension; independent of the
   runtime swap.)
4. **Runtime swap to Pydantic AI** — re-home the port surface from §2
   (`model_tiering`, `result_schema`, `skill_injection`, `outage_classifier`,
   attempt/result plumbing) behind Pydantic AI, inside the unchanged
   orchestration; wire the Layer-2 binding (OrcaRouter handle + concrete-model
   bindings, OpenRouter fallback). Its own seam, behind the unchanged loops/FSM/
   leases/cron.
5. **Eval** — last, and only if reopened. Default is to keep the existing harness.

Steps 1–3 are orchestration extensions doable without touching the runtime; step
4 is the runtime seam behind that unchanged orchestration; step 5 is deferred.
Smaller changes finished one at a time are how this system gets more reliable —
that is the whole corrected premise.

---

## 9. What stays

- **Django** — data and domain layer.
- **SQLite** (WAL, `IMMEDIATE`) — the zero-ops orchestration store, plus the loops,
  FSM, leases, heartbeats, cron, crash-resume, retries.
- **The skills + overlay domain logic** — the actual value of the system.
- **Claude Code** — the interactive cockpit, unchanged.

---

## 10. Risks / revisit triggers

| Risk | Mitigation / revisit trigger |
|---|---|
| **OrcaRouter is the youngest option** | Thin OpenAI-compatible seam behind Pydantic AI's model class; OpenRouter is the mature fallback. Revisit if OrcaRouter's availability or behaviour proves unstable in the metered lane. |
| **Critic path under a black-box router** | The verification/critic slot is bound to a concrete honest model (a Layer-2 binding value, not a new mechanism). Revisit if router opacity ever leaks into that slot. |
| **Crash-mid-merge (resume)** | Idempotent integrate, each action gated on a ground-truth check (HEAD/merged?). Gives safe *re-run of the same merge*, not mutual exclusion. The one place to invest care. |
| **Split-brain on lease handoff (mutual exclusion)** | Distinct from crash-resume: a TTL lease-expiry TOCTOU, or a human steal mid-drain, can leave two sessions both believing they are t3 master — old master finishing X racing new master starting Y on `main`. Mitigation: a **fencing / lease-generation token** re-checked **at the git write** (not just the FSM transition); bounded lease TTL + higher-generation failover; a human steal **waits for the in-flight drain to quiesce** and fences the old worker at its next push. Revisit if a straggler write ever lands under a superseded generation. |
| **Sub-agent authority via inherited session id** | Plan A (sub-agent shares the parent session id) must be confirmed by a pinning test, not assumed. Plan B does not hinge on it: the master passes its **t3 master lease / fencing token explicitly** to the merge-worker and the reserved check accepts that token, so a failed pin does not strand the master's own merge-worker. |
| **DBOS/Postgres reversal** | Recorded as a superseded position (§1) so the reasoning is legible if the question reopens. |

---

## 11. Decision status

Proposed, pending independent review. Nothing here is implemented; no runtime
code changes with this document. If accepted, the migration proceeds as the
ordered, separately-reviewed PRs of §8, each with its own pinning test, with the
interactive Claude Code path working throughout.
