"""Dormant-venv eviction — the lossless reclaim, and the guard that keeps it lossless (#4244).

Real ``git`` checkouts under ``tmp_path``, because the population comes from the
filesystem scan and a mocked one would prove nothing about whether an untracked
ad-hoc checkout is covered (roughly half the reclaimable venvs on the box that
produced this issue sit in checkouts no ledger knows about).

The load-bearing case is the one that must NOT reclaim: a venv is not written to
while it is being imported from, so an in-use one reads as idle by mtime. Every
keep-case here is therefore driven through the process table, and the pass is
refused outright when that table cannot answer.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup import process_table
from teatree.core.cleanup.checkout_registry import CheckoutRegistry
from teatree.core.cleanup.reclaim_pressure import effective_idle_days
from teatree.core.cleanup.venv_eviction import evict_venvs, plan_venv_eviction
from tests._git_repo import make_git_repo
from tests._process_table_venue import blinded_process_table

_REGISTRY = "teatree.core.cleanup.checkout_registry"
_LONG_AGO = 1_600_000_000  # comfortably beyond any idle threshold under test

# The shipped thresholds, so the escalation tests exercise the real decay.
_WARN_GB = 25.0
_CRIT_GB = 10.0
_IDLE_DAYS = 2.0


def _criterion(free_gb: float) -> float | None:
    return effective_idle_days(free_gb=free_gb, warn_gb=_WARN_GB, crit_gb=_CRIT_GB, idle_days=_IDLE_DAYS)


def _venv_in(checkout: Path, *, name: str = ".venv", dormant: bool = True, size: int = 4096) -> Path:
    venv = checkout / name
    (venv / "lib").mkdir(parents=True)
    (venv / "lib" / "big.so").write_bytes(b"x" * size)
    if dormant:
        os.utime(venv, (_LONG_AGO, _LONG_AGO))
        os.utime(checkout, (_LONG_AGO, _LONG_AGO))
    return venv


def _touched_an_hour_ago(checkout: Path, venv: Path) -> None:
    """What a directory some other process rebuilds every few hours looks like."""
    stamp = timezone.now().timestamp() - 3600
    os.utime(venv, (stamp, stamp))
    os.utime(checkout, (stamp, stamp))


class _EvictionFixture(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.workspace = tmp_path
        self.host_proc = tmp_path / "host-proc"
        self.host_proc.mkdir()
        self.enterContext(patch.object(process_table, "_HOST_PROC_ROOT", self.host_proc))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(tmp_path,)))
        self.enterContext(patch(f"{_REGISTRY}.Path.cwd", return_value=tmp_path / "nowhere"))

    def _process_working_in(self, directory: Path, *, pid: str = "4242") -> None:
        (self.host_proc / pid).mkdir(parents=True)
        (self.host_proc / pid / "cwd").symlink_to(directory)

    def _some_process_exists(self) -> None:
        """A readable table with a process placed nowhere near any checkout."""
        self._process_working_in(self.workspace / "unrelated", pid="1")


class TestEviction(_EvictionFixture):
    def test_a_dormant_venv_is_evicted_and_its_checkout_survives(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        venv = _venv_in(checkout)
        self._some_process_exists()

        plan = plan_venv_eviction(self.workspace, idle_days=1)
        outcome = evict_venvs(plan)

        assert not venv.exists()
        assert (checkout / ".git").exists(), "the checkout holds the work; only the cache goes"
        assert outcome.freed_bytes > 0
        assert plan.considered == 1

    def test_an_untracked_checkout_is_covered(self) -> None:
        """No ``Worktree`` row exists — a ledger-keyed reaper walks straight past these."""
        adhoc = make_git_repo(self.workspace / "wt-adhoc")
        venv = _venv_in(adhoc)
        self._some_process_exists()

        evict_venvs(plan_venv_eviction(self.workspace, idle_days=1))

        assert not venv.exists()

    def test_both_venv_flavours_are_covered(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        venv = _venv_in(checkout)
        hook_venv = _venv_in(checkout, name=".venv-hook")
        self._some_process_exists()

        plan = plan_venv_eviction(self.workspace, idle_days=1)
        evict_venvs(plan)

        assert plan.considered == 2
        assert not venv.exists()
        assert not hook_venv.exists()


class TestPressureEscalation(_EvictionFixture):
    """Age stops gating as the disk fills; liveness never does (#4644)."""

    def test_a_fresh_venv_is_evicted_below_the_critical_floor(self) -> None:
        checkout = make_git_repo(self.workspace / "fresh")
        venv = _venv_in(checkout, dormant=False)
        self._some_process_exists()

        plan = plan_venv_eviction(self.workspace, idle_days=_criterion(0.2))
        outcome = evict_venvs(plan)

        assert not venv.exists(), "below the floor a rebuildable cache is not worth an age test"
        assert outcome.freed_bytes > 0
        assert (checkout / ".git").exists(), "the checkout holds the work; only the cache goes"

    def test_liveness_keeps_a_held_venv_at_critical_while_its_free_sibling_goes(self) -> None:
        held = make_git_repo(self.workspace / "held")
        free = make_git_repo(self.workspace / "free")
        _venv_in(held, dormant=False)
        _venv_in(free, dormant=False)
        self._process_working_in(held / "src")

        plan = plan_venv_eviction(self.workspace, idle_days=_criterion(0.2))
        evict_venvs(plan)

        assert (held / ".venv").exists(), "liveness is the sole guard once age stops gating"
        assert not (free / ".venv").exists(), "and that guard is only meaningful if the pass reaches it"
        assert any("a live process is working inside the checkout" in line for line in plan.kept)

    def test_liveness_refuses_at_every_pressure_level(self) -> None:
        held = make_git_repo(self.workspace / "held")
        venv = _venv_in(held, dormant=False)
        self._process_working_in(held / "src")

        for free_gb in (0.2, 17.5, 200.0):
            with self.subTest(free_gb=free_gb):
                evict_venvs(plan_venv_eviction(self.workspace, idle_days=_criterion(free_gb)))
                assert venv.exists()

    def test_an_hourly_rewritten_checkout_does_not_become_permanently_ineligible(self) -> None:
        """Churn faster than ``venv_idle_days`` used to confer immunity on every pass, forever."""
        churned = make_git_repo(self.workspace / "churned")
        venv = _venv_in(churned, dormant=False)
        _touched_an_hour_ago(churned, venv)
        self._some_process_exists()

        plan = plan_venv_eviction(self.workspace, idle_days=_criterion(0.2))
        evict_venvs(plan)

        assert not venv.exists()
        assert not any("too recently" in line for line in plan.kept)


class TestGuards(_EvictionFixture):
    def test_a_checkout_with_a_live_process_keeps_its_venv(self) -> None:
        """THE guard. The venv reads as idle by mtime — only the process says otherwise."""
        checkout = make_git_repo(self.workspace / "live")
        venv = _venv_in(checkout)
        self._process_working_in(checkout / "src")

        plan = plan_venv_eviction(self.workspace, idle_days=1)
        evict_venvs(plan)

        assert venv.exists(), "a venv under a live agent must never be evicted"
        assert any("live process" in kept for kept in plan.kept)

    def test_an_unreadable_process_table_refuses_the_whole_pass(self) -> None:
        """Fail CLOSED: absence of a live process is the whole authority to delete."""
        checkout = make_git_repo(self.workspace / "clone")
        venv = _venv_in(checkout)

        marker = self.workspace / ".dockerenv"
        marker.touch()
        with patch.object(process_table, "_CONTAINER_MARKERS", (marker,)):
            plan = plan_venv_eviction(self.workspace, idle_days=1)
            evict_venvs(plan)

        assert plan.refusal, "a pass that cannot see the processes must say so, not return no candidates"
        assert plan.candidates == ()
        assert venv.exists()

    def test_a_recently_touched_venv_is_kept(self) -> None:
        checkout = make_git_repo(self.workspace / "fresh")
        venv = _venv_in(checkout, dormant=False)
        self._some_process_exists()

        plan = plan_venv_eviction(self.workspace, idle_days=1)
        evict_venvs(plan)

        assert venv.exists()
        assert any("too recently" in kept for kept in plan.kept)

    def test_an_enumeration_gap_costs_reclaim_rather_than_safety(self) -> None:
        """Unlike the env-dir reaper, a missed checkout is simply never a candidate.

        The gap is injected at the scan rather than by breaking a repo: how gaps
        ARISE is :mod:`tests.teatree_core.cleanup.test_checkout_registry`'s
        subject, and what this pass does with one is a different property.
        """
        checkout = make_git_repo(self.workspace / "clone")
        _venv_in(checkout)
        self._some_process_exists()
        partial = CheckoutRegistry(frozenset({str(checkout)}), ("could not scan somewhere",))

        with patch(f"{_REGISTRY}.scan_checkout_paths", return_value=partial):
            plan = plan_venv_eviction(self.workspace, idle_days=1)

        assert plan.gaps, "what went unread is reported"
        assert not plan.refusal, "a gap narrows the population; it does not refuse the pass"
        assert plan.candidates, "the checkouts that WERE seen are still reclaimable"

    def test_a_venv_whose_checkout_gained_a_process_after_planning_is_not_deleted(self) -> None:
        """Plan and delete are separated by the walks and prunes above — 34-68s on the box that produced this."""
        checkout = make_git_repo(self.workspace / "gained")
        venv = _venv_in(checkout)
        self._some_process_exists()

        plan = plan_venv_eviction(self.workspace, idle_days=1)
        assert [candidate.venv for candidate in plan.candidates] == [venv], "the control: it was planned for eviction"
        self._process_working_in(checkout / "src", pid="4243")
        outcome = evict_venvs(plan)

        assert venv.exists(), "an agent that arrived after planning must not have the floor pulled out"
        assert outcome.freed_bytes == 0
        assert any("a live process is working inside the checkout" in line for line in outcome.skipped)

    def test_a_process_table_that_stops_answering_after_planning_refuses_the_eviction(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        venv = _venv_in(checkout)
        self._some_process_exists()

        plan = plan_venv_eviction(self.workspace, idle_days=1)
        assert plan.candidates, "the control: it was planned for eviction"
        with blinded_process_table(self.workspace / "gone"):
            outcome = evict_venvs(plan)

        assert venv.exists(), "a table that went blind between planning and deleting authorises nothing"
        assert outcome.refusal

    def test_the_per_pass_cap_reports_what_it_deferred(self) -> None:
        for index in range(3):
            _venv_in(make_git_repo(self.workspace / f"clone{index}"))
        self._some_process_exists()

        with patch("teatree.core.cleanup.venv_eviction._MAX_EVICTIONS_PER_PASS", 1):
            plan = plan_venv_eviction(self.workspace, idle_days=1)

        assert len(plan.candidates) == 1
        assert sum("per-pass cap" in kept for kept in plan.kept) == 2

    def test_the_cap_takes_the_largest_venvs_and_reports_the_deferred_bytes(self) -> None:
        """A capped pass returns the most bytes — the cap used to truncate in walk order."""
        sizes = {"small": 1024, "largest": 8192, "middling": 4096}
        venvs = {name: _venv_in(make_git_repo(self.workspace / name), size=size) for name, size in sizes.items()}
        self._some_process_exists()

        with patch("teatree.core.cleanup.venv_eviction._MAX_EVICTIONS_PER_PASS", 2):
            plan = plan_venv_eviction(self.workspace, idle_days=1)

        assert [candidate.venv for candidate in plan.candidates] == [venvs["largest"], venvs["middling"]]
        assert any("deferred to the next pass: 1 venv(s), 1024 bytes" in kept for kept in plan.kept)
