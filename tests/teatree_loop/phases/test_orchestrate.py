"""Tests for ``teatree.loop.phases.orchestrate`` — the wip-driven fan-out (#1796)."""

import itertools
from datetime import timedelta
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.config import UserSettings, Wip
from teatree.core.backend_factory import OverlayBackends
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.external_delivery import mark_external_delivery
from teatree.loop.phases.orchestrate import _dispatchable_filter, orchestrate_phase

_url_counter = itertools.count()


def _settings(wip: Wip) -> UserSettings:
    return UserSettings(wip=wip)


def _with_wip(wip: Wip):
    return patch("teatree.loop.phases.orchestrate.get_effective_settings", return_value=_settings(wip))


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


class TestOrchestratePhaseWip(django.test.TestCase):
    def test_medium_is_a_noop_and_never_touches_the_db(self) -> None:
        _dispatchable_task()
        with _with_wip(Wip.MEDIUM):
            manifest = orchestrate_phase(claim=True)
        assert manifest.cap == 0
        assert manifest.entries == []
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 0

    def test_slow_admits_at_most_one(self) -> None:
        for _ in range(3):
            _dispatchable_task()
        with _with_wip(Wip.SLOW):
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
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.cap == 3
        assert len(manifest.entries) == 3
        assert Task.objects.filter(status=Task.Status.CLAIMED).count() == 3

    def test_boost_uses_the_same_budget_as_full(self) -> None:
        for _ in range(4):
            _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_wip(Wip.BOOST):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.cap == 2
        assert len(manifest.entries) == 2


class TestOrchestratePhaseManifestGolden(django.test.TestCase):
    """Pin the manifest shape across ``Wip`` x backlog x budget (#1796).

    A golden-ish table so a regression in the wip dial -> cap -> admitted-count
    contract turns red. Each row claims a fresh set of dispatchable tasks, runs
    ``orchestrate_phase`` at the named wip/budget, and asserts ``(cap, admitted)``.
    """

    def test_wip_backlog_budget_table(self) -> None:
        # Each row is wip, backlog size, summed budget, expected cap, expected admitted.
        table = [
            (Wip.MEDIUM, 3, 5, 0, 0),  # medium is always a no-op
            (Wip.SLOW, 3, 5, 1, 1),  # slow clamps to one
            (Wip.SLOW, 0, 5, 1, 0),  # slow cap is 1 but empty backlog admits 0
            (Wip.FULL, 5, 3, 3, 3),  # full clamps to summed budget
            (Wip.FULL, 2, 5, 5, 2),  # full budget exceeds backlog -> admit backlog
            (Wip.FULL, 4, 0, 0, 0),  # zero budget -> empty manifest
            (Wip.BOOST, 4, 2, 2, 2),  # boost uses the same budget as full
        ]
        for wip, backlog, budget, expected_cap, expected_admitted in table:
            # ``wip.value`` (the plain ``str``) is the subTest label, not the raw
            # ``Wip`` enum: pytest-subtests ships each subTest's kwargs to the
            # xdist controller through execnet, whose serializer cannot encode an
            # arbitrary enum type (``DumpError: can't serialize <enum 'Wip'>``)
            # and so wedges the whole test under ``-n auto`` while passing serially.
            with self.subTest(wip=wip.value, backlog=backlog, budget=budget):
                Task.objects.all().delete()
                for _ in range(backlog):
                    _dispatchable_task()
                backends = [OverlayBackends(name="a", max_concurrent_auto_starts=budget)]
                with _with_wip(wip):
                    manifest = orchestrate_phase(backends=backends, claim=True)
                assert manifest.cap == expected_cap, (wip, backlog, budget)
                assert len(manifest.entries) == expected_admitted, (wip, backlog, budget)
                assert Task.objects.filter(status=Task.Status.CLAIMED).count() == expected_admitted


class TestOrchestratePhaseClaimSemantics(django.test.TestCase):
    def test_default_plan_is_read_only_and_claims_nothing(self) -> None:
        task = _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends)
        assert [e.task_id for e in manifest.entries] == [task.pk]
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_claim_uses_the_existing_cas_and_marks_rows_claimed(self) -> None:
        task = _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_wip(Wip.FULL):
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
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.entries == []
        orphan.refresh_from_db()
        assert orphan.status == Task.Status.PENDING

    def test_manifest_entry_carries_subagent_and_issue_url(self) -> None:
        task = _dispatchable_task(phase="coding")
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=1)]
        with _with_wip(Wip.FULL):
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
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.merge_order == [shipping.pk, testing.pk, coding.pk]


class TestOrchestratePhaseFailOpen(django.test.TestCase):
    def test_full_wip_with_zero_budget_returns_empty_manifest(self) -> None:
        _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=0)]
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.cap == 0
        assert manifest.entries == []

    def test_empty_backends_list_yields_zero_budget_not_active_overlay_cap(self) -> None:
        # An explicit empty list means "no overlays scanned" — budget 0, not
        # a fall-through to the active overlay's cap.
        _dispatchable_task()
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=[], claim=True)
        assert manifest.cap == 0
        assert manifest.entries == []

    def test_claim_sweep_db_error_degrades_to_partial_manifest(self) -> None:
        _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with (
            _with_wip(Wip.FULL),
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
        with _with_wip(Wip.FULL):
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
        with _with_wip(Wip.FULL):
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
        _claim_task(_dispatchable_task())  # live lease — must be subtracted
        pending = _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=False)
        # live-lease task reduces budget by 1; expired-lease task does not
        assert manifest.cap == 1
        assert len(manifest.entries) == 1
        assert manifest.entries[0].task_id == pending.pk

    def test_non_dispatchable_claimed_tasks_are_not_counted(self) -> None:
        non_dispatchable = _claim_task(_dispatchable_task(role=Ticket.Role.REVIEWER, phase="coding"))
        _claim_task(_dispatchable_task())  # dispatchable claimed — must be subtracted
        pending = _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=False)
        # only the dispatchable claimed task reduces the budget
        assert manifest.cap == 1
        assert len(manifest.entries) == 1
        assert manifest.entries[0].task_id == pending.pk
        non_dispatchable.refresh_from_db()
        assert non_dispatchable.status == Task.Status.CLAIMED

    def test_admitted_plus_in_flight_never_exceeds_cap(self) -> None:
        _claim_task(_dispatchable_task())
        for _ in range(3):
            _dispatchable_task()
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        admitted = len(manifest.entries)
        in_flight_before = 1
        assert admitted + in_flight_before <= 2


class TestDispatchExcludesLiveExternalDelivery(django.test.TestCase):
    """A unit under a live #2104 delivery lease must never be dispatched (#2217).

    Reproduces the double-dispatch incident: a hand-delivery owner advanced a
    ticket STARTED -> PLANNED with the lease still live, the loop scheduled a
    ``coding`` Task, and the dispatch chokepoint admitted it -> two coders on
    one ticket. The chokepoint must exclude any phase on a hand-delivered ticket.
    """

    def _lease(self, ticket: Ticket, *, lease_seconds: int) -> None:
        mark_external_delivery(ticket, lease_seconds=lease_seconds)
        ticket.refresh_from_db()

    def test_live_lease_coding_task_is_excluded_from_dispatchable_filter(self) -> None:
        task = _dispatchable_task(phase="coding")
        self._lease(task.ticket, lease_seconds=3600)
        dispatchable = Task.objects.filter(status=Task.Status.PENDING).filter(_dispatchable_filter())
        assert task.pk not in set(dispatchable.values_list("pk", flat=True))

    def test_live_lease_coding_task_is_not_claimed(self) -> None:
        task = _dispatchable_task(phase="coding")
        self._lease(task.ticket, lease_seconds=3600)
        claimed = Task.objects.claim_next_pending(claimed_by="loop", extra_filter=_dispatchable_filter())
        assert claimed is None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_live_lease_task_is_not_admitted_by_orchestrate(self) -> None:
        task = _dispatchable_task(phase="coding")
        self._lease(task.ticket, lease_seconds=3600)
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=5)]
        with _with_wip(Wip.FULL):
            manifest = orchestrate_phase(backends=backends, claim=True)
        assert manifest.entries == []
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_expired_lease_coding_task_dispatches(self) -> None:
        task = _dispatchable_task(phase="coding")
        self._lease(task.ticket, lease_seconds=-1)
        claimed = Task.objects.claim_next_pending(claimed_by="loop", extra_filter=_dispatchable_filter())
        assert claimed is not None
        assert claimed.pk == task.pk

    def test_absent_lease_coding_task_dispatches(self) -> None:
        task = _dispatchable_task(phase="coding")
        claimed = Task.objects.claim_next_pending(claimed_by="loop", extra_filter=_dispatchable_filter())
        assert claimed is not None
        assert claimed.pk == task.pk

    def test_malformed_lease_coding_task_dispatches(self) -> None:
        task = _dispatchable_task(phase="coding")
        task.ticket.extra = {"external_delivery": {"expires_at": "not-a-date"}}
        task.ticket.save(update_fields=["extra"])
        claimed = Task.objects.claim_next_pending(claimed_by="loop", extra_filter=_dispatchable_filter())
        assert claimed is not None
        assert claimed.pk == task.pk

    def test_terminal_ticket_with_live_lease_still_excluded(self) -> None:
        # A terminal ticket carrying a live lease is still excluded — the
        # exclusion is by lease liveness, not by FSM state. Such a task is not
        # normally dispatched anyway, so excluding it is conservative and safe.
        task = _dispatchable_task(phase="coding")
        task.ticket.state = Ticket.State.MERGED
        task.ticket.save(update_fields=["state"])
        self._lease(task.ticket, lease_seconds=3600)
        dispatchable = Task.objects.filter(status=Task.Status.PENDING).filter(_dispatchable_filter())
        assert task.pk not in set(dispatchable.values_list("pk", flat=True))
