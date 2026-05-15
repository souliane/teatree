---
name: update
description: WHEN to bring teatree core and registered overlays up to date with their default branch, and the safety guarantees of doing so. Use when the user wants to "sync teatree", "update teatree", "pull latest teatree", or after merging PRs that change framework/overlay code.
compatibility: macOS/Linux, git, python3.13+, uv.
triggers:
  priority: 75
  keywords:
    - '\b(update teatree|sync teatree|upgrade teatree|pull latest teatree|t3 update)\b'
search_hints:
  - update teatree
  - sync core and overlays
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

## Core ↔ Overlay Version Coupling

An overlay can pin (or assume) a particular teatree core version. After core
advances, an overlay that was in sync may now diverge from the core it
expects. Re-running after the core update lets the overlay reach its own
default branch and re-anchors editable installs, keeping the pair coherent.
