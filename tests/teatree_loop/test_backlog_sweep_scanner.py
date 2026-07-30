"""DB-backed tests for ``BacklogSweepScanner`` (#2419).

The scanner periodically queues a ``backlog_sweep`` ``Task`` row for the
active core overlay on a single trigger: cadence
(``backlog_sweep_cadence_hours``, default 168h = weekly). It mirrors
:mod:`teatree.loop.scanners.scanning_news` — a once-per-cadence platform
behaviour, not coupled to delivery velocity.

The sweep is destructive-capable (it can propose closing issues), so two
safety properties are baked in from day one (the ``t3:sweeping-tickets``
skill § "Scheduling via the loop"):

* **Default-OFF.** ``backlog_sweep_disabled`` defaults *true* — unlike the
    always-on news/eval scanners, the kill switch ships ON so the scanner
    is inert until the user opts in.
* **Ask-gate in the directive.** The queued task carries an ASK-GATE
    marker so the dispatched sweep records proposals and surfaces the batch
    for approval — it never mass-closes unattended.

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
from teatree.loop.scanners.backlog_sweep import BACKLOG_SWEEP_PHASE, BacklogSweepScanner

#: Test overlay anchor — a non-legacy name distinct from any literal the
#: scanner could plausibly hardcode, so a regression that re-bakes a
#: specific overlay name will fail loudly.
TEST_OVERLAY_NAME = "t3-teatree"


def _scanner(
    *,
    overlay_name: str = TEST_OVERLAY_NAME,
    skill: str = "sweeping-tickets",
    cadence_hours: int = 168,
    require_approval: bool = True,
) -> BacklogSweepScanner:
    return BacklogSweepScanner(
        overlay_name=overlay_name,
        skill=skill,
        cadence_hours=cadence_hours,
        require_approval=require_approval,
    )


def _last_sweep_task(overlay_name: str = TEST_OVERLAY_NAME) -> Task | None:
    return (
        Task.objects.filter(
            ticket__overlay=overlay_name,
            phase=BACKLOG_SWEEP_PHASE,
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


class BacklogSweepScannerTests(TestCase):
    def test_no_prior_run_queues_task(self) -> None:
        """First-ever run queues exactly one task (bootstrap trigger)."""
        signals = _scanner().scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "backlog_sweep.queued"
        assert signal.payload["overlay"] == TEST_OVERLAY_NAME
        assert signal.payload["skill"] == "sweeping-tickets"
        assert signal.payload["phase"] == BACKLOG_SWEEP_PHASE
        assert signal.payload["trigger"] == "bootstrap"

        task = _last_sweep_task()
        assert task is not None
        assert task.phase == BACKLOG_SWEEP_PHASE
        assert task.status == Task.Status.PENDING
        assert task.ticket.overlay == TEST_OVERLAY_NAME

    def test_cadence_elapsed_queues_new_task(self) -> None:
        """A prior run older than cadence_hours triggers a new task."""
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_sweep_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate_task(prior, hours=169)

        second = _scanner(cadence_hours=168).scan()

        assert len(second) == 1
        assert second[0].payload["trigger"] == "cadence"
        task = _last_sweep_task()
        assert task is not None
        assert task.pk != prior.pk

    def test_cadence_not_elapsed_no_task(self) -> None:
        """A recent run within the cadence window blocks new queueing."""
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_sweep_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        # 1 hour ago — far inside the 168-hour window.
        _backdate_task(prior, hours=1)

        second = _scanner(cadence_hours=168).scan()

        assert second == []
        latest = _last_sweep_task()
        assert latest is not None
        assert latest.pk == prior.pk

    def test_pending_task_blocks_new_queueing(self) -> None:
        """A still-PENDING sweep task suppresses dupes even after cadence elapses."""
        first = _scanner(cadence_hours=168).scan()
        assert len(first) == 1
        prior = _last_sweep_task()
        assert prior is not None
        # Leave it PENDING and backdate so cadence WOULD trigger.
        _backdate_task(prior, hours=336)

        second = _scanner(cadence_hours=168).scan()

        assert second == []
        latest = _last_sweep_task()
        assert latest is not None
        assert latest.pk == prior.pk
        assert latest.status == Task.Status.PENDING

    def test_claimed_task_blocks_new_queueing(self) -> None:
        """A CLAIMED (in-flight) sweep task is treated as pending — no dupes."""
        _scanner(cadence_hours=168).scan()
        prior = _last_sweep_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.CLAIMED)
        _backdate_task(prior, hours=336)

        second = _scanner(cadence_hours=168).scan()

        assert second == []

    def test_queued_task_carries_skill_name(self) -> None:
        """The skill name lands in the Task's execution_reason for the dispatcher."""
        scanner = _scanner(skill="backlog-sweep-custom")
        scanner.scan()

        task = _last_sweep_task()
        assert task is not None
        assert "backlog-sweep-custom" in task.execution_reason

    def test_signal_summary_is_concise(self) -> None:
        """Statusline-friendly: one-line summary mentioning the overlay anchor."""
        signals = _scanner().scan()
        assert len(signals) == 1
        assert TEST_OVERLAY_NAME in signals[0].summary

    def test_scanner_does_not_hardcode_overlay(self) -> None:
        """The scanner uses the injected overlay, not a baked literal.

        With an arbitrary non-canonical ``overlay_name`` injected at
        construction time, every row, signal payload, and summary must
        reference that name — never the canonical default literal.
        """
        custom_overlay = "fictional-core-overlay"
        signals = _scanner(overlay_name=custom_overlay).scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.payload["overlay"] == custom_overlay
        assert custom_overlay in signal.summary
        task = _last_sweep_task(overlay_name=custom_overlay)
        assert task is not None
        assert task.ticket.overlay == custom_overlay
        assert task.session.overlay == custom_overlay
        # No stale canonical-anchored ticket was created as a side effect.
        assert not Task.objects.filter(ticket__overlay="t3-teatree", phase=BACKLOG_SWEEP_PHASE).exists()


class BacklogSweepAskGateTests(TestCase):
    """The queued task must carry the ask-gate directive by default (#2419).

    Backlog-sweep can close issues — a colleague-visible write under the
    user's identity. The ask-gate threads ``require_approval`` (default
    true, from ``ask_before_backlog_sweep_closes``) into the task so the
    dispatched skill records close proposals and surfaces the batch for
    explicit approval instead of mass-closing unattended. Only the
    high-confidence merged-PR-superseded class auto-closes (the skill's
    own discipline).
    """

    def test_default_task_carries_ask_gate_directive(self) -> None:
        """Default scan → queued task forbids unattended closes, requires approval."""
        signals = _scanner().scan()
        assert len(signals) == 1
        assert signals[0].payload["require_approval"] is True

        task = _last_sweep_task()
        assert task is not None
        reason = task.execution_reason
        # The dispatched skill reads execution_reason — the gate must be
        # explicit there, not just in the in-memory signal payload.
        assert "ASK-GATE" in reason
        assert "do NOT mass-close" in reason

    def test_ask_gate_directive_routes_closes_through_bulk_close(self) -> None:
        """Auto-closes must go through the gated `ticket bulk-close` command (#1931).

        The no-bulk-close gate only protects the `ticket bulk-close` CLI path;
        a raw per-item `ticket ignore` loop bypasses it. The directive routes
        the sweep's autonomous close path through the gated command so an
        over-threshold autonomous close is refused the same as a manual one.
        """
        _scanner().scan()
        task = _last_sweep_task()
        assert task is not None
        reason = task.execution_reason
        assert "ticket bulk-close" in reason
        assert "never a raw per-item `ticket ignore` loop" in reason

    def test_approval_disabled_omits_gate_directive(self) -> None:
        """Opt-out (ask_before_backlog_sweep_closes=false) → no gate directive."""
        signals = _scanner(require_approval=False).scan()
        assert len(signals) == 1
        assert signals[0].payload["require_approval"] is False

        task = _last_sweep_task()
        assert task is not None
        assert "ASK-GATE" not in task.execution_reason
        # The skill name is still present so the dispatcher routes correctly.
        assert "sweeping-tickets" in task.execution_reason


class BacklogSweepWiringTests(TestCase):
    """Confirm the tick-job builder reads core config (#2419).

    The backlog-sweep scanner is a single global scanner (``overlay=""``)
    keyed off teatree-core platform config. Unlike news/eval, the kill
    switch ``backlog_sweep_disabled`` defaults ON (default-OFF scanner) —
    the sweep is destructive-capable, so it stays inert until the user
    opts in.
    """

    def _patched_settings(self, **overrides: object) -> UserSettings:
        return UserSettings(**overrides)

    def test_default_core_config_skips_wiring(self) -> None:
        """Default core config (disabled=True) → wiring produces NO scanner."""
        from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type("Cfg", (), {"user": self._patched_settings()})(),
        ):
            scanner = _backlog_sweep_scanner()
        assert scanner is None

    def test_enabled_in_core_config_builds_scanner(self) -> None:
        """Opt-in: ``backlog_sweep_disabled = False`` → wiring produces a scanner."""
        from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type(
                "Cfg",
                (),
                {"user": self._patched_settings(backlog_sweep_disabled=False)},
            )(),
        ):
            scanner = _backlog_sweep_scanner()
        assert scanner is not None
        assert scanner.skill == "sweeping-tickets"
        assert scanner.cadence_hours == 168

    def test_core_config_propagates_to_scanner_kwargs(self) -> None:
        """Tuned core config flows through to the scanner kwargs."""
        from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type(
                "Cfg",
                (),
                {
                    "user": self._patched_settings(
                        backlog_sweep_disabled=False,
                        backlog_sweep_skill="custom-sweep",
                        backlog_sweep_cadence_hours=72,
                    ),
                },
            )(),
        ):
            scanner = _backlog_sweep_scanner()
        assert scanner is not None
        assert scanner.skill == "custom-sweep"
        assert scanner.cadence_hours == 72

    def test_ask_gate_defaults_on_in_wiring(self) -> None:
        """Default opt-in config wires the scanner with require_approval=True."""
        from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type(
                "Cfg",
                (),
                {"user": self._patched_settings(backlog_sweep_disabled=False)},
            )(),
        ):
            scanner = _backlog_sweep_scanner()
        assert scanner is not None
        assert scanner.require_approval is True

    def test_ask_gate_opt_out_propagates_in_wiring(self) -> None:
        """ask_before_backlog_sweep_closes=false flows to require_approval=False."""
        from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type(
                "Cfg",
                (),
                {
                    "user": self._patched_settings(
                        backlog_sweep_disabled=False,
                        ask_before_backlog_sweep_closes=False,
                    ),
                },
            )(),
        ):
            scanner = _backlog_sweep_scanner()
        assert scanner is not None
        assert scanner.require_approval is False

    def test_wiring_resolves_overlay_name_from_discovery(self) -> None:
        """Wiring layer reads overlay name from ``discover_active_overlay``."""
        from teatree.config import OverlayEntry  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415

        discovered = OverlayEntry(name="t3-teatree", overlay_class="")
        with (
            patch(
                "teatree.loop.global_scanner_factories.load_config",
                return_value=type(
                    "Cfg",
                    (),
                    {"user": self._patched_settings(backlog_sweep_disabled=False)},
                )(),
            ),
            patch(
                "teatree.loop.global_scanner_factories.discover_active_overlay",
                return_value=discovered,
            ),
        ):
            scanner = _backlog_sweep_scanner()
        assert scanner is not None
        assert scanner.overlay_name == "t3-teatree"

    def test_wiring_falls_back_to_canonical_when_no_overlay_discovered(self) -> None:
        """Defensive default — no installed overlay still anchors the canonical name."""
        from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415

        with (
            patch(
                "teatree.loop.global_scanner_factories.load_config",
                return_value=type(
                    "Cfg",
                    (),
                    {"user": self._patched_settings(backlog_sweep_disabled=False)},
                )(),
            ),
            patch(
                "teatree.loop.global_scanner_factories.discover_active_overlay",
                return_value=None,
            ),
        ):
            scanner = _backlog_sweep_scanner()
        assert scanner is not None
        assert scanner.overlay_name == "t3-teatree"
