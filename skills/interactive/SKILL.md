---
name: interactive
description: The standing rule for an attended session — no work-bearing state is terminal, and the mechanisms that enforce it. Use when ending an interactive session, when a session-end report names stranded work, or when deciding what to do with uncommitted, unpushed, untracked, or unmerged work.
compatibility: any
requires:
  - rules
eval_exempt: states one invariant and points at the four mechanisms that enforce it deterministically; there is no agent trajectory here to grade that the mechanisms' own tests do not already pin
metadata:
  version: 0.0.1
  subagent_safe: true
---

# t3:interactive — no work-bearing state is terminal

## The invariant

A session does not end with work it authored sitting unmerged and untracked.

Work is *work-bearing* from the moment it exists in the working tree. There are five
such states, and none of them is a place work may come to rest:

| State | Rests when |
|---|---|
| unstaged in the working tree | committed |
| staged, uncommitted | committed |
| committed, unpushed | pushed |
| pushed, no PR | a PR exists |
| PR open, unmerged | merged, or closed with a reason |

Every state either advances or leaves a durable record that something else drains.
Nothing may exit 0 having observed work and stored nothing.

## The mechanisms

The invariant is not kept by remembering it. Four mechanisms enforce it, each
verifiable on its own:

**Durable deferral + drain.** `ensure-pr` runs pre-push, and a branch's FIRST push
legitimately has no remote ref to open a PR against. That deferral persists a row
carrying the repo, the branch and the PR spec rather than exiting quietly; the
`dispatch` loop drains it on a later tick, and a row that ages without draining
becomes a `t3 doctor check` failure. Verify: `t3 doctor check`.

**Teardown capture.** A checkout is snapshotted before it can be reaped — tracked
modifications, staged changes and unpushed commits, recorded in the DB rather than
only on disk. Dirtiness is read with `git status --porcelain` / `git diff HEAD`
everywhere it is decided; a bare `git diff` reports zero bytes against a worktree
holding only staged work. Verify: `t3 teatree workspace emit`, and
`/t3:sweeping-worktrees` for what to do with each emitted item.

**Session-end check.** Every session end sweeps all five states and names each item
with the exact command that advances it. It runs unconditionally — which skills a
session loaded says nothing about whether it stranded work — and it fails open, so a
probe that cannot answer contributes nothing rather than breaking the session. It
lives in `hooks/scripts/session_end_work_check.py`.

**Aged-skip surfacing.** The merge sweep declines to merge on about ten reasons, all
of them sound per tick and all of them silent. A reason that repeats for the same PR
across consecutive passes is announced once, naming the PR, the reason and how long
it has been held; `t3 doctor check` reports every aged hold standing.

## Using it

When a session-end report names stranded work, run the command it prints for each
item. The states are ordered, so an item usually needs its own next step and nothing
more — commit, push, `t3 teatree pr ensure-pr --branch <name>`, or let the ship loop
take the PR (`t3 loops tick --loop ship`).

Deleting an item is a decision, not a default: `/t3:sweeping-worktrees` covers
salvaging unmerged work to a fresh PR versus deleting something demonstrably shipped.
The reaper refuses a dirty checkout for that reason, so a kept worktree is not a
finished one.
