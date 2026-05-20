"""DB-backed tests for ``ScanningNewsScanner`` (#1191).

The scanner periodically queues a ``scanning_news`` ``Task`` row for the
teatree overlay on a single trigger: cadence
(``scanning_news_cadence_hours``, default 24h). Unlike the architectural-
review scanner, there is no after-merge backstop — news scanning is a
once-a-day platform behaviour, not coupled to delivery velocity.

The scanner is teatree-CORE and overlay-agnostic in its placement: the
wiring layer attaches it as a global scanner (``overlay=""``) alongside
:class:`PendingTasksScanner` / :class:`IncomingEventsScanner`. The
queued :class:`Task` itself is anchored at a fixed placeholder Ticket
with ``overlay="teatree"`` so the dispatcher routes through the standard
pending-task pipeline.

Integration-style with real Django ORM rows. Times are backdated with
``QuerySet.update()`` (mirrors
:mod:`tests.teatree_loop.test_architectural_review_scanner`).
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.config import UserSettings
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.loop.scanners.scanning_news import SCANNING_NEWS_OVERLAY, SCANNING_NEWS_PHASE, ScanningNewsScanner


def _scanner(
    *,
    skill: str = "scanning-news",
    cadence_hours: int = 24,
) -> ScanningNewsScanner:
    return ScanningNewsScanner(skill=skill, cadence_hours=cadence_hours)


def _last_news_task() -> Task | None:
    return (
        Task.objects.filter(
            ticket__overlay=SCANNING_NEWS_OVERLAY,
            phase=SCANNING_NEWS_PHASE,
        )
        .order_by("-id")
        .first()
    )


def _backdate_task(task: Task, *, hours: int) -> None:
    """Move a Task's bookkeeping into the past so the cadence math triggers.

    ``Task`` has no ``created_at`` column, so the scanner derives the
    last-run timestamp from its ``Session.started_at`` (auto_now_add),
    which we can backdate via ``update()``.
    """
    Session.objects.filter(pk=task.session_id).update(
        started_at=timezone.now() - timedelta(hours=hours),
    )


class ScanningNewsScannerTests(TestCase):
    def test_no_prior_run_queues_task(self) -> None:
        """First-ever run queues exactly one task (bootstrap trigger)."""
        signals = _scanner().scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "scanning_news.queued"
        assert signal.payload["overlay"] == SCANNING_NEWS_OVERLAY
        assert signal.payload["skill"] == "scanning-news"
        assert signal.payload["phase"] == SCANNING_NEWS_PHASE
        assert signal.payload["trigger"] == "bootstrap"

        task = _last_news_task()
        assert task is not None
        assert task.phase == SCANNING_NEWS_PHASE
        assert task.status == Task.Status.PENDING
        assert task.ticket.overlay == SCANNING_NEWS_OVERLAY

    def test_cadence_elapsed_queues_new_task(self) -> None:
        """A prior run older than cadence_hours triggers a new task."""
        first = _scanner(cadence_hours=24).scan()
        assert len(first) == 1
        prior = _last_news_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=25)

        second = _scanner(cadence_hours=24).scan()

        assert len(second) == 1
        assert second[0].payload["trigger"] == "cadence"
        task = _last_news_task()
        assert task is not None
        assert task.pk != prior.pk

    def test_cadence_not_elapsed_no_task(self) -> None:
        """A recent run within the cadence window blocks new queueing."""
        first = _scanner(cadence_hours=24).scan()
        assert len(first) == 1
        prior = _last_news_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        # 1 hour ago — far inside the 24-hour window.
        _backdate_task(prior, hours=1)

        second = _scanner(cadence_hours=24).scan()

        assert second == []
        latest = _last_news_task()
        assert latest is not None
        assert latest.pk == prior.pk

    def test_pending_task_blocks_new_queueing(self) -> None:
        """A still-PENDING news task suppresses dupes even after cadence elapses."""
        first = _scanner(cadence_hours=24).scan()
        assert len(first) == 1
        prior = _last_news_task()
        assert prior is not None
        # Leave it PENDING and backdate so cadence WOULD trigger.
        _backdate_task(prior, hours=48)

        second = _scanner(cadence_hours=24).scan()

        assert second == []
        latest = _last_news_task()
        assert latest is not None
        assert latest.pk == prior.pk
        assert latest.status == Task.Status.PENDING

    def test_claimed_task_blocks_new_queueing(self) -> None:
        """A CLAIMED (in-flight) news task is treated as pending — no dupes."""
        _scanner(cadence_hours=24).scan()
        prior = _last_news_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.CLAIMED)
        _backdate_task(prior, hours=48)

        second = _scanner(cadence_hours=24).scan()

        assert second == []

    def test_queued_task_carries_skill_name(self) -> None:
        """The skill name lands in the Task's execution_reason for the dispatcher."""
        scanner = _scanner(skill="scanning-news-custom")
        scanner.scan()

        task = _last_news_task()
        assert task is not None
        assert "scanning-news-custom" in task.execution_reason

    def test_signal_summary_is_concise(self) -> None:
        """Statusline-friendly: one-line summary mentioning the overlay anchor."""
        signals = _scanner().scan()
        assert len(signals) == 1
        assert SCANNING_NEWS_OVERLAY in signals[0].summary


class ScanningNewsWiringTests(TestCase):
    """Confirm the tick-job builder reads core config (#1191).

    The scanning-news scanner is a single global scanner (``overlay=""``)
    keyed off teatree-core platform config — disable via the
    ``scanning_news_disabled`` escape hatch in ``[teatree]``.
    """

    def _patched_settings(self, **overrides: object) -> UserSettings:
        return UserSettings(**overrides)

    def test_default_core_config_builds_scanner(self) -> None:
        """Default core config (disabled=False) → wiring produces a scanner."""
        from teatree.loop.tick_jobs import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type("Cfg", (), {"user": self._patched_settings()})(),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is not None
        assert scanner.skill == "scanning-news"
        assert scanner.cadence_hours == 24

    def test_disabled_in_core_config_skips_wiring(self) -> None:
        """Escape hatch: ``scanning_news_disabled = True`` → no scanner."""
        from teatree.loop.tick_jobs import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type(
                "Cfg",
                (),
                {"user": self._patched_settings(scanning_news_disabled=True)},
            )(),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is None

    def test_core_config_propagates_to_scanner_kwargs(self) -> None:
        """Tuned core config flows through to the scanner kwargs."""
        from teatree.loop.tick_jobs import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type(
                "Cfg",
                (),
                {
                    "user": self._patched_settings(
                        scanning_news_skill="custom-news",
                        scanning_news_cadence_hours=12,
                    ),
                },
            )(),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is not None
        assert scanner.skill == "custom-news"
        assert scanner.cadence_hours == 12
