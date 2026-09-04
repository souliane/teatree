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

Lowering it is welcome but not compulsory. Pairing the ceiling with a tightness
assertion (``slack <= 25``) meant any extraction bigger than 25 LOC went red
until the constant was hand-edited down in the same commit, so the gate charged
for the decomposition it exists to drive. Unbanked headroom is not a hole: the
router is over ``check_module_health``'s 500-LOC cap, so that gate holds it in
shrink-only mode against the merge base with no pin at all — it refuses any
growth whatever this ceiling reads. ``tests/quality/test_ratchet_direction.py``
pins the direction.
"""

import pathlib

import hooks.scripts.hook_router as router

_ROUTER = pathlib.Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"

# The router's non-comment / non-blank LOC ceiling. Shrink-only: only ever
# lowered, never raised. Measured the same way check_module_health._count_loc does.
# Lowered by U17, which extracted the classifier-denial handlers into the
# hooks/scripts/handlers/ per-domain package behind the routing table.
# Lowered by #81 step 1, which extracted the shared forge-API detection
# (effective-method + endpoint regexes/classifiers) into hooks/scripts/forge_api_detect.py.
# Lowered by #3343, which moved the tri-state cwd-managed classifier into the
# managed_repo sibling (cwd_teatree_managed_state).
# Lowered by #F7.9, which extracted the resolved-verdict router-side I/O into the
# quote_scanner_verdict_io sibling (the quote-gate fail-open NOTE stays in the router).
# Lowered again when the current-turn transcript projections (edited paths /
# assistant text) moved into the turn_inspect sibling.
# Lowered by #3810, which moved the SessionStart hand-off pickup into the
# session_handover_pickup sibling.
# Lowered by #3882, which moved the Slack self-DM destination helpers
# (tool-suffix + destination lookup) into the self_dm_destinations sibling.
# Lowered to the measured LOC of the vendored tree after the upstream sync,
# which carries both sides' extractions and so sits below either side's ceiling.
# Lowered by #4004, which moved gate 12's whole measurement chain (repo scope,
# argv, the shelled run and its fail-open branches) into the coverage_gate
# sibling, leaving the router the trigger and the deny.
# Lowered by #4673, which deleted the AskUserQuestion Slack transport (the config /
# post / DM-cache wrappers and the mirror leg) — delivery is now the drain's alone.
_CEILING_LOC = 3768


def _count_loc(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def over_ceiling(text: str, ceiling: int = _CEILING_LOC) -> bool:
    """True iff *text* carries MORE code lines than *ceiling* — the ratchet's whole verdict."""
    return _count_loc(text) > ceiling


def test_router_stays_at_or_below_the_shrink_only_ceiling() -> None:
    body = _ROUTER.read_text(encoding="utf-8")
    loc = _count_loc(body)
    assert not over_ceiling(body), (
        f"hook_router.py grew to {loc} LOC (ceiling {_CEILING_LOC}). The router is a "
        "shrink-only routing table: put a NEW handler in a bare sibling module "
        "(hooks/scripts/<concern>.py) and register it in _HANDLERS, never in the "
        "router body. See hooks/CLAUDE.md."
    )


def test_router_is_a_routing_table_over_registered_handlers() -> None:
    """The router dispatches through a ``_HANDLERS`` table, not ad-hoc per-event code."""
    assert isinstance(router._HANDLERS, dict)
    assert "PreToolUse" in router._HANDLERS
    assert all(callable(h) for handlers in router._HANDLERS.values() for h in handlers)
