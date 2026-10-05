"""The claim composition refuses every claim while the fleet admits no work."""

import itertools
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

import teatree.agents.runner as runner_mod
import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core import managers_task_claim
from teatree.core.claim_liveness import current_owner
from teatree.core.managers_task_claim import claim_admission_block_reason
from teatree.core.models import ModeOverride, Session, Task, Ticket, WorkerGeneration
from teatree.loop.drain import DrainReport, drain_worker
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


def _stop_the_fleet() -> None:
    ModeOverride.objects.set_override("off", reason="test: the operator stopped the fleet")


def admits_then_the_fleet_stops() -> Callable[[], str]:
    """A probe whose first read admits and whose fleet is stopped right after it, as a concurrent writer would."""
    probes = itertools.count()

    def probe() -> str:
        if next(probes) == 0:
            _stop_the_fleet()
            return ""
        return claim_admission_block_reason()

    return probe


def a_drain_lands_mid_claim(reports: list[DrainReport]) -> Callable[[], tuple[int, str]]:
    """Stands in for ``current_owner``, the last call before a claim writes, and runs a whole drain first."""

    def owner() -> tuple[int, str]:
        if not reports:
            reports.append(drain_worker(timeout=0))
        return current_owner()

    return owner


class TestTheOffPostureRefusesEveryClaim(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        self.task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, overlay="test"), phase="coding"
        )

    def test_the_default_posture_claims_the_pending_task(self) -> None:
        assert Task.objects.claim_next_pending(claimed_by="worker-1") == self.task

    def test_claim_next_pending_claims_nothing_under_off(self) -> None:
        _stop_the_fleet()

        assert Task.objects.claim_next_pending(claimed_by="worker-1") is None
        assert not Task.objects.claimable().exists()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING

    def test_the_refusal_names_the_posture(self) -> None:
        _stop_the_fleet()

        assert "admits no loop" in claim_admission_block_reason()

    def test_work_next_runs_no_agent_under_off(self) -> None:
        _stop_the_fleet()

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(runner_mod, "run_agent", MagicMock()) as run_agent,
        ):
            assert call_command("tasks", "work-next", claimed_by="worker-1") is None

        run_agent.assert_not_called()


class TestAFleetVerdictNobodyCanConfirmRefuses(TestCase):
    def test_an_unreadable_verdict_refuses_and_says_so(self) -> None:
        with patch("teatree.loops.enable_verdict.membership_loop_names", side_effect=RuntimeError("control DB locked")):
            assert "unreadable" in claim_admission_block_reason()

    def test_an_unregistered_verdict_fails_loud(self) -> None:
        with (
            patch.object(managers_task_claim._FLEET_ADMISSION, "refusal", None),
            pytest.raises(RuntimeError, match=r"CoreConfig\.ready"),
        ):
            claim_admission_block_reason()


class TestAStopBetweenTheProbeAndTheClaimWinsNoClaim(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        self.task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, overlay="test"), phase="coding"
        )

    def test_claim_next_pending_claims_nothing(self) -> None:
        with patch("teatree.core.managers.claim_admission_block_reason", admits_then_the_fleet_stops()):
            assert Task.objects.claim_next_pending(claimed_by="worker-1") is None

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING

    def test_work_next_runs_no_agent(self) -> None:
        with (
            patch("teatree.core.managers.claim_admission_block_reason", admits_then_the_fleet_stops()),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(runner_mod, "run_agent", MagicMock()) as run_agent,
        ):
            assert call_command("tasks", "work-next", claimed_by="worker-1") is None

        run_agent.assert_not_called()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING


class TestAQuiesceLandingMidClaimWinsNoClaim(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        self.task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, overlay="test"), phase="coding"
        )

    def test_claim_next_pending_claims_nothing_once_the_drain_reports_no_claim(self) -> None:
        reports: list[DrainReport] = []

        with patch("teatree.core.managers.current_owner", a_drain_lands_mid_claim(reports)):
            assert Task.objects.claim_next_pending(claimed_by="worker-1") is None

        assert reports[0].drained
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING

    def test_a_raw_quiesce_through_config_setting_set_wins_no_claim(self) -> None:
        issued: list[bool] = []

        def quiesce_through_the_public_command() -> tuple[int, str]:
            if not issued:
                issued.append(True)
                call_command("config_setting", "set", "worker_quiescing", "true", stdout=StringIO())
            return current_owner()

        with patch("teatree.core.managers.current_owner", quiesce_through_the_public_command):
            assert Task.objects.claim_next_pending(claimed_by="worker-1") is None

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING


_N = "a" * 40
_N1 = "b" * 40


def _in_an_hour() -> datetime:
    return timezone.now() + timedelta(hours=1)


def _running_as(sha: str) -> AbstractContextManager[object]:
    return patch.dict("os.environ", {"TEATREE_GENERATION": sha})


class TestADrainingGenerationRefusesItsOwnClaims(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        self.task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, overlay="test"), phase="coding"
        )
        WorkerGeneration.objects.boot(_N).begin_drain(deadline=timezone.now())
        WorkerGeneration.objects.boot(_N1)

    def test_the_draining_generation_is_refused_by_name(self) -> None:
        with _running_as(_N):
            assert claim_admission_block_reason() == "this worker's generation aaaaaaaaaaaa is draining"
            assert Task.objects.claim_next_pending(claimed_by="worker-1") is None

    def test_the_next_generation_still_claims(self) -> None:
        with _running_as(_N1):
            assert Task.objects.claim_next_pending(claimed_by="worker-1") == self.task

    def test_a_legacy_worker_ignores_the_generation_registry(self) -> None:
        with _running_as(""):
            assert claim_admission_block_reason() == ""

    def test_an_unregistered_generation_is_admitted(self) -> None:
        with _running_as("c" * 40):
            assert claim_admission_block_reason() == ""


class TestAGenerationDrainLandingMidClaimWinsNoClaim(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        self.task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, overlay="test"), phase="coding"
        )
        self.generation = WorkerGeneration.objects.boot(_N)

    def test_the_fence_rolls_the_claim_back(self) -> None:
        def drain_lands() -> tuple[int, str]:
            WorkerGeneration.objects.get(sha=_N).begin_drain(deadline=_in_an_hour())
            return current_owner()

        with _running_as(_N), patch("teatree.core.managers.current_owner", drain_lands):
            assert Task.objects.claim_next_pending(claimed_by="worker-1") is None

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING

    def test_an_active_verdict_read_before_a_drain_admits_no_claim_after_it(self) -> None:
        with _running_as(_N):
            assert claim_admission_block_reason() == ""
            self.generation.begin_drain(deadline=_in_an_hour())

            assert Task.objects.claim_next_pending(claimed_by="worker-1") is None

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
