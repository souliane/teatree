---
name: update
description: WHEN to bring teatree core and registered overlays up to date with their default branch, and the safety guarantees of doing so. Use when the user wants to "sync teatree", "update teatree", "pull latest teatree", or after merging PRs that change framework/overlay code.
compatibility: macOS/Linux, git, python3.13+, uv.
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Teatree Update

The mechanism for syncing teatree core and every registered overlay to their
default branch is the deterministic CLI command `t3 update` — it is not
reproduced here. Run `t3 update`.

## Dependencies

- **setup** — `t3 update` reuses the idempotent setup/reinstall step at the
  end. The reverse never holds: `t3 setup` is pure bootstrap and never
  mutates checkouts or jumps the running code to newer `main`.

## When This Applies

Updating is a *mutating, network, version-changing* operation, deliberately
separate from the idempotent `t3 setup` bootstrap. It is relevant after
merging PRs that change framework or overlay code, or whenever the local
teatree core / overlay clones have drifted behind their default branch and
the running `t3` should reflect the latest code.

It is *not* part of routine setup: folding update into setup would let a
routine bootstrap silently jump the running code to newer `main` with
breaking changes.

## Safety Guarantee (Skips, Never Clobbers)

`t3 update` only fast-forwards a clean clone that is on its default branch
with a tracking upstream. A dirty working tree, a feature-branch checkout, or
a missing upstream is reported as a skip with a reason — work in progress is
never stashed, reset, merged, or rebased away. A skip is a normal outcome,
not a failure; the process exits non-zero only when a fetch or fast-forward
hard-fails.

## Self-DB Migrations

`t3 update` probes the teatree self-DB and, when migrations are pending,
applies them **non-destructively** (no DB drop). Both the probe and the
migrate run **in the runtime interpreter** (`python -m teatree migrate`),
so they target the exact control DB the running `t3` resolves — not a
`uv --directory <clone>` sibling DB, which for a worktree-anchored editable
install auto-isolates onto a different DB and silently reports
"already migrated" while the runtime DB stays stale (#126). This is the
sanctioned first-class alternative to the destructive `resetdb` (which
discards all local ticket/session/lease state).

For an on-demand, always-available self-rescue of a stale runtime self-DB —
e.g. when the sanctioned merge path refuses with "unapplied migration(s)" —
run `t3 teatree db migrate`. It applies pending migrations in-process against
the same DB the merge gate reads, is idempotent and non-destructive, and is
reachable even while the merge gate is refusing.

The migration is gated on **whether migrations are actually pending —
not on whether a repo advanced this run** (#929). An interrupted prior
`t3 update` (pulled new migrations, killed before reinstall) or an
out-of-band `git pull` before `t3 update` runs leaves the SHA already
current; the next run still probes and migrates the stale self-DB. A
migration failure is **fail-closed**: `t3 update` exits non-zero rather
than swallowing a warning, so it can never report success with a
half-migrated self-DB and silently break the sanctioned merge path's
fail-closed-on-unmigrated-self-DB guarantee (#870). A missing `uv` or an
unresolvable clone can't be probed — that warns but doesn't hard-fail
(unverifiable differs from verified-unmigrated).

## Core ↔ Overlay Version Coupling

An overlay can pin (or assume) a particular teatree core version. After core
advances, an overlay that was in sync may now diverge from the core it
expects. Re-running after the core update lets the overlay reach its own
default branch and re-anchors editable installs, keeping the pair coherent.
