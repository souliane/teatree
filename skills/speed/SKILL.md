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

## `boost` — one parallel-backlog-blast wave

An explicit burst across the pending backlog. This is the former `/t3:full-speed` behaviour, unchanged.

### Classify before dispatching

Before spawning any worker, sort every open, assigned ticket into exactly one bucket:

| Bucket | Criteria | Action |
|--------|----------|--------|
| **(a) Autonomous-safe** | Teatree/overlay code, structural work, bug-fixes with clear scope, no ambiguous spec, no human-gated substrate merge | Fan out in parallel — one worker per ticket |
| **(b) Needs-user** | Ambiguous spec, architectural choice with ≥2 equally-valid options, substrate merge that requires human authorize | Surface individually via `AskUserQuestion`; do not batch into a menu |
| **(c) Colleague-facing** | Client overlay repos, tenant-scoped changes, anything that triggers a peer review gate | Hold; route one-at-a-time after human confirmation |

Only bucket (a) gets blasted unattended. Tickets in (b) and (c) surface in separate, individual `AskUserQuestion` calls — the one-at-a-time rule from [`../rules/SKILL.md`](../rules/SKILL.md) § "Always Use AskUserQuestion for Questions" applies strictly here. Never present (b)/(c) as a bulk-approval menu.

### Fan-out pattern for bucket (a)

Dispatch one worker sub-agent per ticket, all in parallel. Each worker:

- Creates its own isolated worktree via `t3 <overlay> workspace ticket <ticket_url>`.
- Runs the full delivery cycle (implement → test → self-review → push → PR) as documented in [`../teatree-batch/SKILL.md`](../teatree-batch/SKILL.md) § Workflow.
- Returns a structured result the orchestrator records before handling results.

The orchestrator (main conversation) fans out all (a) workers simultaneously, collects results as they land, and merges PRs in dependency-aware order (see § Merge serialization below). It holds no per-ticket implementation context. Fan-out is clamped to `max_concurrent_auto_starts` so a wave never exceeds the per-overlay auto-start budget.

Each worker dispatch prompt MUST open with:

```text
NEAR-ZERO COMMENTS: names + types are the documentation. Do NOT add comments that restate the code. NO comments referencing MRs/tickets/workstreams/Slack threads. Rationale belongs in the commit message, never inline.
```

Skill prose does not propagate into a spawned agent's context — include the instruction verbatim.

### Fixed roster in Agent-Team mode

The fan-out above spawns an ephemeral worker per ticket only in **solo** mode (the main agent owns the Agent/Task tool). When the session is an **Agent Team**, the roster is **fixed up front**: the team's makers and reviewer are created once. A new task is then routed to an **existing idle teammate** via the shared task list — `TaskUpdate` the task's `owner` to that teammate (or the teammate claims it), then a `SendMessage` hands off context. Never spawn a **fresh teammate per task**: teammates cannot spawn teammates, the lead's roster is sized once, and minting a new mate per unit of work fragments ownership and breaks the claim model. Reuse the roster; the task list is the work queue, not a reason to grow the team.

**Team mates are spawned `model=opus`, never `sonnet` (Non-Negotiable).** When you do spawn an Agent-Team teammate (the boot-time roster, or a genuinely new standing role), the `Agent` spawn carries `model=opus`. A teammate is long-lived — it claims a unit, works it across many turns, waits on CI, picks up the next unit — so a `sonnet` teammate hits its compaction threshold mid-task and silently loses the context it was carrying (the diff, the plan, the half-written test). `sonnet` is for explicit one-off **non-team** sub-agents (a quick read-only fetch, a throwaway grep), never a standing teammate; `fable` stays banned for team mates (too token-expensive, reserved for honesty-critical verification). The tier is a required, fixed parameter of a teammate spawn, not a budget knob — downgrading a mate to save tokens is a false economy, because the compacted mate re-reads everything and redoes work. Pinned by `evals/scenarios/speed.yaml` (`team_mate_spawned_opus_never_sonnet`).

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
