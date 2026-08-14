# test-path: cross-cutting — drives hooks/scripts/hook_budget.py against hooks/hooks.json; no src/teatree/ mirror.
"""The shared PreToolUse budget every in-hook subprocess draws from (#4305).

``coverage_gate``'s 30s measurement ran ahead of ``existing_artifact``'s 15s
probe inside a 30s hook ceiling — 45s of fixed timeouts in a 30s window, with
nothing accounting for the budget already spent. An overrun does not truncate
the probe; the harness cancels the hook, so no ``permissionDecision`` is emitted
at all and the guarded ``gh pr create`` proceeds. These pin the arithmetic that
makes the sum fit, and pin the ceiling constant to the ``hooks.json`` it mirrors.
"""

import json
from pathlib import Path

import pytest

from hooks.scripts.hook_budget import HOOK_CEILING_S, bounded_timeout_s

_HOOKS_JSON = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"


class TestBoundedTimeout:
    """A timeout is a request against the remaining ceiling, never an entitlement."""

    def test_ample_budget_grants_the_preferred_timeout(self) -> None:
        assert bounded_timeout_s(5.0, elapsed=0.0) == pytest.approx(5.0)

    def test_a_spent_budget_shrinks_the_timeout_to_the_remainder(self) -> None:
        # 25s gone of a 30s ceiling: the probe gets what is left minus the emit
        # reserve, not the 15s it asked for.
        granted = bounded_timeout_s(15.0, elapsed=25.0)
        assert granted is not None
        assert granted < 15.0
        assert 25.0 + granted < HOOK_CEILING_S

    def test_an_exhausted_budget_grants_nothing(self) -> None:
        assert bounded_timeout_s(15.0, elapsed=float(HOOK_CEILING_S)) is None

    def test_the_reserve_is_withheld_before_the_ceiling_is_reached(self) -> None:
        # Not merely `elapsed < ceiling`: the handler still has to write its
        # decision after the last subprocess returns.
        assert bounded_timeout_s(15.0, elapsed=HOOK_CEILING_S - 0.5) is None

    def test_a_backwards_clock_reading_is_treated_as_no_time_spent(self) -> None:
        assert bounded_timeout_s(5.0, elapsed=-100.0) == pytest.approx(5.0)

    def test_the_gate_12_pair_of_timeouts_fits_inside_the_ceiling(self) -> None:
        # The #4305 invariant, stated over the two real call sites: whatever the
        # measurement is granted, the probe that follows it cannot push the pair
        # past the ceiling.
        measurement = bounded_timeout_s(30.0, elapsed=0.0)
        assert measurement is not None
        probe = bounded_timeout_s(15.0, elapsed=measurement)
        assert measurement + (probe or 0.0) <= HOOK_CEILING_S


class TestCeilingMirrorsHooksJson:
    """``HOOK_CEILING_S`` is a mirror; a hooks.json edit must not silently outrun it."""

    def test_ceiling_matches_every_declared_pretooluse_timeout(self) -> None:
        matchers = json.loads(_HOOKS_JSON.read_text())["hooks"]["PreToolUse"]
        declared = {hook.get("timeout") for entry in matchers for hook in entry.get("hooks", [])}
        assert declared == {HOOK_CEILING_S}
