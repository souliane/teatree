"""Budget gate unit tests (no DB needed for the pure-logic surface)."""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.admission_pressure import MachineSignal
from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve import budget as budget_module
from teatree.loop.self_improve.budget import (
    DEFAULT_SPAWN_CAP,
    BudgetVerdict,
    precheck_budget,
    recent_self_improve_firings,
)

_HEALTHY_MACHINE = MachineSignal(cores=8, load1=1.0, ram_available_gb=8.0)
_LOW_MACHINE = MachineSignal(cores=8, load1=1.0, ram_available_gb=2.0)


@pytest.fixture(autouse=True)
def _healthy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget_module, "read_disk_used_percent", lambda: 20.0)


def test_allow_when_all_under_limits() -> None:
    verdict = precheck_budget(machine=_HEALTHY_MACHINE, recent_self_improve_spawns=0)
    assert verdict.ok is True
    assert verdict.reason == ""


def test_low_ram_short_circuits() -> None:
    verdict = precheck_budget(machine=_LOW_MACHINE)
    assert verdict.ok is False
    assert "memory_pressure" in verdict.reason


def test_self_improve_uses_the_governor_memory_floor_and_names_unknown() -> None:
    floor = MachineSignal(cores=8, load1=1.0, ram_available_gb=4.0, memory_source="container reading")
    unreadable = MachineSignal(cores=8, load1=1.0, ram_available_gb=None, memory_source="container reading")
    assert "4 GB floor" in precheck_budget(machine=floor).reason
    verdict = precheck_budget(machine=unreadable)
    assert not verdict.ok
    assert "unknown; container reading" in verdict.reason


def test_low_disk_skips_a_self_improve_cycle() -> None:
    verdict = precheck_budget(machine=_HEALTHY_MACHINE, disk_used_percent=96)
    assert verdict.ok is False
    assert "low_disk" in verdict.reason


def test_unmeasurable_disk_does_not_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget_module, "read_disk_used_percent", lambda: None)
    assert precheck_budget(machine=_HEALTHY_MACHINE).ok


def test_spawn_cap_blocks_at_the_cap() -> None:
    # A cap of N admits N spawns, so the N-th sample is already spent.
    verdict = precheck_budget(machine=_HEALTHY_MACHINE, recent_self_improve_spawns=DEFAULT_SPAWN_CAP)
    assert verdict.ok is False
    assert "spawn_cap" in verdict.reason


def test_one_spawn_below_the_cap_still_allows() -> None:
    verdict = precheck_budget(machine=_HEALTHY_MACHINE, recent_self_improve_spawns=DEFAULT_SPAWN_CAP - 1)
    assert verdict.ok is True


def test_token_budget_exhausted_blocks() -> None:
    verdict = precheck_budget(
        machine=_HEALTHY_MACHINE,
        token_budget_remaining=0,
    )
    assert verdict.ok is False
    assert "token_budget_exhausted" in verdict.reason


def test_token_budget_none_allows() -> None:
    verdict = precheck_budget(machine=_HEALTHY_MACHINE, token_budget_remaining=None)
    assert verdict.ok is True


def test_machine_probe_callable_is_consulted() -> None:
    verdict = precheck_budget(machine_probe=lambda: _LOW_MACHINE)
    assert verdict.ok is False
    assert "memory_pressure" in verdict.reason


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
