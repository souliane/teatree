"""DB-backed tests for ``ScanningNewsScanner`` (#1191, #1267).

The scanner periodically queues a ``scanning_news`` ``Task`` row for the
active core overlay on a single trigger: cadence
(``scanning_news_cadence_hours``, default 24h). Unlike the architectural-
review scanner, there is no after-merge backstop — news scanning is a
once-a-day platform behaviour, not coupled to delivery velocity.

The scanner is teatree-CORE and overlay-agnostic in its module: the
overlay-anchor identity is injected at construction time by the wiring
layer (``loop.global_scanner_factories._scanning_news_scanner``), which resolves
:func:`teatree.config.discover_active_overlay`. The queued
:class:`Task` is anchored at a placeholder Ticket carrying that
resolved overlay name so the dispatcher routes through the standard
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
from teatree.loop.scanners.scanning_news import SCANNING_NEWS_PHASE, ScanningNewsScanner

#: Test overlay anchor — a non-legacy name distinct from any literal the
#: scanner could plausibly hardcode, so a regression that re-bakes a
#: specific overlay name will fail loudly.
TEST_OVERLAY_NAME = "t3-teatree"


def _scanner(
    *,
    overlay_name: str = TEST_OVERLAY_NAME,
    skill: str = "scanning-news",
    cadence_hours: int = 24,
    require_approval: bool = True,
) -> ScanningNewsScanner:
    return ScanningNewsScanner(
        overlay_name=overlay_name,
        skill=skill,
        cadence_hours=cadence_hours,
        require_approval=require_approval,
    )


def _last_news_task(overlay_name: str = TEST_OVERLAY_NAME) -> Task | None:
    return (
        Task.objects.filter(
            ticket__overlay=overlay_name,
            phase=SCANNING_NEWS_PHASE,
        )
        .order_by("-id")
        .first()
    )


def _backdate_task(task: Task, *, hours: int) -> None:
    """Move a Task's bookkeeping into the past so the cadence math triggers.

    ``Task`` now has a ``created_at`` (migration 0004), but the scanner
    intentionally keys on ``Session.started_at`` (auto_now_add) as the queue
    time, so we derive the last-run timestamp from there and backdate the
    Session row via ``update()``.
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
        assert signal.payload["overlay"] == TEST_OVERLAY_NAME
        assert signal.payload["skill"] == "scanning-news"
        assert signal.payload["phase"] == SCANNING_NEWS_PHASE
        assert signal.payload["trigger"] == "bootstrap"

        task = _last_news_task()
        assert task is not None
        assert task.phase == SCANNING_NEWS_PHASE
        assert task.status == Task.Status.PENDING
        assert task.ticket.overlay == TEST_OVERLAY_NAME

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
        assert TEST_OVERLAY_NAME in signals[0].summary

    def test_scanner_does_not_hardcode_legacy_teatree_overlay(self) -> None:
        """Regression #1267 — scanner uses the injected overlay, not the literal "teatree".

        Pre-fix the scanner module hardcoded ``SCANNING_NEWS_OVERLAY = "teatree"``
        and wrote that legacy value into every queued task. With an arbitrary
        non-legacy ``overlay_name`` injected at construction time, no row,
        signal payload, or summary may reference the bare literal "teatree".
        """
        custom_overlay = "fictional-core-overlay"
        signals = _scanner(overlay_name=custom_overlay).scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.payload["overlay"] == custom_overlay
        assert custom_overlay in signal.summary
        # The legacy bare literal must not appear anywhere — neither in
        # the signal payload nor in the persisted Task/Ticket/Session rows.
        assert signal.payload["overlay"] != "teatree"
        assert "teatree" not in signal.summary or custom_overlay in signal.summary
        task = _last_news_task(overlay_name=custom_overlay)
        assert task is not None
        assert task.ticket.overlay == custom_overlay
        assert task.session.overlay == custom_overlay
        # And no stale "teatree"-anchored ticket was created as a side effect.
        assert not Task.objects.filter(ticket__overlay="teatree", phase=SCANNING_NEWS_PHASE).exists()


class ScanningNewsAskGateTests(TestCase):
    """The queued task must carry the ask-gate directive by default (#1391).

    Pre-fix the scanner queued a task whose ``execution_reason`` only
    named the trigger + skill — nothing instructed the dispatched skill
    NOT to auto-create issues, and the skill mass-filed
    ``from-news-scan`` tickets without user confirmation (backlog
    pollution). The ask-gate threads ``require_approval`` (default true,
    from ``ask_before_creating_news_tickets``) into the task so the
    skill records ``PendingArticleSuggestion`` candidates for approval
    instead of auto-filing.
    """

    def test_default_task_carries_ask_gate_directive(self) -> None:
        """Default scan → queued task forbids auto-create, requires approval."""
        signals = _scanner().scan()
        assert len(signals) == 1
        assert signals[0].payload["require_approval"] is True

        task = _last_news_task()
        assert task is not None
        reason = task.execution_reason
        # The dispatched skill reads execution_reason — the gate must be
        # explicit there, not just in the in-memory signal payload.
        assert "ASK-GATE" in reason
        assert "do NOT auto-create issues" in reason
        assert "PendingArticleSuggestion" in reason

    def test_approval_disabled_omits_gate_directive(self) -> None:
        """Opt-out (ask_before_creating_news_tickets=false) → no gate directive."""
        signals = _scanner(require_approval=False).scan()
        assert len(signals) == 1
        assert signals[0].payload["require_approval"] is False

        task = _last_news_task()
        assert task is not None
        assert "ASK-GATE" not in task.execution_reason
        # The skill name is still present so the dispatcher routes correctly.
        assert "scanning-news" in task.execution_reason


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
        from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type("Cfg", (), {"user": self._patched_settings()})(),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is not None
        assert scanner.skill == "scanning-news"
        assert scanner.cadence_hours == 24

    def test_disabled_in_core_config_skips_wiring(self) -> None:
        """Escape hatch: ``scanning_news_disabled = True`` → no scanner."""
        from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
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
        from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
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

    def test_ask_gate_defaults_on_in_wiring(self) -> None:
        """#1391 — default core config wires the scanner with require_approval=True."""
        from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type("Cfg", (), {"user": self._patched_settings()})(),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is not None
        assert scanner.require_approval is True

    def test_ask_gate_opt_out_propagates_in_wiring(self) -> None:
        """#1391 — ask_before_creating_news_tickets=false flows to require_approval=False."""
        from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type(
                "Cfg",
                (),
                {"user": self._patched_settings(ask_before_creating_news_tickets=False)},
            )(),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is not None
        assert scanner.require_approval is False

    def test_wiring_resolves_overlay_name_from_discovery(self) -> None:
        """#1267 — wiring layer reads overlay name from ``discover_active_overlay``."""
        from teatree.config import OverlayEntry  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415

        discovered = OverlayEntry(name="t3-teatree", overlay_class="")
        with (
            patch(
                "teatree.loop.global_scanner_factories.load_config",
                return_value=type("Cfg", (), {"user": self._patched_settings()})(),
            ),
            patch(
                "teatree.loop.global_scanner_factories.discover_active_overlay",
                return_value=discovered,
            ),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is not None
        assert scanner.overlay_name == "t3-teatree"

    def test_wiring_falls_back_to_canonical_when_no_overlay_discovered(self) -> None:
        """Defensive default — no installed overlay still queues against the canonical name."""
        from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415

        with (
            patch(
                "teatree.loop.global_scanner_factories.load_config",
                return_value=type("Cfg", (), {"user": self._patched_settings()})(),
            ),
            patch(
                "teatree.loop.global_scanner_factories.discover_active_overlay",
                return_value=None,
            ),
        ):
            scanner = _scanning_news_scanner()
        assert scanner is not None
        # Canonical post-0027 fallback — no bare legacy "teatree".
        assert scanner.overlay_name == "t3-teatree"
