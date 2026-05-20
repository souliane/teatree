"""DB-backed tests for ``ArchitecturalReviewScanner`` (#1136).

The scanner periodically queues an ``architectural_review`` ``Task`` row for
each enabled overlay, using two independent triggers: a cadence (last review
older than ``architectural_review_cadence_hours``) and a merge-count
(``architectural_review_after_merge_count`` ticket merges since the last
queued review). Both default OFF — the overlay must opt in by setting
``architectural_review_enabled = True``.

Integration-style with real Django ORM rows. Times are backdated with
``QuerySet.update()`` so we avoid an extra dep on a time-travel library
(mirrors :mod:`tests.teatree_loop.test_stale_tickets`).
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.loop.scanners.architectural_review import ARCHITECTURAL_REVIEW_PHASE, ArchitecturalReviewScanner

OVERLAY = "acme"


def _scanner(
    *,
    enabled: bool = True,
    skill: str = "ac-reviewing-codebase",
    cadence_hours: int = 168,
    after_merge_count: int = 25,
) -> ArchitecturalReviewScanner:
    return ArchitecturalReviewScanner(
        overlay_name=OVERLAY,
        enabled=enabled,
        skill=skill,
        cadence_hours=cadence_hours,
        after_merge_count=after_merge_count,
    )


def _last_review_task(overlay: str = OVERLAY) -> Task | None:
    return (
        Task.objects.filter(
            ticket__overlay=overlay,
            phase=ARCHITECTURAL_REVIEW_PHASE,
        )
        .order_by("-id")
        .first()
    )


def _backdate_task(task: Task, *, hours: int) -> None:
    """Move a Task's bookkeeping into the past so the cadence math triggers.

    ``Task`` has no ``created_at`` column, so the scanner derives the
    last-review timestamp from its ``Session.started_at`` (auto_now_add),
    which we can backdate via ``update()``.
    """
    Session.objects.filter(pk=task.session_id).update(
        started_at=timezone.now() - timedelta(hours=hours),
    )


def _make_merge_after(overlay: str, *, after_hours: int) -> Ticket:
    """Create a merged-state ticket with a transition timestamp ``after_hours`` ago.

    The scanner counts merged/delivered tickets whose latest matching
    TicketTransition is *after* the last review task. We backdate the
    transition row's ``created_at`` to control the test ordering.
    """
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://example.com/issues/{Ticket.objects.count() + 100}",
        state=Ticket.State.MERGED,
    )
    transition = TicketTransition.objects.create(
        ticket=ticket,
        from_state=Ticket.State.SHIPPED,
        to_state=Ticket.State.MERGED,
    )
    TicketTransition.objects.filter(pk=transition.pk).update(
        created_at=timezone.now() - timedelta(hours=after_hours),
    )
    return ticket


class ArchitecturalReviewScannerTests(TestCase):
    def test_disabled_overlay_queues_nothing(self) -> None:
        """Default-off: with ``enabled=False`` no task is queued regardless of cadence."""
        # Cadence trivially elapsed (no prior review), but disabled wins.
        signals = _scanner(enabled=False, cadence_hours=1).scan()
        assert signals == []
        assert _last_review_task() is None

    def test_enabled_no_prior_review_queues_task(self) -> None:
        """First-ever run on an enabled overlay queues exactly one task.

        The fail-safe assertion (#1136 RED CARD): when enabled and no prior
        review task exists, the cadence is trivially elapsed → a task MUST
        be queued. Absence is the bug.
        """
        signals = _scanner().scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "architectural_review.queued"
        assert signal.payload["overlay"] == OVERLAY
        assert signal.payload["skill"] == "ac-reviewing-codebase"
        assert signal.payload["phase"] == ARCHITECTURAL_REVIEW_PHASE

        task = _last_review_task()
        assert task is not None
        assert task.phase == ARCHITECTURAL_REVIEW_PHASE
        assert task.status == Task.Status.PENDING
        assert task.ticket.overlay == OVERLAY

    def test_cadence_elapsed_queues_new_task(self) -> None:
        """A prior review older than cadence_hours triggers a new task."""
        # Seed a completed review task ``8 days`` ago.
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=24 * 8)

        second = _scanner(cadence_hours=168).scan()

        assert len(second) == 1
        task = _last_review_task()
        assert task is not None
        assert task.pk != prior.pk

    def test_cadence_not_elapsed_no_task(self) -> None:
        """A recent review within the cadence window blocks new queueing."""
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        # 1 hour ago — far inside the 168-hour window.
        _backdate_task(prior, hours=1)

        second = _scanner(cadence_hours=168).scan()

        assert second == []
        # No new task created.
        latest = _last_review_task()
        assert latest is not None
        assert latest.pk == prior.pk

    def test_pending_task_blocks_new_queueing(self) -> None:
        """A still-PENDING review task suppresses dupes even after cadence elapses."""
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        # Leave it PENDING and backdate so cadence WOULD trigger.
        _backdate_task(prior, hours=24 * 14)

        second = _scanner(cadence_hours=168).scan()

        assert second == []
        latest = _last_review_task()
        assert latest is not None
        assert latest.pk == prior.pk
        assert latest.status == Task.Status.PENDING

    def test_claimed_task_blocks_new_queueing(self) -> None:
        """A CLAIMED (in-flight) review task is treated as pending → no dupes."""
        _scanner(cadence_hours=168).scan()
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.CLAIMED)
        _backdate_task(prior, hours=24 * 14)

        second = _scanner(cadence_hours=168).scan()

        assert second == []

    def test_merge_count_trigger_fires(self) -> None:
        """3 merges since the last review with after_merge_count=2 → queue."""
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        # 1 hour ago — cadence will not fire.
        _backdate_task(prior, hours=1)

        # Three merges after the prior review (transition timestamps "now").
        for _ in range(3):
            _make_merge_after(OVERLAY, after_hours=0)

        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert len(second) == 1
        # Cadence is not elapsed; only the merge-count trigger could have fired.
        assert second[0].payload["trigger"] == "after_merge_count"

    def test_merge_count_below_threshold_no_task(self) -> None:
        """One merge with after_merge_count=2 → no task (cadence also not elapsed)."""
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=1)

        _make_merge_after(OVERLAY, after_hours=0)

        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert second == []

    def test_merge_count_ignores_merges_before_last_review(self) -> None:
        """Merges that happened *before* the last review don't count."""
        # An old merge in the books.
        _make_merge_after(OVERLAY, after_hours=24 * 30)

        # Seed and complete a recent review.
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=1)

        # Old merge predates the review — should not count.
        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert second == []

    def test_overlay_isolation(self) -> None:
        """Merges in another overlay don't count toward this overlay's quota."""
        first = _scanner(cadence_hours=999, after_merge_count=25).scan()
        assert len(first) == 1
        prior = _last_review_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=1)

        # Three merges on a *different* overlay — must not count.
        for _ in range(3):
            _make_merge_after("other-overlay", after_hours=0)

        second = _scanner(cadence_hours=999, after_merge_count=2).scan()

        assert second == []

    def test_queued_task_carries_skill_name(self) -> None:
        """The skill name lands in the Task's execution_reason for the dispatcher to pick up."""
        scanner = _scanner(skill="ac-custom-review")
        scanner.scan()

        task = _last_review_task()
        assert task is not None
        assert "ac-custom-review" in task.execution_reason

    def test_signal_summary_is_concise(self) -> None:
        """Statusline-friendly: one-line summary mentioning overlay + cadence reason."""
        signals = _scanner().scan()
        assert len(signals) == 1
        # No prior review → first-time trigger reason.
        assert OVERLAY in signals[0].summary


class ArchitecturalReviewWiringTests(TestCase):
    """Confirm the tick-job builder honours ``OverlayConfig`` opt-in (#1136)."""

    def test_disabled_overlay_skips_wiring(self) -> None:
        """An overlay with the default OFF setting registers no scanner."""
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.tick_jobs import _architectural_review_scanner_for  # noqa: PLC0415

        class _StubOverlay:
            class config:  # noqa: N801
                architectural_review_enabled = False
                architectural_review_skill = "ac-reviewing-codebase"
                architectural_review_cadence_hours = 168
                architectural_review_after_merge_count = 25

        backend = OverlayBackends(name="acme", overlay=_StubOverlay())
        assert _architectural_review_scanner_for(backend) is None

    def test_enabled_overlay_propagates_config(self) -> None:
        """Enabled overlay → scanner is built with the configured kwargs."""
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.tick_jobs import _architectural_review_scanner_for  # noqa: PLC0415

        class _StubOverlay:
            class config:  # noqa: N801
                architectural_review_enabled = True
                architectural_review_skill = "ac-custom"
                architectural_review_cadence_hours = 72
                architectural_review_after_merge_count = 10

        backend = OverlayBackends(name="acme", overlay=_StubOverlay())
        scanner = _architectural_review_scanner_for(backend)
        assert scanner is not None
        assert scanner.overlay_name == "acme"
        assert scanner.enabled is True
        assert scanner.skill == "ac-custom"
        assert scanner.cadence_hours == 72
        assert scanner.after_merge_count == 10

    def test_no_overlay_instance_skips(self) -> None:
        """TOML-only overlay (no Python class) is silently skipped."""
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.tick_jobs import _architectural_review_scanner_for  # noqa: PLC0415

        backend = OverlayBackends(name="acme", overlay=None)
        assert _architectural_review_scanner_for(backend) is None
