"""The eval lane's resource caps resolve generous defaults and honour their env overrides.

Each cap is deliberately generous: a truncated run measures the cap rather than
the behaviour under test. These pin that a bad override never silently TIGHTENS
a cap — an unparsable or non-positive value falls back to the generous default
rather than to zero.
"""

import pytest

from teatree.eval.resource_caps import (
    DEFAULT_WATCHDOG_SECONDS,
    METERED_DEFAULT_BUDGET_USD,
    METERED_DEFAULT_EFFORT,
    env_float,
    resolve_max_turns_override,
    resolve_metered_budget_usd,
    resolve_metered_effort,
    resolve_watchdog_seconds,
)


class TestWatchdogSeconds:
    def test_resolves_the_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_WATCHDOG_SECONDS", "450")
        assert resolve_watchdog_seconds() == pytest.approx(450.0)

    def test_falls_back_to_the_generous_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_EVAL_WATCHDOG_SECONDS", raising=False)
        assert resolve_watchdog_seconds() == pytest.approx(float(DEFAULT_WATCHDOG_SECONDS))

    def test_default_is_generous_enough_for_a_delegating_scenario(self) -> None:
        # 120s was too tight — a scenario that spawns sub-agents timed out before
        # finishing, which reads as a behaviour failure but is really a cap.
        assert DEFAULT_WATCHDOG_SECONDS >= 300


class TestMaxTurnsOverride:
    def test_resolves_the_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_MAX_TURNS", "50")
        assert resolve_max_turns_override() == 50

    def test_defers_to_per_scenario_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_EVAL_MAX_TURNS", raising=False)
        assert resolve_max_turns_override() is None

    def test_ignores_a_non_positive_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_MAX_TURNS", "0")
        assert resolve_max_turns_override() is None

    def test_ignores_an_unparsable_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_MAX_TURNS", "lots")
        assert resolve_max_turns_override() is None

    def test_prefers_an_explicit_value_over_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_MAX_TURNS", "9")
        assert resolve_max_turns_override(explicit=4) == 4


class TestMeteredBudget:
    def test_resolves_the_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_MAX_BUDGET_USD", "2.5")
        assert resolve_metered_budget_usd() == pytest.approx(2.5)

    def test_falls_back_to_the_generous_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_EVAL_MAX_BUDGET_USD", raising=False)
        assert resolve_metered_budget_usd() == pytest.approx(METERED_DEFAULT_BUDGET_USD)


class TestMeteredEffort:
    def test_resolves_a_valid_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_EFFORT", "low")
        assert resolve_metered_effort() == "low"

    def test_an_unknown_level_falls_back_rather_than_reaching_the_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_EFFORT", "turbo")
        assert resolve_metered_effort() == METERED_DEFAULT_EFFORT


class TestEnvFloat:
    """A fat-fingered override must never silently tighten a cap to an accidental 0."""

    @pytest.mark.parametrize("raw", ["", "   ", "not-a-number", "0", "-5"])
    def test_a_bad_value_yields_the_default(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("T3_CAP_UNDER_TEST", raw)
        assert env_float("T3_CAP_UNDER_TEST", default=7.5) == pytest.approx(7.5)

    def test_an_absent_var_yields_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_CAP_UNDER_TEST", raising=False)
        assert env_float("T3_CAP_UNDER_TEST", default=7.5) == pytest.approx(7.5)

    def test_a_positive_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CAP_UNDER_TEST", "12.25")
        assert env_float("T3_CAP_UNDER_TEST", default=7.5) == pytest.approx(12.25)
