---
name: full-speed
description: Parallel backlog blast — classify the actionable backlog, fan out autonomous-safe work across isolated worktrees, surface the rest. Use when the user says "full speed", "blast the backlog", "parallel mode", "max throughput", or "go wide".
compatibility: any
requires:
  - rules
  - workspace
triggers:
  priority: 70
  keywords:
    - '\b(full[- ]speed|blast the backlog|parallel mode|max throughput|go wide|parallel backlog)\b'
    - '\b(work in parallel|fan[- ]out|all tickets at once|tackle everything)\b'
search_hints:
  - full speed
  - blast backlog
  - parallel mode
  - max throughput
  - go wide
  - fan out
metadata:
  version: 0.1.0
  subagent_safe: false
---

# Full-Speed — Parallel Backlog Blast

An explicit, opt-in burst mode for maximum throughput across a pending backlog. Not the default because unconditional parallelism would recreate batching thrash and try to bypass the one-by-one human authorize gate on substrate merges. `/t3:full-speed` is the deliberate override.

## When to load

Load when the user wants to drive as many tickets in parallel as safely possible — "full speed", "blast the backlog", "go wide". Do NOT load for a single ticket; single-ticket work stays in the normal lifecycle (`/t3:code` → `/t3:ship`).

## Classify before dispatching

Before spawning any worker, sort every open, assigned ticket into exactly one bucket:

| Bucket | Criteria | Action |
|--------|----------|--------|
| **(a) Autonomous-safe** | Teatree/overlay code, structural work, bug-fixes with clear scope, no ambiguous spec, no human-gated substrate merge | Fan out in parallel — one worker per ticket |
| **(b) Needs-user** | Ambiguous spec, architectural choice with ≥2 equally-valid options, substrate merge that requires human authorize | Surface individually via `AskUserQuestion`; do not batch into a menu |
| **(c) Colleague-facing** | Client overlay repos, tenant-scoped changes, anything that triggers a peer review gate | Hold; route one-at-a-time after human confirmation |

Only bucket (a) gets blasted unattended. Tickets in (b) and (c) surface in separate, individual `AskUserQuestion` calls — the one-at-a-time rule from [`../rules/SKILL.md`](../rules/SKILL.md) § "Always Use AskUserQuestion for Questions" applies strictly here. Never present (b)/(c) as a bulk-approval menu.

## Fan-out pattern for bucket (a)

Dispatch one worker sub-agent per ticket, all in parallel. Each worker:

- Creates its own isolated worktree via `t3 <overlay> workspace ticket <ticket_url>`.
- Runs the full delivery cycle (implement → test → self-review → push → PR) as documented in [`../teatree-batch/SKILL.md`](../teatree-batch/SKILL.md) § Workflow.
- Returns a structured result the orchestrator records before handling results.

The orchestrator (main conversation) fans out all (a) workers simultaneously, collects results as they land, and merges PRs in dependency-aware order (see § Merge serialization below). It holds no per-ticket implementation context.

Each worker dispatch prompt MUST open with:

```text
NEAR-ZERO COMMENTS: names + types are the documentation. Do NOT add comments that restate the code. NO comments referencing MRs/tickets/workstreams/Slack threads. Rationale belongs in the commit message, never inline.
```

Skill prose does not propagate into a spawned agent's context — include the instruction verbatim.

## Hard rails parallelization must not break

These are references to canonical rule homes, not restatements:

- **Substrate merges stay one-by-one** — each requires a separate human authorize via `AskUserQuestion`; never batch them. (`/t3:rules` § "Always Use AskUserQuestion for Questions")
- **maker ≠ checker** — every PR gets an independent cold-review sub-agent in an isolated worktree before merge. Parallel workers must not review each other's PRs. (`/t3:rules` § "Concurrent Agent Safety")
- **Dependency-aware merge chains** — when multiple PRs land in the same repo, the forge's "require up-to-date" rule serializes merges: update-branch + re-wait CI on each PR before issuing its merge. Fan-out dispatch is parallel; same-repo merges are sequential. (`/t3:rules` § "Never Change PR Base Branch or Dependencies")
- **One consolidated MR per repo for cleanup work** — structural or multi-item cleanup in a single repo ships as one PR, not one-per-item. (`/t3:rules` § "Do Work Now, Don't Defer to 'Later' Tickets")
- **No code in the main agent, no edits to the main clone** — all implementation happens in worktrees via sub-agents. (`/t3:rules` § "Worktree-First Work")
- **Privacy gate on public repos** — `refuse-public-push-with-leak` pre-push hook runs `t3 tool privacy-scan`; clean scan is a precondition for every push. (`/t3:rules` § "Verify Repo Visibility Before Filing External Issues")

## Merge serialization

After workers return, merge PRs in this order:

1. PRs with no same-repo siblings first (safe to merge immediately on CI green).
2. For each repo with multiple pending PRs: merge in dependency order, updating the branch and waiting for CI to re-green before each successive merge.
3. Never issue a merge for a PR whose base is another open PR — wait for the base to land first.

Use `t3 <overlay> ticket merge <clear_id>` (the keystone path, not raw `gh pr merge`) for every merge.

## Result tracking

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
