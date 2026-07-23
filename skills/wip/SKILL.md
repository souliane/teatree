---
name: wip
description: The bounded-WIP throughput dial — slow / medium / full / boost — plus the WRITE-parallel / MERGE-serial phase split and the per-ticket unattended delivery cycle. `boost` keeps `boost_concurrency = N` workers live; `full` arms a self-sustaining boost loop; `medium` (baseline) and `slow` cap concurrency. Use when the user says "wip", "go full speed", "blast the backlog", "boost", "parallel mode", "max throughput", "go wide", "slow down", "set wip", "work unattended", or "tackle tickets".
compatibility: any
requires:
  - rules
  - workspace
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Wip — the bounded-WIP throughput dial

`wip` is a single dial governing **how much new work a loop tick admits at once**. It is orthogonal to `mode` and `autonomy` (which gate *whether* a publishing action may proceed) — `wip` never relaxes a safety gate, it only changes concurrency.

The dial, lowest to highest throughput (default **`medium`**):

| Level | Concurrency |
|-------|-------------|
| **`slow`** | At most **one implementation worker** in flight at a time (the cold-review reviewer still runs separately). For a fragile tree or a constrained host. |
| **`medium`** (baseline) | **NO orchestrator fan-out.** Throughput comes only from the intrinsic loop, the PR sweep, and the per-overlay `max_concurrent_auto_starts` auto-start cap. |
| **`full`** | Arm `/loop /t3:wip boost` — each wave re-classifies the backlog and fans out a burst, sustained across waves. |
| **`boost`** | A **pool-refill** burst that keeps `boost_concurrency = N` live workers in flight: when a worker exits below `N`, the next tick admits the shortfall. `N` is clamped by the PR-01 resource concurrency ceiling; with `boost_concurrency = 0` (unset) it keeps `full`'s summed `max_concurrent_auto_starts` target. Set both at once with `t3 <overlay> wip boost N`. |

## The phase split — WRITE parallel, MERGE serial

The dial is split by phase. Implementation (coding / testing / reviewing) runs
`write_wip` wide; the merge lane (shipping) is **single-flight**, so the next PR
always rebases against what just landed. That serialization IS the conflict-safety
guarantee, so `merge_wip` is clamped to 1 rather than honoured above it.

```bash
t3 <overlay> wip split 3     # write_wip = 3 (parallel), merge_wip = 1 (serial)
t3 <overlay> wip show        # the dial plus both lane widths
```

Each lane counts its own in-flight workers, so a saturated merge lane never
starves implementation and a full WRITE fan-out never blocks the one merge.
Within a lane, candidates are spread round-robin across a **cheap area key** (the
ticket's declared repos, else the issue URL's repo slug) so a fan-out prefers
tickets in different repos — deliberately a heuristic, not conflict prediction. It
only re-orders; nothing is dropped.

## Resolving the invocation

- **No argument (`/t3:wip`)** → treat as **`full`**: arm the boost loop. A bare invocation is the deliberate "go fast now" override regardless of the persisted baseline.
- **`/t3:wip <level>`** → run that level once and persist it as the resting dial: call `t3 <overlay> wip set <level>` (never hand-edit config). Then act on the level per the table below.
- **`/t3:wip show`** → report the effective dial via `t3 <overlay> wip show` and stop.
- **`/t3:wip split N`** → set the WRITE lane width: `t3 <overlay> wip split N` (merge stays single-flight).
- **`/t3:wip boost N`** → arm the pool-refill burst at a live-worker target of `N`: `t3 <overlay> wip boost N` (sets `wip = boost` and `boost_concurrency = N` in one write). Admission drains queued TODO/followup work before auto-starting new tickets.

The persisted value (the DB-home `wip` setting in the `ConfigSetting` store, per-overlay overridable, `T3_WIP` env) is the resting dial the loop reads each tick. A `[teatree] wip` TOML value is ignored on read; persist it with `t3 <overlay> config_setting set wip <level>` (the `t3 <overlay> wip set` wrapper does this for you). Friendly aliases on input: `low`→`slow`, `normal`→`medium`, `high`→`full`.

## `slow` — single-worker

Dispatch implementation work strictly one ticket at a time. The independent cold-review reviewer (maker ≠ checker) still runs in its own worktree — `slow` caps *implementation* concurrency, not the review that gates a merge. Do not fan out a second impl worker until the first has pushed.

## `medium` — baseline, no fan-out

Do nothing extra. The loop, the PR sweep, and the `max_concurrent_auto_starts` auto-start cap are the only sources of concurrency. This is the resting posture: the orchestrator does not blast the backlog.

## `full` — arm the boost loop

Run `/loop /t3:wip boost`. Each wave:

1. Re-reads the effective `wip` (`t3 <overlay> wip show`). If it is no longer `full`, **self-terminate the loop** — the dial was turned down.
2. Runs one `boost` wave (below).
3. Yields to the next interval.

The classification each wave is **agent judgment in prose** (the bucketing below), never a Python scanner.

## `boost` — sustained pool refill, session TODO list FIRST

A sustained **pool-refill** drive that keeps `boost_concurrency = N` workers live — when a worker exits below `N`, the next tick admits the shortfall — and **starts from the session TODO list**: the harness task list for THIS session (`/t3:checking` / `TaskList`). `boost` completes the work already on the session's plate **before** it touches the forge. **Only once the session TODO list is complete** does it go on to classify and dispatch every open, assigned forge ticket — pull that list with the `mcp__teatree__github_issue_list_assigned` / `mcp__teatree__gitlab_issue_list_assigned` MCP tool (structured JSON, no text parsing; fall back to `gh issue list` / `glab issue list` when the MCP server isn't connected). Never pull fresh forge tickets while session TODO items are still open — finish the plate first.

### Classify before dispatching

Before spawning any worker, sort every open item — **session TODO items first, then (only once the TODO list is done) every open, assigned forge ticket** — into exactly one bucket:

| Bucket | Criteria | Action |
|--------|----------|--------|
| **(a) Autonomous-safe** | Teatree/overlay code, structural work, bug-fixes with clear scope, no ambiguous spec, no human-gated substrate merge | Fan out in parallel — one worker per ticket |
| **(b) Needs-user** | Ambiguous spec, architectural choice with ≥2 equally-valid options, substrate merge that requires human authorize | Surface individually via `AskUserQuestion`; do not batch into a menu |
| **(c) Colleague-facing** | Client overlay repos, tenant-scoped changes, anything that triggers a peer review gate | Hold; route one-at-a-time after human confirmation |

Only bucket (a) gets blasted unattended. Tickets in (b) and (c) surface in separate, individual `AskUserQuestion` calls — the one-at-a-time rule from [`../rules/SKILL.md`](../rules/SKILL.md) § "Always Use AskUserQuestion for Questions" applies strictly here. Never present (b)/(c) as a bulk-approval menu.

### Fan-out pattern for bucket (a)

**At `full`/`boost` over N autonomous-safe tickets, your single next action is to FAN OUT — one `Task`/`Agent` worker per ticket, in parallel — never to implement a ticket serially in the foreground (do X, never Y).** The cheap path is the trap: "I could just start editing TODO-7 myself and knock the three out one at a time" is exactly the serial-in-main drift this dial bans. The orchestrator **classifies and dispatches; it never implements**. So when the dial is `full` and three bucket-(a) tickets are in front of you, issue the parallel worker dispatches NOW — do **not** `Edit`/`Write` a ticket's `.py`, do **not** run its `pytest` / `ruff` / `git add` / `git commit` in the foreground.

```python
# Three autonomous-safe tickets at full speed. do X — fan out one parallel worker per ticket (orchestrator never implements):
Task(description="TODO-7 wire-provision-timeout", prompt="<NEAR-ZERO COMMENTS block> ... fix src/teatree/core/provision.py timeout guard, full delivery cycle, report branch+PR.")
Task(description="TODO-9 scanner-ordering",       prompt="<NEAR-ZERO COMMENTS block> ... fix src/teatree/loop/scanner.py ordering flake, full delivery cycle, report branch+PR.")
Task(description="TODO-11 notify-public route",   prompt="<NEAR-ZERO COMMENTS block> ... fix src/teatree/core/notify.py route classifier, full delivery cycle, report branch+PR.")
# never Y — do NOT pick up TODO-7 and implement it yourself in the foreground, one ticket at a time:
# Edit(file_path="src/teatree/core/provision.py", ...)   # FORBIDDEN: serial-in-main is the drift the dial bans
```

**The fan-out IS the action — once the N dispatches are issued your turn is DONE; do NOT then implement the tickets yourself in the foreground (do X, never Y).** The worst recurrence under load is not skipping the fan-out — it is firing the N parallel dispatches (so a "did you fan out" check passes) and then, in the SAME turn, **re-doing every ticket by hand**: `find`/`grep` to locate each module, `Edit` its `.py`, `Write` its test, `git checkout -b`, `pytest`, `git commit` — exactly what the workers were dispatched to do. That hybrid (fan-out THEN serial) is strictly worse than pure serial: the workers and the main agent now both implement the same three tickets, the work is duplicated, and the run blows its budget/timeout grinding through all three in the foreground. **A fan-out you immediately undo by hand-doing the tickets is not a fan-out.** So after issuing the parallel dispatches, STOP — the orchestrator does not locate files, edit `.py`, write tests, create branches, or run `pytest`/`git commit` for any dispatched ticket. Its next foreground action is collecting the workers' reported results, never re-implementing their units.

**The fan-out is the LAST tool call of the turn — re-investigation is forbidden too, not only re-implementation (do X, never Y).** The drift hides in a softer move than re-editing a ticket's `.py`: after the N dispatches the agent "just has a quick look" at the first ticket — `find`/`cat`/`ls`/`grep`/`rg` in `Bash`, or `Read`/`Glob`/`Grep` — to inspect a module it just handed to a worker, and that read-only peek slides straight into editing, testing, and committing it serially. A read-only probe of a dispatched ticket is NOT a harmless look; it is the first step of re-doing the worker's job and it has zero purpose for the orchestrator (each worker reads its own files inside its own worktree). So once the wave is fanned out: the **only** permitted next foreground actions are dispatcher work — fan out the next wave, arm a `Monitor`, or surface a `(b)`/`(c)` decision via `AskUserQuestion` — never `find`/`cat`/`ls`/`grep`/`Read`/`Edit`/`Write`/`pytest`/`git` against a dispatched ticket's surface. When there is no further ticket to dispatch and no decision to surface, the turn ENDS — an empty post-fan-out turn is the correct shape; filling it with foreground `find`/`cat`/`Edit` is the recurrence. The test: if your next tool call names or touches a file/module/ticket you just dispatched — to read it OR to write it — you have re-entered serial-in-main mode. This mirrors [`../rules/SKILL.md`](../rules/SKILL.md) § "DISPATCH IMMEDIATELY" → "Post-dispatch checklist".

Dispatch one worker sub-agent per ticket, all in parallel. Each worker:

- Creates its own isolated worktree via `t3 <overlay> workspace ticket <ticket_url>`.
- Runs the full delivery cycle (implement → test → self-review → push → PR) documented in § "Unattended delivery" below.
- Returns a structured result the orchestrator records before handling results.

The orchestrator (main conversation) fans out all (a) workers simultaneously, collects results as they land, and merges PRs in dependency-aware order (see § Merge serialization below). It holds no per-ticket implementation context. Fan-out is clamped to `max_concurrent_auto_starts` so a wave never exceeds the per-overlay auto-start budget.

**Dispatch with the ticket scope you already have — never stall a wave to ask for issue URLs.** A worker needs only the ticket id and its scope (the module/file paths and the one-line ask) to start; the WORKER resolves its own worktree and the canonical ticket URL inside its run (`t3 <overlay> workspace ticket <id>`). The orchestrator does NOT pre-resolve a forge URL per ticket before it can dispatch. So when a `full`/`boost` directive hands you a backlog of identified tickets (e.g. `TODO-7`, `TODO-9`, `TODO-11` with their file paths), the right move is to **issue the parallel `Agent`/`Task` dispatches NOW** — one per ticket, carrying the id + scope — not to reply "give me the GitHub/GitLab issue URLs first". Asking for a URL you don't strictly need to dispatch is the serial-stall this dial bans; a missing URL is a thing the worker fetches, not a blocker on the fan-out.

Each worker dispatch prompt MUST open with:

```text
NEAR-ZERO COMMENTS: names + types are the documentation. Do NOT add comments that restate the code. NO comments referencing MRs/tickets/workstreams/Slack threads. Rationale belongs in the commit message, never inline.
```

Skill prose does not propagate into a spawned agent's context — include the instruction verbatim.

### Fixed roster in Agent-Team mode

The fan-out above spawns an ephemeral worker per ticket only in **solo** mode (the main agent owns the Agent/Task tool). When the session is an **Agent Team**, the roster is **fixed up front**: the team's makers and reviewer are created once. A new task is then routed to an **existing idle teammate** via the shared task list — `TaskUpdate` the task's `owner` to that teammate (or the teammate claims it), then a `SendMessage` hands off context. Never spawn a **fresh teammate per task**: teammates cannot spawn teammates, the lead's roster is sized once, and minting a new mate per unit of work fragments ownership and breaks the claim model. Reuse the roster; the task list is the work queue, not a reason to grow the team.

**As the team LEAD with a new task and an idle teammate, your single next action is a `TaskUpdate`/`SendMessage` to that existing mate — NEVER an `Agent` spawn (do X, never Y).** The under-load reflex is "spin up a worker for this" — that is the spawn-per-task drift the user flagged. In team mode the roster is fixed; "also handle this" / "now also handle X" is answered by ASSIGNING the unit to whichever teammate is idle (e.g. `core-maker`), not by minting a new mate. A new `Agent` spawn for a task the standing roster can take is the failure, even if the spawned mate is plausibly named. The Agent tool is a **boot-time** roster act, never a per-unit move.

Worked example — a new task arrives mid-run and `core-maker` is idle. Route it to the existing mate; do **not** `Agent`-spawn a new one:

```python
# DO — assign the unit to an existing idle teammate via the shared task list, then hand off context:
TaskUpdate(id="<task-id>", owner="core-maker")
SendMessage(to="core-maker", body="<task-id> is yours: <one-line context + acceptance>")

# NEVER — minting a fresh per-task teammate (any Agent spawn) for a task the fixed roster can take:
# Agent(team_name="<team>", name="ship-flag-maker", model="opus", prompt="...")   # banned: roster is fixed
```

In team mode the correct response to "also handle this" is a `TaskUpdate`/`SendMessage` to an idle roster member, never a new `Agent` spawn.

**Team mates are spawned `model=opus`, never `sonnet` (Non-Negotiable).** When you do spawn an Agent-Team teammate (the boot-time roster, or a genuinely new standing role), the `Agent` spawn carries `model=opus`.

Worked example — spawning a standing teammate. The `model="opus"` argument is required and fixed; do this, never the cheaper variant:

```python
# DO — every Agent-Team teammate (boot roster or a new standing role) is opus:
Agent(team_name="<team>", name="core-maker", model="opus", prompt="<role brief>")

# NEVER — a sonnet (or omitted-model) teammate auto-compacts mid-task and drops its context:
# Agent(team_name="<team>", name="core-maker", model="sonnet", prompt="...")   # banned
# Agent(team_name="<team>", name="core-maker", prompt="...")                   # banned: model omitted
```

**Spawning a teammate is a real `Agent` tool call — never narrate, echo, or shell it.** Issue the actual `Agent(...)` tool invocation. Do NOT print the spawn as text, do NOT wrap it in a Bash `cat <<'EOF' … Agent(…) … EOF` heredoc, and do NOT reply "I don't have an Agent tool" — when the task is to spawn a standing teammate, the `Agent` tool is the action, and a `Bash`/`echo` rendering of it is a non-action that spawns nothing. One `Agent` call with `model="opus"`, then stop.

`model="opus"` is a required parameter of every teammate spawn, not a budget knob — omitting it or downgrading to `sonnet`/`haiku` is the banned path. A teammate is long-lived — it claims a unit, works it across many turns, waits on CI, picks up the next unit — so a `sonnet` teammate hits its compaction threshold mid-task and silently loses the context it was carrying (the diff, the plan, the half-written test). `sonnet` is for explicit one-off **non-team** sub-agents (a quick read-only fetch, a throwaway grep), never a standing teammate; a team mate is never spawned above the `opus` tier either — a costlier model is too token-expensive for routine roster work and reserved for honesty-critical verification only. The tier is a required, fixed parameter of a teammate spawn, not a budget knob — downgrading a mate to save tokens is a false economy, because the compacted mate re-reads everything and redoes work. This opus-floor is enforced in the **real Agent-Team runtime** (the host roster) — the headless SDK eval lane fixes the run model centrally and cannot control or verify a per-teammate tier, so it is NOT graded there. teatree's own maker-pane spawn helper (`teatree.teams.pane_spawn._floor_teammate_model`) enforces the same floor **deterministically** on its SDK pane layer — an inherited or sub-opus resolution is raised to opus — so the native roster and the pane layer agree: a team mate is opus or stronger, full stop.

**Delegate the heavy standing-role unit to a sub-agent — never do the heavy work inline in the main agent (do X, never Y).** When a big multi-file standing-role unit is overdue (e.g. the BLUEPRINT + README sync the makers keep deferring), the team lead's single next action is to DISPATCH it — an `Agent`/`Task` whose prompt is that unit — keeping the main agent thin. The cheap path is the trap: "the docs sync is mechanical, I'll just open the BLUEPRINT here and knock it out myself" is the do-it-inline drift. The lead classifies and dispatches; it does not `Edit`/`Write` the BLUEPRINT/README itself or run the doc pass in the foreground.

```python
# DO — dispatch the heavy standing-role unit to a sub-agent; the main agent stays thin:
Agent(name="docs-maker", model="opus", prompt="Do the overdue BLUEPRINT + README sync in a fresh worktree; commit; report back.")

# NEVER — open the BLUEPRINT and edit it inline in the main agent because "it's mechanical":
# Edit(file_path="BLUEPRINT.md", ...)   # banned: the lead dispatches, it does not implement
```

The opus-floor (above) is the host-runtime tier rule; this delegate-don't-do-it-inline rule is the SDK-testable essence both the real runtime and the eval lane share. Pinned by `evals/scenarios/wip.yaml` (`team_mate_spawned_opus_never_sonnet` — the SDK lane grades the delegation, the host runtime enforces the opus tier).

### Hard rails parallelization must not break

These are references to canonical rule homes, not restatements:

- **Substrate merges stay one-by-one** — each requires a separate human authorize via `AskUserQuestion`; never batch them. (`/t3:rules` § "Always Use AskUserQuestion for Questions")
- **maker ≠ checker** — every PR gets an independent cold-review sub-agent in an isolated worktree before merge. Parallel workers must not review each other's PRs. (`/t3:rules` § "Concurrent Agent Safety")
- **Dependency-aware merge chains** — when multiple PRs land in the same repo, the forge's "require up-to-date" rule serializes merges: update-branch + re-wait CI on each PR before issuing its merge. Fan-out dispatch is parallel; same-repo merges are sequential. (`/t3:rules` § "Never Change PR Base Branch or Dependencies")
- **One consolidated MR per repo for cleanup work** — structural or multi-item cleanup in a single repo ships as one PR, not one-per-item. (`/t3:rules` § "Do Work Now, Don't Defer to 'Later' Tickets")
- **No code in the main agent, no edits to the main clone** — all implementation happens in worktrees via sub-agents. (`/t3:rules` § "Worktree-First Work")
- **Privacy gate on public repos** — `refuse-public-push-with-leak` pre-push hook runs `t3 tool privacy-scan`; clean scan is a precondition for every push. (`/t3:rules` § "Verify Repo Visibility Before Filing External Issues")

### Merge serialization

After workers return, merge PRs in this order:

1. PRs with no same-repo siblings first (safe to merge immediately on CI green).
2. For each repo with multiple pending PRs: merge in dependency order, updating the branch and waiting for CI to re-green before each successive merge.
3. Never issue a merge for a PR whose base is another open PR — wait for the base to land first.

Use `t3 <overlay> ticket merge <clear_id>` (the keystone path, not raw `gh pr merge`) for every merge.

### Result tracking

After each worker returns, record its result before starting the next merge cycle:

```text
✓ #<IID> — <title>
  PR: <clickable url> | CI: green | merged: yes/no
```

Present a summary table after all workers have reported and all green PRs are merged:

| Ticket | Status | PR | Notes |
|--------|--------|----|-------|
| #N | Merged | [!X](url) | — |
| #N | Held | — | Needs architectural decision |
| #N | Open | [!X](url) | Awaiting CI |

## Unattended delivery — the per-ticket cycle

### Per-ticket delivery cycle

1. **Run a codebase health audit** (load `ac-reviewing-codebase` in a sub-agent). Scope: all repos in the user's workspace directories. This finds actionable items beyond the issue tracker: god-modules, broken CI gates, missing coverage, stale branches.
2. **Fetch "what's next"** — the repo's open issues plus any tracking epics from `/t3:sweeping-tickets` (an epic groups related work; pick its highest-value unchecked item) — and sort by effort (quick wins first). There is no board to sync; the tracker is just the open issues + the epics.
3. **For each ticket**, in order. The main conversation acts as the **orchestrator only** — it queues tickets, spawns one delivery sub-agent per ticket, records the structured result, and moves on. It holds no per-ticket implementation context (see § Rules "Singleton delivery sub-agent"):
   - The orchestrator reads only enough of the issue to decide routing. A ticket that needs design decisions or user input is skipped and the next one starts.
   - One delivery sub-agent owns the ticket's full cycle: it creates a worktree via `t3 teatree workspace ticket <ticket_url>` (uses `$T3_WORKSPACE_DIR`), implements to `ac-python`/`ac-django` standards (when a teatree change affects the overlay API, the corresponding overlay fix lands in the same session), runs tests + lint, and self-reviews with a `t3:reviewer` sub-agent.
   - The orchestrator's delivery-sub-agent dispatch prompt MUST open with this verbatim block — it is not optional and not a "remember to add it" note. Skill prose does not propagate into a spawned agent's context, so the near-zero-comments rule is lost unless it is inline in the prompt itself:

     ```text
     NEAR-ZERO COMMENTS: names + types are the documentation. Do NOT add comments that restate the code. NO comments referencing MRs/tickets/workstreams/Slack threads. Rationale belongs in the commit message, never inline.
     ```

   - **Privacy gate.** A push to a PUBLIC repo is gated by the `refuse-public-push-with-leak` pre-push hook: it runs `t3 tool privacy-scan` on the branch-vs-base diff and the push is refused on any finding. The delivery sub-agent treats a clean scan as a precondition for the push, not an afterthought — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Verify Repo Visibility Before Filing External Issues".
   - The sub-agent pushes, creates the PR, waits for CI, merges, cleans up the worktree, and updates main, then returns a structured result the orchestrator records before starting the next ticket.
   - Delivery is sequential — each PR merges before the next ticket's sub-agent is spawned, never in parallel.
4. **Close stale issues** that are already resolved in the codebase.
5. **Report** what was done and what was skipped (with reasons) at the end.

### Reserving tickets from the live factory (collision-safe burst)

When burst work runs **interactively while the headless factory is also live**, a ticket you pick up can be claimed by the factory at the same time — the ticket/worktree lease keys on the FSM claim, not on raw `git`/PR work done outside it, so an interactive burst and the loop are invisible to each other (souliane/teatree#3561). Reserve every ticket **before** touching it, and confirm the factory hasn't already claimed it.

- **Reserve with a generic label, not a harness-specific one.** Add the `interactive-implementation` label (generic and harness-agnostic — describes *why* it's held, not *who* holds it; do not overload `needs-triage`, which means "maintainer review required"). Keeping the issue **unassigned** is the second guard: the factory's `assigned_issues` scanner auto-starts only *assigned* issues.
- **The label must be honoured by the factory to be load-bearing.** Both intake paths (`assigned_issues` `exclude_labels`, and the trusted-author `issue_implementer`) need wiring to skip a reserved label; until that lands, *unassigned* is the real guard and the label is advisory. Tracking: souliane/teatree#3573 (consolidate the two intake paths into one scanner with a single `exclude_labels` gate).
- **Check-first, before implementing.** Confirm no loop worktree or task already owns the ticket: `t3 teatree worktree status <ticket>` (empty = unclaimed) and `t3 teatree tasks list` (no pending/claimed task for it). If the factory already holds it, pick another ticket — never race it.
- **Release on completion.** Remove the `interactive-implementation` label once the PR merges, so the ticket rejoins the factory's view if follow-up is needed.

#### Sequential by default; parallel only for disjoint files

Reserving from the factory is orthogonal to how many burst coders run at once. Merge conflicts are the *second* collision axis (burst coders vs each other):

- **Default sequential** — the singleton delivery rule below stands: each PR merges before the next ticket starts, so burst PRs can never conflict at merge.
- **Parallelise only a disjoint-file set.** When several reserved tickets provably touch **non-overlapping files**, spawn one delivery sub-agent per ticket with `isolation: "worktree"` (each gets an isolated checkout, so writes never collide) and let their PRs land independently. Group tickets by file-overlap first; any two that touch the same file stay in the same sequential lane. When in doubt, sequential.

This whole pattern should compound into a single `t3` command (reserve + check + burst + release) rather than a hand-run query sequence — the interim queries above are the spec for it.

### Handling User Requests Mid-Session

During burst sessions, the user may send new requests (bug reports, feature ideas, feedback) while you're implementing a ticket. When this happens:

1. **Create a GitHub issue immediately** for the new request — don't defer or forget it.
2. **Resume the current ticket** without losing progress.
3. If the request is a quick rename or one-line fix in a file you're already editing, fold it into the current PR.
4. If the request requires its own worktree/branch, add it to your ticket queue and implement it in order after the current ticket.

### Rules

- **Singleton delivery sub-agent (canonical statement).** The singleton constraint is scoped to exactly one thing: the burst-mode loop that *monitors the issue tracker / PR queue and triggers delivery work*. Within that loop, each ticket's full delivery cycle belongs to one dedicated sub-agent, spawned by the orchestrator and run one at a time — parallel delivery sub-agents are out of scope for unattended burst mode. The constraint does not reach loops in general, nor sub-agent usage in general, nor any other concurrency in a session: an ordinary (non-monitor) session is free to use loops and sub-agents as usual. The orchestrator carries no per-ticket implementation context; it queues tickets, spawns the delivery sub-agent, and records its structured result, which keeps its context lean across a long backlog. This is the explicit burst-mode exception to [`../rules/SKILL.md`](../rules/SKILL.md) § "Sub-Agent Limitations": the delivery sub-agent loads the skills it needs via the Skill tool itself, so the "loses all loaded skills" caveat does not apply to it. The exception is scoped to the monitor/work-trigger loop and does not generalize to ordinary sessions.
- **Public-repo privacy gate.** A merge to a PUBLIC repo is preceded by a clean `t3 tool privacy-scan` on the branch-vs-base diff so no customer or internal identifier reaches a public repo. The `refuse-public-push-with-leak` pre-push hook is the deterministic enforcement; the canonical statement is [`../rules/SKILL.md`](../rules/SKILL.md) § "Verify Repo Visibility Before Filing External Issues".
- The main clone is read-only for burst work — every change happens in a worktree.
- Issues and PRs are created only when they are also implemented in the same session.
- Tickets that need architectural decisions are collected for the user rather than guessed at.
- Every PR is self-reviewed before it merges.
- Commits land progressively at stable states.
- Overlay fixes ship together with the core change they depend on rather than being left broken.
