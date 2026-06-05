"""DB-backed tests for ``EvalLocalScanner``.

The scanner periodically queues an ``eval_local`` ``Task`` row for the
active core overlay on a single trigger: cadence
(``eval_local_cadence_hours``, default 168h = weekly). It mirrors the
shape of :mod:`teatree.loop.scanners.scanning_news`: the queued task
directs running the SCOPED eval suite locally via the same runner
``t3 eval run`` uses (the subscription backend, no API key), without
blocking the tick.

Integration-style with real Django ORM rows. Times are backdated with
``QuerySet.update()`` (mirrors
:mod:`tests.teatree_loop.test_scanning_news_scanner`).
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.config import UserSettings
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.loop.scanners.eval_local import EVAL_LOCAL_PHASE, EvalLocalScanner

TEST_OVERLAY_NAME = "t3-teatree"


def _scanner(
    *,
    overlay_name: str = TEST_OVERLAY_NAME,
    skill: str = "eval",
    cadence_hours: int = 168,
) -> EvalLocalScanner:
    return EvalLocalScanner(overlay_name=overlay_name, skill=skill, cadence_hours=cadence_hours)


def _last_eval_task(overlay_name: str = TEST_OVERLAY_NAME) -> Task | None:
    return Task.objects.filter(ticket__overlay=overlay_name, phase=EVAL_LOCAL_PHASE).order_by("-id").first()


def _backdate_task(task: Task, *, hours: int) -> None:
    Session.objects.filter(pk=task.session_id).update(started_at=timezone.now() - timedelta(hours=hours))


class EvalLocalScannerTests(TestCase):
    def test_no_prior_run_queues_task(self) -> None:
        signals = _scanner().scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "eval_local.queued"
        assert signal.payload["overlay"] == TEST_OVERLAY_NAME
        assert signal.payload["skill"] == "eval"
        assert signal.payload["phase"] == EVAL_LOCAL_PHASE
        assert signal.payload["trigger"] == "bootstrap"

        task = _last_eval_task()
        assert task is not None
        assert task.phase == EVAL_LOCAL_PHASE
        assert task.status == Task.Status.PENDING
        assert task.ticket.overlay == TEST_OVERLAY_NAME

    def test_cadence_elapsed_queues_new_task(self) -> None:
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_eval_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=169)

        second = _scanner(cadence_hours=168).scan()

        assert len(second) == 1
        assert second[0].payload["trigger"] == "cadence"
        task = _last_eval_task()
        assert task is not None
        assert task.pk != prior.pk

    def test_cadence_not_elapsed_no_task(self) -> None:
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_eval_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=24)

        second = _scanner(cadence_hours=168).scan()

        assert second == []
        latest = _last_eval_task()
        assert latest is not None
        assert latest.pk == prior.pk

    def test_pending_task_blocks_new_queueing(self) -> None:
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_eval_task()
        assert prior is not None
        _backdate_task(prior, hours=336)

        second = _scanner(cadence_hours=168).scan()

        assert second == []
        latest = _last_eval_task()
        assert latest is not None
        assert latest.pk == prior.pk
        assert latest.status == Task.Status.PENDING

    def test_claimed_task_blocks_new_queueing(self) -> None:
        _scanner(cadence_hours=168).scan()
        prior = _last_eval_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.CLAIMED)
        _backdate_task(prior, hours=336)

        second = _scanner(cadence_hours=168).scan()

        assert second == []

    def test_queued_task_directs_scoped_subscription_runner(self) -> None:
        """The execution_reason must direct the SCOPED, no-API-key local runner.

        The dispatched skill reads ``execution_reason``; the directive must
        name ``t3 eval run`` and the subscription backend so the local run
        spends no API budget — mirroring what the user runs by hand.
        """
        _scanner().scan()

        task = _last_eval_task()
        assert task is not None
        reason = task.execution_reason
        assert "t3 eval run" in reason
        assert "subscription" in reason

    def test_signal_summary_mentions_overlay(self) -> None:
        signals = _scanner().scan()
        assert len(signals) == 1
        assert TEST_OVERLAY_NAME in signals[0].summary

    def test_scanner_uses_injected_overlay_not_a_literal(self) -> None:
        custom_overlay = "fictional-core-overlay"
        signals = _scanner(overlay_name=custom_overlay).scan()

        assert len(signals) == 1
        assert signals[0].payload["overlay"] == custom_overlay
        task = _last_eval_task(overlay_name=custom_overlay)
        assert task is not None
        assert task.ticket.overlay == custom_overlay
        assert not Task.objects.filter(ticket__overlay="teatree", phase=EVAL_LOCAL_PHASE).exists()


class EvalLocalWiringTests(TestCase):
    """The tick-job builder reads teatree-core config for the eval-local scanner."""

    def _settings(self, **overrides: object) -> UserSettings:
        return UserSettings(**overrides)

    def test_default_core_config_builds_scanner(self) -> None:
        from teatree.loop.tick_jobs import _eval_local_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type("Cfg", (), {"user": self._settings()})(),
        ):
            scanner = _eval_local_scanner()
        assert scanner is not None
        assert scanner.skill == "eval"
        assert scanner.cadence_hours == 168

    def test_disabled_in_core_config_skips_wiring(self) -> None:
        from teatree.loop.tick_jobs import _eval_local_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type("Cfg", (), {"user": self._settings(eval_local_disabled=True)})(),
        ):
            scanner = _eval_local_scanner()
        assert scanner is None

    def test_tuned_core_config_propagates_to_scanner_kwargs(self) -> None:
        from teatree.loop.tick_jobs import _eval_local_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type(
                "Cfg",
                (),
                {"user": self._settings(eval_local_skill="custom-eval", eval_local_cadence_hours=72)},
            )(),
        ):
            scanner = _eval_local_scanner()
        assert scanner is not None
        assert scanner.skill == "custom-eval"
        assert scanner.cadence_hours == 72

    def test_wiring_resolves_overlay_name_from_discovery(self) -> None:
        from teatree.config import OverlayEntry  # noqa: PLC0415
        from teatree.loop.tick_jobs import _eval_local_scanner  # noqa: PLC0415

        discovered = OverlayEntry(name="t3-teatree", overlay_class="")
        with (
            patch(
                "teatree.loop.tick_jobs.load_config",
                return_value=type("Cfg", (), {"user": self._settings()})(),
            ),
            patch("teatree.loop.tick_jobs.discover_active_overlay", return_value=discovered),
        ):
            scanner = _eval_local_scanner()
        assert scanner is not None
        assert scanner.overlay_name == "t3-teatree"

    def test_wiring_falls_back_to_canonical_when_no_overlay_discovered(self) -> None:
        from teatree.loop.tick_jobs import _eval_local_scanner  # noqa: PLC0415

        with (
            patch(
                "teatree.loop.tick_jobs.load_config",
                return_value=type("Cfg", (), {"user": self._settings()})(),
            ),
            patch("teatree.loop.tick_jobs.discover_active_overlay", return_value=None),
        ):
            scanner = _eval_local_scanner()
        assert scanner is not None
        assert scanner.overlay_name == "t3-teatree"

    def test_default_jobs_includes_eval_local_global_scanner(self) -> None:
        """The global build wires the eval-local scanner as overlay='' by default."""
        from teatree.loop.tick_jobs import build_default_jobs  # noqa: PLC0415

        jobs = build_default_jobs()
        assert any(job.scanner.name == "eval_local" and job.overlay == "" for job in jobs)
