"""Explicit shrink-only size gate on the router file (#2384 PR-09).

``hooks/scripts/hook_router.py`` is a god-module being decomposed into per-domain
handler siblings behind a thin routing table (``_HANDLERS``). The generic
module-health ratchet (``scripts/hooks/check_module_health.py``) already refuses a
commit that GROWS the over-cap router, but that gate lives in a pre-commit / CI
script. This test makes the shrink-only contract a VISIBLE, discoverable
regression pin: the router may only shrink, a new handler goes in a bare sibling
module (see ``hooks/CLAUDE.md``), never in the router.

Contract: when you legitimately shrink the router further, LOWER ``_CEILING_LOC``
to lock the win in — that is the only sanctioned edit to this number. A rising
LOC means a concern was added to the router that belongs in a sibling.
"""

import pathlib

import hooks.scripts.hook_router as router

_ROUTER = pathlib.Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"

# The router's non-comment / non-blank LOC ceiling. Shrink-only: only ever
# lowered, never raised. Measured the same way check_module_health._count_loc does.
# Lowered by PR-28 c3, which removed the loop-registration nudge gate + helpers.
_CEILING_LOC = 4698


def _count_loc(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def test_router_stays_at_or_below_the_shrink_only_ceiling() -> None:
    loc = _count_loc(_ROUTER.read_text(encoding="utf-8"))
    assert loc <= _CEILING_LOC, (
        f"hook_router.py grew to {loc} LOC (ceiling {_CEILING_LOC}). The router is a "
        "shrink-only routing table: put a NEW handler in a bare sibling module "
        "(hooks/scripts/<concern>.py) and register it in _HANDLERS, never in the "
        "router body. See hooks/CLAUDE.md."
    )


def test_ceiling_is_kept_tight_so_the_gate_has_teeth() -> None:
    """The ceiling must track the actual LOC — a slack ceiling could hide re-accretion."""
    loc = _count_loc(_ROUTER.read_text(encoding="utf-8"))
    slack = _CEILING_LOC - loc
    assert slack >= 0, "ceiling below actual LOC — raise is forbidden; this means the gate already fired"
    assert slack <= 25, (
        f"_CEILING_LOC ({_CEILING_LOC}) is {slack} LOC above the actual router size ({loc}). "
        "Lower _CEILING_LOC to the current size so a re-accreted handler cannot hide under the slack."
    )


def test_router_is_a_routing_table_over_registered_handlers() -> None:
    """The router dispatches through a ``_HANDLERS`` table, not ad-hoc per-event code."""
    assert isinstance(router._HANDLERS, dict)
    assert "PreToolUse" in router._HANDLERS
    assert all(callable(h) for handlers in router._HANDLERS.values() for h in handlers)
