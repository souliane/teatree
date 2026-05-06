---
name: teatree-batch
description: Unattended batch ticket processing — work through a prioritized backlog one ticket at a time, sequentially. Create worktree, implement with TDD, self-review, push, merge, clean up. Skip tickets that need design decisions. Use when the user says "batch mode", "work unattended", "tackle tickets", or "quick wins".
metadata:
  version: 0.0.1
  subagent_safe: false
triggers:
  priority: 85
  keywords:
    - '\b(batch mode|work unattended|tackle tickets|quick wins)\b'
search_hints:
  - batch mode
  - unattended
  - quick wins
  - tackle tickets
  - sequential delivery
---

# TeaTree — Batch Mode (Unattended Ticket Processing)

Follows after prioritization (see `/teatree-plan`). Use when the user says "batch mode", "work unattended", "tackle tickets", or "quick wins".

## Prerequisites

Load `ac-python` and `ac-django` — all code must follow their review checklists. If the overlay has a companion skill, load it too.

## Workflow

1. **Run a codebase health audit** (load `ac-reviewing-codebase` in a sub-agent). Scope: all repos in the user's workspace directories. This finds actionable items beyond the issue tracker: god-modules, broken CI gates, missing coverage, stale branches.
2. **Fetch the prioritized board** (see `/teatree-plan` § 6) and sort by effort (quick wins first).
3. **For each ticket**, in order:
   - Read the issue. If it requires design decisions or user input, **skip it** and move to the next.
   - Create a worktree via `t3 teatree workspace ticket <ticket_url>` (uses `$T3_WORKSPACE_DIR`).
   - Implement following `ac-python`/`ac-django` standards. When a teatree change affects the overlay API, make the corresponding overlay fix in the same session.
   - Run tests + lint, self-review with a `t3:reviewer` sub-agent.
   - Push, create PR, wait for CI, merge.
   - Clean up worktree, update main.
   - **Merge each PR before starting the next** (sequential, not parallel).
4. **Close stale issues** that are already resolved in the codebase.
5. **Report** what was done and what was skipped (with reasons) at the end.

## Handling User Requests Mid-Session

During batch/quickwin sessions, the user may send new requests (bug reports, feature ideas, feedback) while you're implementing a ticket. When this happens:

1. **Create a GitHub issue immediately** for the new request — don't defer or forget it.
2. **Resume the current ticket** without losing progress.
3. If the request is a quick rename or one-line fix in a file you're already editing, fold it into the current PR.
4. If the request requires its own worktree/branch, add it to your ticket queue and implement it in order after the current ticket.

## Rules

- Never edit the main clone — always use worktrees.
- Never create issues/PRs without implementing them.
- Skip tickets needing architectural decisions — collect them for the user.
- Self-review every PR before merging.
- Commit progressively at stable states.
- Fix overlays together with core changes — don't leave them broken.
