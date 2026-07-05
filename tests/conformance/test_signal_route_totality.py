"""Signal-KIND ↔ dispatch/statusline route totality — the kind-level anti-drift lane (SELFCATCH-4).

``test_registry_parity.py`` LANE 1 governs the *zone* level (``AGENT_ZONES`` ↔
``_ZONE_HANDLERS``): a dispatch zone with no persistence executor. This lane
governs the *kind* level, the distinct gap that leaves: a scanner emitting a NEW
``ScanSignal.kind`` with no ``AGENT_BY_KIND`` / ``MECHANICAL_BY_KIND`` /
``STATUSLINE_ZONE_BY_KIND`` / drop entry is not caught by the zone lane — it
falls through :func:`teatree.loop.dispatch._dispatch_one` to the generic
``("statusline", "in_flight")`` fallback, a silent statusline drop into the wrong
zone.

Both directions, introspective (won't drift). Forward (kind → route): every
scanner-emitted kind is EXPLICITLY routed (a route/conditional/special-case),
explicitly dropped (a drop-kind / drop-prefix), or named in the
``INTENTIONAL_FALLBACK_KINDS`` allowlist (deliberate in_flight rendering). A NEW
kind that is none of these fails loud. Reverse (route → kind): every
``AGENT_BY_KIND`` / ``MECHANICAL_BY_KIND`` dispatch-route key is a kind some
scanner actually emits (as a literal, a resolved module constant, or an f-string
family), or is named in ``ROUTES_WITHOUT_STATIC_PRODUCER`` — a dispatch route for
a kind nothing produces is dead surface.
"""

import ast
from pathlib import Path

from teatree.loop.dispatch import _CONDITIONAL_HANDLERS
from teatree.loop.dispatch_tables import (
    AGENT_BY_KIND,
    MECHANICAL_BY_KIND,
    STATUSLINE_DROP_KINDS,
    STATUSLINE_DROP_PREFIXES,
    STATUSLINE_ZONE_BY_KIND,
)

_LOOP_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree" / "loop"

# Kinds that DELIBERATELY render through the generic ``("statusline", "in_flight")``
# fallback in ``_dispatch_one`` — low-priority bookkeeping / status observations
# that need no dedicated route. Named + reviewable (the SIG-4 allowlist idiom): a
# NEW emitted kind is NOT on this list, so it fails the forward walk until a
# conscious route-or-fallback decision is made.
INTENTIONAL_FALLBACK_KINDS: frozenset[str] = frozenset(
    {
        "deferred_question.mirrored",
        "eval_local.queued",
        "incoming_event.dead_letter",
        "notify.redelivered",
        "pr.approved",
        "team_pane.reaped",
        "waiting.digest",
    }
)

# Dispatch-route keys with no statically discoverable producer in ``src/teatree/loop``.
# ``skill_drift_detected`` (#1295 cap H) routes the ac-reviewing-codebase auto-fix
# sweep's per-finding signal to ``t3:coder``; the emitting sweep is documented on
# ``AssessFinding`` but is not wired as a static ``ScanSignal`` in this tree, so the
# route is pre-provisioned for it. Named so a reviewer sees the pre-provisioned
# route; a NEW unproduced route is NOT on this list and fails the reverse walk.
ROUTES_WITHOUT_STATIC_PRODUCER: frozenset[str] = frozenset({"skill_drift_detected"})


def _kind_argument(call: ast.Call) -> ast.expr | None:
    """The ``kind`` argument of a ``ScanSignal(...)`` call — ``kind=`` kwarg or first positional."""
    for keyword in call.keywords:
        if keyword.arg == "kind":
            return keyword.value
    return call.args[0] if call.args else None


def _string_assignments(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """Every ``NAME = <expr>`` binding in the module (module- and function-level).

    Resolves an indirect ``kind`` argument that is a ``Name`` — a module constant
    (``CLOSE_CANDIDATE_KIND``) or a local ``kind = f"self_update.{...}"`` — back to
    its value expression(s), so a dynamically-built kind is not invisible.
    """
    bindings: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.value)
    return bindings


def _resolve_kind(expr: ast.expr, bindings: dict[str, list[ast.expr]]) -> tuple[set[str], set[str]]:
    """Resolve a kind-argument expression to (literal kinds, family prefixes).

    ``Constant`` -> a literal kind. ``JoinedStr`` (f-string) -> its leading literal
    prefix (``f"pr_sweep.{x}"`` -> ``"pr_sweep."``). ``IfExp`` -> both branches.
    ``Name`` -> its binding(s). ``Attribute`` (``signal.kind`` re-emission) is a
    pass-through and contributes nothing.
    """
    literals: set[str] = set()
    prefixes: set[str] = set()
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        literals.add(expr.value)
    elif isinstance(expr, ast.JoinedStr):
        head = expr.values[0] if expr.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            prefixes.add(head.value)
    elif isinstance(expr, ast.IfExp):
        for branch in (expr.body, expr.orelse):
            branch_literals, branch_prefixes = _resolve_kind(branch, bindings)
            literals |= branch_literals
            prefixes |= branch_prefixes
    elif isinstance(expr, ast.Name):
        for value in bindings.get(expr.id, ()):
            value_literals, value_prefixes = _resolve_kind(value, bindings)
            literals |= value_literals
            prefixes |= value_prefixes
    return literals, prefixes


def emitted_signal_kinds() -> tuple[set[str], set[str]]:
    """Every ``ScanSignal`` kind produced under ``src/teatree/loop`` — (literals, prefixes).

    Introspection, not a hand-list: a new scanner emitting a new kind is discovered
    the moment it lands, so the totality assertion cannot silently lag it.
    """
    literals: set[str] = set()
    prefixes: set[str] = set()
    for path in _LOOP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bindings = _string_assignments(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "ScanSignal":
                continue
            argument = _kind_argument(node)
            if argument is None:
                continue
            call_literals, call_prefixes = _resolve_kind(argument, bindings)
            literals |= call_literals
            prefixes |= call_prefixes
    return literals, prefixes


#: Every kind the dispatcher routes or drops EXPLICITLY (never via the in_flight
#: fallback): the three routing tables, the drop-kinds, the payload-conditional
#: handlers, and the one payload-branch special case in ``_dispatch_one``.
_EXPLICITLY_ROUTED_KINDS: frozenset[str] = frozenset(
    set(AGENT_BY_KIND)
    | set(MECHANICAL_BY_KIND)
    | set(STATUSLINE_ZONE_BY_KIND)
    | set(STATUSLINE_DROP_KINDS)
    | set(_CONDITIONAL_HANDLERS)
    | {"ticket.disposition_candidate"}
)

#: The two generic DISPATCH route tables (agent + mechanical), whose keys must each
#: name a kind a scanner actually produces — the reverse (dead-route) direction.
_DISPATCH_ROUTE_KEYS: frozenset[str] = frozenset(set(AGENT_BY_KIND) | set(MECHANICAL_BY_KIND))


def _prefix_dropped(kind: str) -> bool:
    return any(kind.startswith(prefix) for prefix in STATUSLINE_DROP_PREFIXES)


def _produced(kind: str, literals: set[str], prefixes: set[str]) -> bool:
    return kind in literals or any(kind.startswith(prefix) for prefix in prefixes)


class TestEveryEmittedKindHasAnExplicitRoute:
    """Forward — a scanner-emitted kind is never a silent in_flight fallback.

    Every literal ``ScanSignal.kind`` must be explicitly routed, explicitly
    dropped, or an allowlisted deliberate fallback. A new emitted kind that
    resolves to the generic fallback fails here instead of shipping.
    """

    def test_no_emitted_kind_falls_through_to_the_generic_fallback(self) -> None:
        literals, _ = emitted_signal_kinds()
        uncovered = sorted(
            kind
            for kind in literals
            if kind not in _EXPLICITLY_ROUTED_KINDS
            and not _prefix_dropped(kind)
            and kind not in INTENTIONAL_FALLBACK_KINDS
        )
        assert not uncovered, (
            "scanner-emitted signal kind(s) with no explicit dispatch/statusline route — "
            "each falls through to the generic ('statusline', 'in_flight') fallback (a silent drop). "
            "Route them, drop them, or add to INTENTIONAL_FALLBACK_KINDS on purpose: " + str(uncovered)
        )

    def test_allowlisted_fallback_kinds_are_all_still_emitted(self) -> None:
        # A stale allowlist entry (a kind that no scanner emits anymore) is dead
        # surface — the allowlist must not outlive its kinds.
        literals, _ = emitted_signal_kinds()
        stale = sorted(INTENTIONAL_FALLBACK_KINDS - literals)
        assert not stale, f"INTENTIONAL_FALLBACK_KINDS entries no scanner emits (dead allowlist): {stale}"

    def test_no_allowlisted_fallback_kind_is_secretly_routed(self) -> None:
        # An allowlist entry that IS explicitly routed / dropped is a contradiction:
        # keep the allowlist minimal to the genuinely-fallback kinds.
        contradictory = sorted(
            kind for kind in INTENTIONAL_FALLBACK_KINDS if kind in _EXPLICITLY_ROUTED_KINDS or _prefix_dropped(kind)
        )
        assert not contradictory, (
            f"INTENTIONAL_FALLBACK_KINDS entries that are actually routed/dropped: {contradictory}"
        )


class TestEveryDispatchRouteHasAProducer:
    """Reverse — a dispatch route for a kind nothing emits is dead surface.

    Every ``AGENT_BY_KIND`` / ``MECHANICAL_BY_KIND`` key must name a kind a scanner
    produces (literal, resolved constant, or f-string family), or be an
    allowlisted pre-provisioned route.
    """

    def test_no_dispatch_route_key_is_a_dead_route(self) -> None:
        literals, prefixes = emitted_signal_kinds()
        dead = sorted(
            kind
            for kind in _DISPATCH_ROUTE_KEYS
            if not _produced(kind, literals, prefixes) and kind not in ROUTES_WITHOUT_STATIC_PRODUCER
        )
        assert not dead, (
            "dispatch-route key(s) with no scanner producing the kind (dead route) — "
            "wire a producer or add to ROUTES_WITHOUT_STATIC_PRODUCER on purpose: " + str(dead)
        )

    def test_pre_provisioned_routes_are_registered_routes(self) -> None:
        # A ROUTES_WITHOUT_STATIC_PRODUCER entry must still BE a live dispatch route;
        # otherwise the allowlist points at nothing.
        orphan = sorted(ROUTES_WITHOUT_STATIC_PRODUCER - _DISPATCH_ROUTE_KEYS)
        assert not orphan, f"ROUTES_WITHOUT_STATIC_PRODUCER entries that are not dispatch routes: {orphan}"


class TestSignalRouteTotalityCardinalityFloors:
    """Anti-vacuity — a broken enumerator that discovers nothing must not pass green."""

    def test_emitted_kind_floor(self) -> None:
        literals, prefixes = emitted_signal_kinds()
        assert len(literals) >= 50, sorted(literals)
        assert len(prefixes) >= 3, sorted(prefixes)

    def test_route_table_floor(self) -> None:
        assert len(_EXPLICITLY_ROUTED_KINDS) >= 30, sorted(_EXPLICITLY_ROUTED_KINDS)
        assert len(_DISPATCH_ROUTE_KEYS) >= 10, sorted(_DISPATCH_ROUTE_KEYS)


class TestSignalRouteTotalityFiresRed:
    """Anti-vacuity — the lane must actually catch a new-kind silent drop and a dead route."""

    def test_a_synthetic_unrouted_kind_is_reported(self) -> None:
        # A synthetic kind that no table routes, no prefix drops, and no allowlist
        # names is exactly the silent-drop class — the forward predicate flags it.
        synthetic = "synthetic_scanner.brandnew_unrouted_kind"
        assert synthetic not in _EXPLICITLY_ROUTED_KINDS
        assert not _prefix_dropped(synthetic)
        assert synthetic not in INTENTIONAL_FALLBACK_KINDS

    def test_a_synthetic_dead_route_is_reported(self) -> None:
        # A route key produced by no scanner and not pre-provisioned is a dead route.
        literals, prefixes = emitted_signal_kinds()
        synthetic = "synthetic_route.no_producer"
        assert not _produced(synthetic, literals, prefixes)
        assert synthetic not in ROUTES_WITHOUT_STATIC_PRODUCER

    def test_a_resolved_family_prefix_covers_its_dynamic_kinds(self) -> None:
        # The reverse walk must not false-positive on dynamically-built kinds: the
        # ``pr_sweep.`` f-string family means ``pr_sweep.flag_no_review`` is produced.
        literals, prefixes = emitted_signal_kinds()
        assert _produced("pr_sweep.flag_no_review", literals, prefixes)
        assert _produced("issue_disposition.close_candidate", literals, prefixes)
