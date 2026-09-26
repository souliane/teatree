"""Budget gate unit tests (no DB needed for the pure-logic surface)."""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve import budget as budget_module
from teatree.loop.self_improve.budget import (
    DEFAULT_SPAWN_CAP,
    DEFAULT_TOKEN_BUDGET_ENV,
    BudgetVerdict,
    precheck_budget,
    recent_self_improve_firings,
    token_budget_from_env,
)


@pytest.fixture(autouse=True)
def _healthy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget_module, "read_disk_used_percent", lambda: 20.0)


def test_allow_when_all_under_limits() -> None:
    verdict = precheck_budget(ram_used_percent=50, recent_self_improve_spawns=0)
    assert verdict.ok is True
    assert verdict.reason == ""


def test_low_ram_short_circuits() -> None:
    verdict = precheck_budget(ram_used_percent=95)
    assert verdict.ok is False
    assert "low_ram" in verdict.reason


def test_low_disk_skips_a_self_improve_cycle() -> None:
    verdict = precheck_budget(ram_used_percent=20, disk_used_percent=96)
    assert verdict.ok is False
    assert "low_disk" in verdict.reason


def test_unmeasurable_disk_does_not_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget_module, "read_disk_used_percent", lambda: None)
    assert precheck_budget(ram_used_percent=20).ok


def test_spawn_cap_blocks_at_the_cap() -> None:
    # A cap of N admits N spawns, so the N-th sample is already spent.
    verdict = precheck_budget(ram_used_percent=10, recent_self_improve_spawns=DEFAULT_SPAWN_CAP)
    assert verdict.ok is False
    assert "spawn_cap" in verdict.reason


def test_one_spawn_below_the_cap_still_allows() -> None:
    verdict = precheck_budget(ram_used_percent=10, recent_self_improve_spawns=DEFAULT_SPAWN_CAP - 1)
    assert verdict.ok is True


def test_token_budget_exhausted_blocks() -> None:
    verdict = precheck_budget(
        ram_used_percent=10,
        token_budget_remaining=0,
    )
    assert verdict.ok is False
    assert "token_budget_exhausted" in verdict.reason


def test_token_budget_none_allows() -> None:
    verdict = precheck_budget(ram_used_percent=10, token_budget_remaining=None)
    assert verdict.ok is True


def test_token_budget_from_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_TOKEN_BUDGET_ENV, raising=False)
    assert token_budget_from_env() is None


def test_token_budget_from_env_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_BUDGET_ENV, "5000")
    assert token_budget_from_env() == 5000


def test_token_budget_from_env_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_BUDGET_ENV, "not-a-number")
    assert token_budget_from_env() is None


def test_ram_probe_callable_is_consulted() -> None:
    """When ``ram_used_percent`` is None, the probe callable is used."""
    verdict = precheck_budget(ram_probe=lambda: 95.0)
    assert verdict.ok is False
    assert "low_ram" in verdict.reason


def test_budget_verdict_helpers() -> None:
    assert BudgetVerdict.skip("foo") == BudgetVerdict(ok=False, reason="foo")
    assert BudgetVerdict.allow() == BudgetVerdict(ok=True, reason="")


class RecentFiringsTests(TestCase):
    def test_counts_only_within_window(self) -> None:
        old = timezone.now() - dt.timedelta(hours=2)
        recent = timezone.now() - dt.timedelta(minutes=10)
        SelfImproveFiring.objects.create(
            detector="d",
            dedup_key="k1",
            state_hash="h",
            severity="info",
            last_fired_at=old,
        )
        SelfImproveFiring.objects.create(
            detector="d",
            dedup_key="k2",
            state_hash="h",
            severity="info",
            last_fired_at=recent,
        )
        assert recent_self_improve_firings(seconds=30 * 60) == 1
