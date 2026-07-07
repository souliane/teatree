"""Conformance test § 5.1: registered overlays override every falsy-default hook.

The forensic provisioning root-cause analysis
(``docs/provisioning-rootcause-2026-05-27.md``) identifies Pattern A: an
overlay can forget ``get_required_ports`` /
``provisioning.db_import_strategy`` / ``runtime.readiness_probes`` and the lifecycle
reports green. The default returns falsy, the FSM never checks, and the
runtime silently degrades.

This test is the falsification experiment for that pattern. If it passes
on ``main`` without code changes → paradigm-mismatch overstated. If RED →
the Pydantic ``OverlayConfig`` move (paradigm issue) is the exit, because
"override every hook" is exactly the contract that fail-closed defaults
make ungrammatical to violate.
"""

import inspect

import pytest

from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_all_overlays

_FALSY_RETURN_LITERALS = {
    "return []",
    "return {}",
    "return set()",
    "return False",
    "return None",
    'return ""',
}


def _hook_names_with_falsy_default() -> list[str]:
    """Return the ``OverlayBase`` hook names whose default body returns falsy."""
    hooks: list[str] = []
    for name, member in inspect.getmembers(OverlayBase, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if not name.startswith(("get_", "uses_", "declared_")):
            continue
        try:
            source = inspect.getsource(member)
        except (OSError, TypeError):
            continue
        if any(literal in source for literal in _FALSY_RETURN_LITERALS):
            hooks.append(name)
    return sorted(hooks)


def _is_overridden(overlay: OverlayBase, hook_name: str) -> bool:
    base_method = getattr(OverlayBase, hook_name, None)
    overlay_method = getattr(type(overlay), hook_name, None)
    if base_method is None or overlay_method is None:
        return False
    return overlay_method is not base_method


@pytest.fixture(scope="module")
def registered_overlays() -> dict[str, OverlayBase]:
    return get_all_overlays()


@pytest.fixture(scope="module")
def falsy_default_hooks() -> list[str]:
    hooks = _hook_names_with_falsy_default()
    assert hooks, "Expected at least one falsy-default hook on OverlayBase"
    return hooks


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Documents the open paradigm-mismatch claim (#1385): the contract today "
        "permits falsy-default hooks to remain inherited. Goes RED on main on "
        "purpose — XPASS once #1385 lands will force this marker off."
    ),
)
def test_every_overlay_overrides_every_falsy_default_hook(
    registered_overlays: dict[str, OverlayBase],
    falsy_default_hooks: list[str],
) -> None:
    """Falsify Pattern A: a falsy-default hook left at the base is a silent no-op.

    Today the bundled ``t3_teatree`` overlay (and every third-party overlay
    registered via the ``teatree.overlays`` entry point) inherits most of
    these hooks at their falsy default. This test goes RED on ``main`` —
    that is the proof that the contract cannot hold the invariants the
    individual fix PRs encode.

    Marked ``xfail(strict=True)``: it WILL fail until paradigm issue
    [#1385](https://github.com/souliane/teatree/issues/1385) lands the
    Pydantic ``OverlayConfig`` move. If it ever passes (XPASS), strict mode
    fails the suite — forcing this marker to be removed so the test
    becomes a real green gate.
    """
    assert registered_overlays, "no overlays registered — cannot validate the contract"

    violations = [
        f"{overlay_name}.{hook}"
        for overlay_name, overlay in sorted(registered_overlays.items())
        for hook in falsy_default_hooks
        if not _is_overridden(overlay, hook)
    ]

    assert not violations, (
        "Overlays inherit falsy-default hooks (Pattern A silent degradation):\n  "
        + "\n  ".join(violations)
        + "\n\nEach listed (overlay, hook) pair is a silent no-op at runtime. "
        "See docs/provisioning-rootcause-2026-05-27.md § 2 Pattern A and § 3.1."
    )
