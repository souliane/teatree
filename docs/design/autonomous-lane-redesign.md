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
- **Measured cost.** About **92% of measured autonomous-lane spend is
  cache-reads**, dominated by Claude Code's per-request overhead — the giant
  system prompt and the ~48 skills re-read on every call. A lean headless runner
  sheds most of that. This is the concrete, measured reason; the strategic reason
  alone would not justify the move.

**Be honest about the coupling surface — this is not a one-file seam.** The
orchestration consumes an SDK-shaped contract, and the migration has to re-home
all of it. The real port surface:

| Today (`src/teatree/agents/`) | What it does | Why the port must carry it |
|---|---|---|
| `model_tiering.py` | Slot / honesty routing — picks the logical model slot per step | The orchestration decides the slot; the runtime must keep honouring it (see §3) |
| `result_schema.py` | The `needs_user_input` envelope | The blocked-subagent escalation path depends on this exact shape |
| `skill_injection.py` | The subagent skill preamble | Subagents must still receive their skill bundle headless |
| `outage_classifier.py` | Rate-limit / outage classification | Retry and backoff behaviour keys off this |
| `headless.py` + attempt/result plumbing | The attempt loop, results, heartbeats | The lease/heartbeat contract the orchestration relies on |

Naming these as the port surface is the point: the migration succeeds only when
each is re-homed behind Pydantic AI with a pinning test, not when a single entry
point is swapped.

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
autonomous lane is the metered, routed one. The two lanes have different cost
models on purpose.

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

### t3 master — the single privileged session

**t3 master** is the single privileged session, identified by the **t3 master
lease**. The lease is **atomic**: holding it *is* being t3 master, and it grants
all privileged roles together —

- owns the loops (drives `/t3:loops`),
- hosts the orchestrator agent,
- is the integration / merge authority,
- may perform `reserve_to_t3_master` FSM transitions.

It **does not split.** There is no separate "loop owner" alongside it. (The old
`loop-owner` lease and the `loop_owner` command are the *historical* name for
this same privilege; this design renames and unifies them into the t3 master
lease — `loop-owner` appears only as the thing being renamed, never as a live
separate concept.)

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

**Enforcement.** A transition is refused when it is reserved **and** the acting
session's id is not the t3 master lease holder. This is enforced at the single
`t3` CLI transition chokepoint — every FSM transition already goes through `t3`,
never raw `gh`/`glab`, so there is exactly one place to check.

**Refuse is not a dead-end.** A refused unit transitions to a
**ready-for-integration** hand-off state that the t3 master drains serially. The
producer is not stuck; it has handed off.

### Authority vs work

`reserve_to_t3_master` means only the t3 master may **initiate** the merge — not
that the master does the labour itself. The master **spawns a serialized
merge-worker sub-agent** to do the work, so the master stays responsive.

- The master's own sub-agents **share its session id**, so they pass the reserved
  check. *(Pinning test required: confirm a spawned sub-agent's `t3` calls inherit
  the parent session id used for the lease check. The whole serialization rests on
  this, so it must be pinned, not assumed.)*
- A non-master session's agents are **refused → enqueue** to the hand-off.
- **The human is never blocked.** To force a merge, claim the t3 master lease.

### Reconciliation — generic git, tech-stack-agnostic

Reconciliation is **generic git** (serial rebase / resolve) plus the existing
CI-green-before-merge gate. It is **tech-stack-agnostic** by design — this serves
*all* overlays, not just teatree's Django.

**No proactive, stack-coupled conflict detectors.** Worked example: two Django
migrations numbered `0028_*` git-merge clean but break the linear-migration
graph. The design does **not** add a migration-graph detector. Instead, when the
second unit is rebased onto the merged first, **Django's own check fails in CI**;
the t3 master sees red and dispatches a fix. Any stack-specific reconciliation, if
ever genuinely needed, lives behind an **overlay seam (opt-in)**, never in core.

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

Plus a **thin, always-on minimal guard** whose only job is: *work that belongs to
t3 is done through t3.* It fires **only** when the cwd touches a t3-owned unit — a
provisioned-worktree marker, or a managed repo on a branch with a backing claim.
Unrelated directories → it does nothing.

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
| **t3 master** | The single privileged session. |
| **t3 master lease** | The atomic lease whose holder *is* t3 master; grants all privileged roles together (loops, orchestrator, integration/merge authority, `reserve_to_t3_master` transitions). Does not split. Renames+unifies the former `loop-owner` lease. |
| **`reserve_to_t3_master`** | The FSM guard field on integration-class transitions. `t3_` prefix is deliberate (not a git "master" branch). |
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
   enforces "through t3" on t3-owned cwds and reads `autoload` via
   `cold_reader`; migrate `autoload` DB-home. (Reuses the existing gate pattern.)
3. **t3 master rename + unification** — rename the `loop-owner` lease to the t3
   master lease; introduce `reserve_to_t3_master` and its enforcement at the `t3`
   transition chokepoint; add the ready-for-integration hand-off and the
   serialized merge-worker; pin the sub-agent-inherits-session-id assumption.
   (Orchestration extension; independent of the runtime swap.)
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
| **Crash-mid-merge** | Idempotent integrate, each action gated on a ground-truth check (HEAD/merged?). The one place to invest care. |
| **Sub-agent inherits parent session id** | The serialization rests on this; it must be confirmed by a pinning test in the t3 master step, not assumed. |
| **DBOS/Postgres reversal** | Recorded as a superseded position (§1) so the reasoning is legible if the question reopens. |

---

## 11. Decision status

Proposed, pending independent review. Nothing here is implemented; no runtime
code changes with this document. If accepted, the migration proceeds as the
ordered, separately-reviewed PRs of §8, each with its own pinning test, with the
interactive Claude Code path working throughout.
