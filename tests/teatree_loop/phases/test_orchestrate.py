"""Tests for ``teatree.loop.phases.orchestrate`` — the speed-driven fan-out (#1796)."""

import itertools
from datetime import timedelta
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.config import Speed, UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.core.models import Session, Task, Ticket
from teatree.loop.phases.orchestrate import orchestrate_phase

_url_counter = itertools.count()


def _settings(speed: Speed) -> UserSettings:
    return UserSettings(speed=speed)


def _with_speed(speed: Speed):
    return patch("teatree.loop.phases.orchestrate.get_effective_settings", return_value=_settings(speed))


def _dispatchable_task(*, phase: str = "coding", role: str = Ticket.Role.AUTHOR) -> Task:
    n = next(_url_counter)
    ticket = Ticket.objects.create(role=role, issue_url=f"https://x/{phase}/{n}", overlay="acme")
    session = Session.objects.create(ticket=ticket, agent_id=f"a-{ticket.pk}")
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.PENDING)


def _claim_task(task: Task) -> Task:
    """Mark a dispatchable task as CLAIMED with a live lease, simulating an in-flight worker."""
    now = timezone.now()
    task.status = Task.Status.CLAIMED
    task.claimed_by = "test-worker"
    task.claimed_at = now
    task.heartbeat_at = now
    task.lease_expires_at = now + timedelta(seconds=300)
    task.save(update_fields=["status", "claimed_by", "claimed_at", "heartbeat_at", "lease_expires_at"])
    return task


class TestOrchestratePhaseSpeed(django.test.TestCase):
    def test_medium_is_a_noop_and_never_touches_the_db(self) -> None:
        _dispatchable_task()
        with _with_speed(Speed.MEDIUM):
            manifest = orchestrate_phase(claim=True)
        assert manifest.cap == 0
        assert manifest.entries == []
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 0

    def test_slow_admits_at_most_one(self) -> None:
        for _ in range(3):
            _dispatchable_task()
        with _with_speed(Speed.SLOW):
            manifest = orchestrate_phase(claim=True)
        assert manifest.cap == 1
        assert len(manifest.entries) == 1
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 1

    def test_full_clamps_the_manifest_to_summed_overlay_budget(self) -> None:
        for _ in range(5):
            _dispatchable_task()
        backends = [
            OverlayBackends(name="a", max_concurrent_auto_starts=2),
            OverlayBackends(name="b", max_concurrent_auto_starts=1),
        ]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.cap == 3
        assert len(manifest.entries) == 3
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 3

    def test_boost_uses_the_same_budget_as_full(self) -> None:
        for _ in range(4):
            _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_speed(Speed.BOOST):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.cap == 2
        assert len(manifest.entries) == 2


class TestOrchestratePhaseClaimSemantics(django.test.TestCase):
    def test_default_plan_is_read_only_and_claims_nothing(self) -> None:
        task = _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends)
        assert [e.task_id for e in manifest.entries] == [task.pk]
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_claim_uses_the_existing_cas_and_marks_rows_claimed(self) -> None:
        task = _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_speed(Speed.FULL):
            orchestrate_phase(backends=backends, claim=True, claimed_by="orchestrate-x")
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "orchestrate-x"

    def test_non_dispatchable_rows_are_left_untouched(self) -> None:
        # A reviewer-role coding task has no registered subagent, so it is
        # not dispatchable and must never be admitted.
        ticket = Ticket.objects.create(role=Ticket.Role.REVIEWER, issue_url="https://x/n", overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="n")
        orphan = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=5)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.entries == []
        orphan.refresh_from_db()
        assert orphan.status == Task.Status.PENDING

    def test_manifest_entry_carries_subagent_and_issue_url(self) -> None:
        task = _dispatchable_task(phase="coding")
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=1)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        entry = manifest.entries[0]
        assert entry.task_id == task.pk
        assert entry.subagent == "t3:coder"
        assert entry.issue_url == task.ticket.issue_url
        assert entry.overlay == "acme"


class TestOrchestratePhaseMergeOrder(django.test.TestCase):
    def test_merge_order_puts_tasks_nearer_shipping_first(self) -> None:
        coding = _dispatchable_task(phase="coding")
        shipping = _dispatchable_task(phase="shipping")
        testing = _dispatchable_task(phase="testing")
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=9)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.merge_order == [shipping.pk, testing.pk, coding.pk]


class TestOrchestratePhaseFailOpen(django.test.TestCase):
    def test_full_speed_with_zero_budget_returns_empty_manifest(self) -> None:
        _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=0)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.cap == 0
        assert manifest.entries == []

    def test_empty_backends_list_yields_zero_budget_not_active_overlay_cap(self) -> None:
        # An explicit empty list means "no overlays scanned" — budget 0, not
        # a fall-through to the active overlay's cap.
        _dispatchable_task()
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=[], claim=True)
        assert manifest.cap == 0
        assert manifest.entries == []

    def test_claim_sweep_db_error_degrades_to_partial_manifest(self) -> None:
        _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with (
            _with_speed(Speed.FULL),
            patch("teatree.core.models.task.Task.objects.claim_next_pending", side_effect=RuntimeError("db down")),
        ):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.entries == []


class TestPipelinedWIPStandingCap(django.test.TestCase):
    """Pipelined WIP cap: admitted + in-flight claimed <= cap at all times (#1796)."""

    def test_in_flight_claimed_tasks_reduce_available_budget(self) -> None:
        already_claimed = _claim_task(_dispatchable_task())
        pending = _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=False)
        assert manifest.cap == 1
        assert len(manifest.entries) == 1
        assert manifest.entries[0].task_id == pending.pk
        already_claimed.refresh_from_db()
        assert already_claimed.status == Task.Status.CLAIMED

    def test_cap_is_never_exceeded_when_all_slots_already_claimed(self) -> None:
        for _ in range(3):
            _claim_task(_dispatchable_task())
        for _ in range(2):
            _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=3)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.cap == 0
        assert manifest.entries == []
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 3

    def test_expired_lease_tasks_are_not_counted_as_in_flight(self) -> None:
        stale = _dispatchable_task()
        stale.status = Task.Status.CLAIMED
        stale.claimed_by = "dead-worker"
        stale.claimed_at = timezone.now() - timedelta(seconds=600)
        stale.lease_expires_at = timezone.now() - timedelta(seconds=300)
        stale.save(update_fields=["status", "claimed_by", "claimed_at", "lease_expires_at"])
        _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=False)
        assert manifest.cap == 2

    def test_non_dispatchable_claimed_tasks_are_not_counted(self) -> None:
        non_dispatchable = _claim_task(_dispatchable_task(role=Ticket.Role.REVIEWER, phase="coding"))
        _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=False)
        assert manifest.cap == 2
        non_dispatchable.refresh_from_db()
        assert non_dispatchable.status == Task.Status.CLAIMED

    def test_admitted_plus_in_flight_never_exceeds_cap(self) -> None:
        _claim_task(_dispatchable_task())
        for _ in range(3):
            _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_speed(Speed.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        admitted = len(manifest.entries)
        in_flight_before = 1
        assert admitted + in_flight_before <= 2
