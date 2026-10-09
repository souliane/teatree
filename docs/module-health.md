# Module health — the shrink ratchet, and extract-first

`check_module_health` caps how large a first-party Python module may grow, and holds
every module already over that cap to **shrink-only**. This page is the rule for working
inside one: **extract offsetting code to a sibling BEFORE you add, not after the commit
is refused.**

## What the gate measures

| Check | Cap | Counted as |
|---|---|---|
| Module length | 500 | non-blank, non-comment lines |
| Public module-level functions | 10 | `def name(...)` not starting with `_` |
| `dict[str, object]` annotations | 0 | on lines the change ADDED |

Scope is the first-party tree — `src/`, `hooks/` and `scripts/` — minus auto-generated
Django migrations, which legitimately capture a whole app's schema in one file.

Two entry paths share one predicate. The commit stage measures the **index blob** (the
version being committed) against `HEAD`; `--from-ref <base>` measures the **working
tree** across the PR's whole `base..HEAD` range, which is the bypass-proof CI twin.

## Grandfathering: over-cap modules may only shrink

A module already over a cap when the ratchet met it is not forced to split on the next
unrelated PR. It is **grandfathered and ratcheted**: it may hold steady or shrink, never
grow. So a god-module can never re-accrete after a split, and nobody inherits a whole
decomposition mid-task.

The consequence that surprises people: **the two lines a new gate needs are a
violation.**

```text
  hooks/scripts/hook_router.py: 3743 LOC, up from 3741 (over the 500 cap) — net +2.
  Over-cap files may only shrink — extract first: move at least 2 LOC of an
  in-concern helper out to a sibling module in this same change (docs/module-health.md).
```

There is no bypass. The refusal names the deficit precisely, so the extraction can be
sized before any code is written.

## The rule: extract first

Before adding a line to an over-cap module, move a cohesive helper OUT of it, so the
file nets smaller in the same change.

- **Pick a helper your change already touches.** Deleting unrelated lines to make room
  games the ratchet and churns code you did not come to change. The right extraction is
  one whose concern your ticket is already in.
- **Move the whole concern**, not a fragment — the helper, its private callees, and any
  constant only it reads. Whatever the origin module no longer uses is extra shrink for
  free; `ruff` F401 will name it.
- **Size it against the deficit.** The refusal's `net +N` is the floor, not the target.

## Mechanics that keep a move safe

- **Back-import the origin module's internals INSIDE the moved function**, never at
  module scope. They resolve at call time, so a test patching them on the origin still
  steers the moved code, and no import cycle forms:

  ```python
  def handle_track_active_repo(data: dict) -> None:
      from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 — call-time back-import
  ```

- **Keep a one-line re-export in the origin** so `origin.handle_x` still resolves for
  every registry entry and test that reaches it that way.
- **Alias both module identities** in a `hooks/scripts/` sibling, so the bare import the
  live hook uses and the `hooks.scripts.<name>` import a test uses are ONE module object:

  ```python
  sys.modules.setdefault("<name>", sys.modules[__name__])
  sys.modules.setdefault("hooks.scripts.<name>", sys.modules[__name__])
  ```

- **Lower the explicit ceiling** where one exists (`tests/test_hook_router_size_gate.py`
  pins the router's) to bank the win. Lowering it is the only sanctioned edit to that
  number.

## Seeing it coming

Two surfaces report the same predicate before a commit does.

```bash
uv run python scripts/hooks/check_module_health.py --report-debt             # every grandfathered module, advisory
uv run python scripts/hooks/check_module_health.py --from-ref origin/main    # the CI twin's verdict for this branch
```

A `PostToolUse` advisory (`hooks/scripts/over_cap_growth_advisory.py`) says the same
thing the moment a write grows an over-cap module, once per session per file. It can
never block — the enforcement stays with the commit ratchet; the advisory only moves the
discovery earlier, so the extraction is planned rather than paid as rework.
