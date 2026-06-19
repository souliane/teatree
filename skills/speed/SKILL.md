---
name: speed
description: The parallel-work throughput dial — slow / medium / full / boost. `boost` runs one parallel-backlog-blast wave; `full` arms a self-sustaining boost loop; `medium` (baseline) and `slow` cap concurrency. Use when the user says "speed", "go full speed", "full speed", "blast the backlog", "boost", "parallel mode", "max throughput", "go wide", "slow down", or "set speed".
compatibility: any
requires:
  - rules
  - workspace
triggers:
  priority: 70
  keywords:
    - '\b(t3:?speed|set speed|speed (up|level)|go full[- ]speed|full[- ]speed)\b'
    - '\b(blast the backlog|parallel mode|max throughput|go wide|parallel backlog|boost mode)\b'
    - '\b(work in parallel|fan[- ]out|all tickets at once|tackle everything|slow down the loop)\b'
search_hints:
  - speed
  - full speed
  - boost
  - blast backlog
  - parallel mode
  - max throughput
  - go wide
  - fan out
  - slow down
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Speed — the parallel-work throughput dial

`speed` is a single dial governing **how many threads of work the orchestrator drives at once**. It is orthogonal to `mode` and `autonomy` (which gate *whether* a publishing action may proceed) — `speed` never relaxes a safety gate, it only changes concurrency.

The dial, lowest to highest throughput (default **`medium`**):

| Level | Concurrency |
|-------|-------------|
| **`slow`** | At most **one implementation worker** in flight at a time (the cold-review reviewer still runs separately). For a fragile tree or a constrained host. |
| **`medium`** (baseline) | **NO orchestrator fan-out.** Throughput comes only from the intrinsic loop, the PR sweep, and the per-overlay `max_concurrent_auto_starts` auto-start cap. |
| **`full`** | Arm `/loop /t3:speed boost` — each wave re-classifies the backlog and fans out a burst, sustained across waves. |
| **`boost`** | Exactly **one** parallel-backlog-blast wave, clamped to `max_concurrent_auto_starts`. |

## Resolving the invocation

- **No argument (`/t3:speed`)** → treat as **`full`**: arm the boost loop. A bare invocation is the deliberate "go fast now" override regardless of the persisted baseline.
- **`/t3:speed <level>`** → run that level once and persist it as the resting dial: call `t3 <overlay> speed set <level>` (never hand-edit `~/.teatree.toml`). Then act on the level per the table below.
- **`/t3:speed show`** → report the effective dial via `t3 <overlay> speed show` and stop.

The persisted value (`[teatree] speed`, per-overlay overridable, `T3_SPEED` env) is the resting dial the loop reads each tick. Friendly aliases on input: `low`→`slow`, `normal`→`medium`, `high`→`full`.

## `slow` — single-worker

Dispatch implementation work strictly one ticket at a time. The independent cold-review reviewer (maker ≠ checker) still runs in its own worktree — `slow` caps *implementation* concurrency, not the review that gates a merge. Do not fan out a second impl worker until the first has pushed.

## `medium` — baseline, no fan-out

Do nothing extra. The loop, the PR sweep, and the `max_concurrent_auto_starts` auto-start cap are the only sources of concurrency. This is the resting posture: the orchestrator does not blast the backlog.

## `full` — arm the boost loop

Run `/loop /t3:speed boost`. Each wave:

1. Re-reads the effective `speed` (`t3 <overlay> speed show`). If it is no longer `full`, **self-terminate the loop** — the dial was turned down.
2. Runs one `boost` wave (below).
3. Yields to the next interval.

The classification each wave is **agent judgment in prose** (the bucketing below), never a Python scanner.

## `boost` — one parallel wave, session TODO list FIRST

An explicit burst that **starts from the session TODO list** — the harness task list for THIS session (`/t3:todos` / `TaskList`). `boost` completes the work already on the session's plate **before** it touches the forge. **Only once the session TODO list is complete** does it go on to classify and blast every open, assigned forge ticket (`gh issue list` / `glab issue list`). Never pull fresh forge tickets while session TODO items are still open — finish the plate first. (This is the former `/t3:full-speed` behaviour.)

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

Dispatch one worker sub-agent per ticket, all in parallel. Each worker:

- Creates its own isolated worktree via `t3 <overlay> workspace ticket <ticket_url>`.
- Runs the full delivery cycle (implement → test → self-review → push → PR) as documented in [`../teatree-batch/SKILL.md`](../teatree-batch/SKILL.md) § Workflow.
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

`model="opus"` is a required parameter of every teammate spawn, not a budget knob — omitting it or downgrading to `sonnet`/`haiku` is the banned path. A teammate is long-lived — it claims a unit, works it across many turns, waits on CI, picks up the next unit — so a `sonnet` teammate hits its compaction threshold mid-task and silently loses the context it was carrying (the diff, the plan, the half-written test). `sonnet` is for explicit one-off **non-team** sub-agents (a quick read-only fetch, a throwaway grep), never a standing teammate; `fable` stays banned for team mates (too token-expensive, reserved for honesty-critical verification). The tier is a required, fixed parameter of a teammate spawn, not a budget knob — downgrading a mate to save tokens is a false economy, because the compacted mate re-reads everything and redoes work. Pinned by `evals/scenarios/speed.yaml` (`team_mate_spawned_opus_never_sonnet`).

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
