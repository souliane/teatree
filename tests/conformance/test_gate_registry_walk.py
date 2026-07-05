"""Gate/resolver registry ↔ call-site bidirectional walk — the registered-but-uncalled lane (SELFCATCH-4).

``teatree.core.model_registries`` inverts an intra-``core`` up-edge: a gate module
registers its callable via ``register_gate("<name>", fn)`` at app-ready time, and
:class:`teatree.core.models.ticket.Ticket` fetches it at call time via
``get_gate("<name>")``. The name is the seam — and a bare string on both sides, so
the two can drift silently: a registered gate that no FSM transition calls is dead
authority, and a ``get_gate("typo")`` call site for a name nothing registered
raises only when that transition executes (a runtime ``KeyError``, not a CI
failure). ``test_model_registries.py`` guards the round-trip mechanics and
hand-enumerates six known gates; this lane makes the parity TOTAL and
introspective — an AST walk of ``src/teatree`` in both directions, so a NEW
registered gate with no call site (or a NEW call site for an unregistered name)
fails the PR that introduces it.

Also covers the resolver registry (``register_resolver`` ↔ ``get_resolver``), the
sibling seam sharing the same drift shape.
"""

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

from teatree.core.model_registries import populate_model_registries
from teatree.core.modelkit import gate_registry

_SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree"

# The register/get callable pairs the walk covers, keyed by the runtime resolver
# used for the liveness cross-check. A NEW registry seam of this shape enrolls
# here (and, via the roster meta-ratchet, must); the pair is walked both ways.
_REGISTRY_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("register_gate", "get_gate", "gate"),
    ("register_resolver", "get_resolver", "resolver"),
)


def _first_string_arg(call: ast.Call) -> str | None:
    """The first positional argument of *call* when it is a string literal, else ``None``.

    A non-literal first argument (``get_gate(gate)`` where ``gate`` is a variable)
    is a dynamic target the static walk cannot resolve — skipped, not a phantom.
    """
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _literal_call_names(func_name: str) -> set[str]:
    """Every string-literal first arg passed to a call of ``func_name`` under ``src/teatree``.

    Introspection over the source tree: ``register_gate("x", fn)`` and
    ``get_gate("x")`` both surface here so neither side of the seam can be
    hand-listed out of sync with the code.
    """
    names: set[str] = set()
    for path in _SRC_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called != func_name:
                continue
            literal = _first_string_arg(node)
            if literal is not None:
                names.add(literal)
    return names


def _assert_covers(*, producers: Iterable[str], consumers: Iterable[str], label: str) -> None:
    uncovered = sorted(set(producers) - set(consumers))
    assert not uncovered, f"{label}: {uncovered}"


class TestGateRegistryBidirectionalWalk:
    """Every registered gate/resolver has a call site, and every call site is registered."""

    @pytest.mark.parametrize(("register_fn", "get_fn", "kind"), _REGISTRY_PAIRS)
    def test_every_registration_has_a_call_site(self, register_fn: str, get_fn: str, kind: str) -> None:
        _assert_covers(
            producers=_literal_call_names(register_fn),
            consumers=_literal_call_names(get_fn),
            label=f"registered {kind}(s) with no {get_fn}(...) call site (dead {kind})",
        )

    @pytest.mark.parametrize(("register_fn", "get_fn", "kind"), _REGISTRY_PAIRS)
    def test_every_call_site_references_a_registration(self, register_fn: str, get_fn: str, kind: str) -> None:
        _assert_covers(
            producers=_literal_call_names(get_fn),
            consumers=_literal_call_names(register_fn),
            label=f"{get_fn}(...) call site(s) for a {kind} nothing {register_fn}s (runtime KeyError waiting to fire)",
        )

    def test_every_registered_gate_resolves_at_runtime(self) -> None:
        # Ties the static walk to runtime reality: each statically-registered gate
        # name must resolve through the LIVE registry after population — an
        # ``@transition`` calling ``get_gate("x")`` would otherwise KeyError.
        populate_model_registries()
        for name in _literal_call_names("register_gate"):
            assert gate_registry.get_gate(name) is not None, f"registered gate {name!r} does not resolve at runtime"

    def test_every_registered_resolver_resolves_at_runtime(self) -> None:
        populate_model_registries()
        for name in _literal_call_names("register_resolver"):
            assert gate_registry.get_resolver(name) is not None, f"registered resolver {name!r} does not resolve"


class TestGateRegistryWalkCardinalityFloors:
    """Anti-vacuity — a broken AST walk that finds nothing must not pass green."""

    def test_gate_floor(self) -> None:
        registered = _literal_call_names("register_gate")
        called = _literal_call_names("get_gate")
        assert len(registered) >= 8, sorted(registered)
        assert len(called) >= 8, sorted(called)

    def test_resolver_floor(self) -> None:
        assert len(_literal_call_names("register_resolver")) >= 2
        assert len(_literal_call_names("get_resolver")) >= 2


class TestGateRegistryWalkFiresRed:
    """Anti-vacuity — the walk must actually name a registered-uncalled gate and a called-unregistered name."""

    def test_a_registered_gate_with_no_call_site_is_named(self) -> None:
        with pytest.raises(AssertionError, match="synthetic_registered_only"):
            _assert_covers(
                producers={"plan_artifact", "synthetic_registered_only"},
                consumers={"plan_artifact"},
                label="registered gate(s) with no call site",
            )

    def test_a_call_site_for_an_unregistered_gate_is_named(self) -> None:
        with pytest.raises(AssertionError, match="synthetic_called_only"):
            _assert_covers(
                producers={"plan_artifact", "synthetic_called_only"},
                consumers={"plan_artifact"},
                label="get_gate(...) call site(s) for an unregistered gate",
            )

    def test_a_dynamic_call_target_is_not_a_phantom(self) -> None:
        # ``get_gate(gate)`` with a variable arg must NOT be mistaken for a literal
        # call site — the reverse walk would false-positive on it otherwise.
        dynamic = ast.parse("get_gate(gate)").body[0].value
        assert isinstance(dynamic, ast.Call)
        assert _first_string_arg(dynamic) is None
