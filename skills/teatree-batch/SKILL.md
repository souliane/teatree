---
name: teatree-batch
description: Unattended batch ticket processing — work through a prioritized backlog one ticket at a time, sequentially. Create worktree, implement with TDD, self-review, push, merge, clean up. Skip tickets that need design decisions. Use when the user says "batch mode", "work unattended", "tackle tickets", or "quick wins".
eval_exempt: batch-mode orchestration that delegates each ticket to the code/ship/review skills; their evals in evals/scenarios/ grade the actual per-step behaviour
requires:
  - teatree
metadata:
  version: 0.0.1
  subagent_safe: false
---

# TeaTree — Batch Mode (Unattended Ticket Processing)

Works the open issue tracker unattended, one ticket at a time. Use when the user says "batch mode", "work unattended", "tackle tickets", or "quick wins".

## Prerequisites

Load `ac-python` and `ac-django` — all code must follow their review checklists. If the overlay has a companion skill, load it too.

## Workflow

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

## Reserving tickets from the live factory (collision-safe burst)

When batch work runs **interactively while the headless factory is also live**, a ticket you pick up can be claimed by the factory at the same time — the ticket/worktree lease keys on the FSM claim, not on raw `git`/PR work done outside it, so an interactive burst and the loop are invisible to each other (souliane/teatree#3561). Reserve every ticket **before** touching it, and confirm the factory hasn't already claimed it.

- **Reserve with a generic label, not a harness-specific one.** Add the `interactive-implementation` label (generic and harness-agnostic — describes *why* it's held, not *who* holds it; do not overload `needs-triage`, which means "maintainer review required"). Keeping the issue **unassigned** is the second guard: the factory's `assigned_issues` scanner auto-starts only *assigned* issues.
- **The label must be honoured by the factory to be load-bearing.** Both intake paths (`assigned_issues` `exclude_labels`, and the trusted-author `issue_implementer`) need wiring to skip a reserved label; until that lands, *unassigned* is the real guard and the label is advisory. Tracking: souliane/teatree#3573 (consolidate the two intake paths into one scanner with a single `exclude_labels` gate).
- **Check-first, before implementing.** Confirm no loop worktree or task already owns the ticket: `t3 teatree worktree status <ticket>` (empty = unclaimed) and `t3 teatree tasks list` (no pending/claimed task for it). If the factory already holds it, pick another ticket — never race it.
- **Release on completion.** Remove the `interactive-implementation` label once the PR merges, so the ticket rejoins the factory's view if follow-up is needed.

### Sequential by default; parallel only for disjoint files

Reserving from the factory is orthogonal to how many burst coders run at once. Merge conflicts are the *second* collision axis (burst coders vs each other):

- **Default sequential** — the singleton delivery rule below stands: each PR merges before the next ticket starts, so burst PRs can never conflict at merge.
- **Parallelise only a disjoint-file set.** When several reserved tickets provably touch **non-overlapping files**, spawn one delivery sub-agent per ticket with `isolation: "worktree"` (each gets an isolated checkout, so writes never collide) and let their PRs land independently. Group tickets by file-overlap first; any two that touch the same file stay in the same sequential lane. When in doubt, sequential.

This whole pattern should compound into a single `t3` command (reserve + check + burst + release) rather than a hand-run query sequence — the interim queries above are the spec for it.

## Handling User Requests Mid-Session

During batch/quickwin sessions, the user may send new requests (bug reports, feature ideas, feedback) while you're implementing a ticket. When this happens:

1. **Create a GitHub issue immediately** for the new request — don't defer or forget it.
2. **Resume the current ticket** without losing progress.
3. If the request is a quick rename or one-line fix in a file you're already editing, fold it into the current PR.
4. If the request requires its own worktree/branch, add it to your ticket queue and implement it in order after the current ticket.

## Rules

- **Singleton delivery sub-agent (canonical statement).** The singleton constraint is scoped to exactly one thing: the batch-mode loop that *monitors the issue tracker / PR queue and triggers delivery work*. Within that loop, each ticket's full delivery cycle belongs to one dedicated sub-agent, spawned by the orchestrator and run one at a time — parallel delivery sub-agents are out of scope for batch mode. The constraint does not reach loops in general, nor sub-agent usage in general, nor any other concurrency in a session: an ordinary (non-monitor) session is free to use loops and sub-agents as usual. The orchestrator carries no per-ticket implementation context; it queues tickets, spawns the delivery sub-agent, and records its structured result, which keeps its context lean across a long backlog. This is the explicit batch-mode exception to [`../rules/SKILL.md`](../rules/SKILL.md) § "Sub-Agent Limitations": the delivery sub-agent loads the skills it needs via the Skill tool itself, so the "loses all loaded skills" caveat does not apply to it. The exception is scoped to the monitor/work-trigger loop and does not generalize to ordinary sessions.
- **Public-repo privacy gate.** A merge to a PUBLIC repo is preceded by a clean `t3 tool privacy-scan` on the branch-vs-base diff so no customer or internal identifier reaches a public repo. The `refuse-public-push-with-leak` pre-push hook is the deterministic enforcement; the canonical statement is [`../rules/SKILL.md`](../rules/SKILL.md) § "Verify Repo Visibility Before Filing External Issues".
- The main clone is read-only for batch work — every change happens in a worktree.
- Issues and PRs are created only when they are also implemented in the same session.
- Tickets that need architectural decisions are collected for the user rather than guessed at.
- Every PR is self-reviewed before it merges.
- Commits land progressively at stable states.
- Overlay fixes ship together with the core change they depend on rather than being left broken.
