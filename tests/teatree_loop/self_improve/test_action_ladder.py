"""Structural + behavioural tests for the self-improve action ladder.

The ``test_no_auto_fix_outside_whitelist`` test enumerates every Phase 1
detector and asserts ``auto_fix`` is ``True`` only for the
``StaleStatuslineEntryDetector`` per BLUEPRINT § 5.7.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.management.commands.loop_self_improve import _deliver_owner_alert
from teatree.core.models import BotPing, SelfImproveFiring, Ticket
from teatree.core.notify_types import DELIVERED, NotifyReason, blocked
from teatree.loop.self_improve import (
    SLACK_RATE_CAP_SECONDS,
    ActionRung,
    DetectorReport,
    record_firing,
    run_action_ladder,
)
from teatree.loop.self_improve.detectors import (
    ALL_PHASE_1_DETECTORS,
    DispatchGapDetector,
    ForgottenMergeDetector,
    LifecycleIncidentDetector,
    PressureIncidentDetector,
    SkillAssuranceGapDetector,
    StaleStatuslineEntryDetector,
    TelemetryActionGapDetector,
)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _report(  # noqa: PLR0913  # test helper: each kwarg maps 1:1 to a DetectorReport field.
    *,
    detector: str = "dispatch_gap",
    dedup_key: str = "dispatch_gap::global",
    state_hash_value: str = "h1",
    severity: str = "warn",
    max_rung: str = ActionRung.TICKET,
    auto_fix: bool = False,
    requested_rung: str = ActionRung.STATUSLINE,
) -> DetectorReport:
    return DetectorReport(
        detector=detector,
        dedup_key=dedup_key,
        state_hash=state_hash_value,
        severity=severity,
        max_rung=max_rung,
        summary="test report",
        payload={"slack_channel": "C123"},
        auto_fix=auto_fix,
        requested_rung=requested_rung,
    )


class ActionLadderStructuralTests(TestCase):
    """Phase 1 structural invariants — auto-fix whitelist."""

    def test_no_auto_fix_outside_whitelist(self) -> None:
        """``auto_fix=True`` must appear on exactly StaleStatuslineEntryDetector."""
        observed: dict[str, bool] = {}
        for detector_cls in ALL_PHASE_1_DETECTORS:
            observed[detector_cls.__name__] = bool(detector_cls.auto_fix)
        assert observed == {
            DispatchGapDetector.__name__: False,
            ForgottenMergeDetector.__name__: False,
            LifecycleIncidentDetector.__name__: False,
            PressureIncidentDetector.__name__: False,
            SkillAssuranceGapDetector.__name__: False,
            StaleStatuslineEntryDetector.__name__: True,
            TelemetryActionGapDetector.__name__: False,
        }

    def test_ladder_constants_match_model_choices(self) -> None:
        """Sanity: every ``ActionRung`` constant has a model choice."""
        valid = {choice.value for choice in SelfImproveFiring.Action}
        assert ActionRung.LOG in valid
        assert ActionRung.STATUSLINE in valid
        assert ActionRung.SLACK in valid
        assert ActionRung.TICKET in valid
        assert ActionRung.AUTO_FIX in valid


class ActionLadderBehaviourTests(TestCase):
    """Action-ladder run behaviour (rung resolution, dedup, Slack cap)."""

    def test_first_fire_records_statusline_rung(self) -> None:
        result = run_action_ladder(_report())
        assert result is not None
        assert result.rung == ActionRung.STATUSLINE
        assert SelfImproveFiring.objects.count() == 1

    def test_dedup_within_same_state_hash_returns_none(self) -> None:
        report = _report()
        run_action_ladder(report)
        # Same dedup_key + same state_hash => suppressed (cool-down).
        result = run_action_ladder(report)
        assert result is None
        assert SelfImproveFiring.objects.get().action_count == 1

    def test_changed_state_hash_escalates_one_rung(self) -> None:
        first = _report(state_hash_value="h1")
        run_action_ladder(first)
        second = _report(state_hash_value="h2")
        messaging = MagicMock()
        messaging.post_message.return_value = {"ok": True, "ts": "100.1"}
        result = run_action_ladder(second, messaging=messaging)
        assert result is not None
        # statusline → slack is the escalation when ceiling allows it.
        assert result.rung == ActionRung.SLACK
        messaging.post_message.assert_called_once()

    def test_ceiling_caps_escalation(self) -> None:
        """A statusline-ceiling detector cannot escalate past statusline."""
        first = _report(max_rung=ActionRung.STATUSLINE, state_hash_value="h1")
        run_action_ladder(first)
        second = _report(max_rung=ActionRung.STATUSLINE, state_hash_value="h2")
        messaging = MagicMock()
        result = run_action_ladder(second, messaging=messaging)
        assert result is not None
        assert result.rung == ActionRung.STATUSLINE
        messaging.post_message.assert_not_called()

    def test_slack_cap_downgrades_to_statusline(self) -> None:
        """One slack firing in 30 min ⇒ next slack rung downgrades to statusline."""
        # Seed a slack firing within the cap window.
        seed = _report(detector="forgotten_merge", dedup_key="forgotten_merge::pr1")
        record_firing(seed, action=ActionRung.SLACK)
        # Now try to escalate a different detector to slack.
        first = _report(detector="other", dedup_key="other::1", state_hash_value="h1")
        run_action_ladder(first)
        second = _report(detector="other", dedup_key="other::1", state_hash_value="h2")
        messaging = MagicMock()
        result = run_action_ladder(second, messaging=messaging)
        assert result is not None
        assert result.rung == ActionRung.STATUSLINE
        assert result.slack_capped is True
        messaging.post_message.assert_not_called()

    def test_auto_fix_only_runs_when_detector_opted_in(self) -> None:
        """Even at AUTO_FIX rung, a non-whitelisted detector falls back to statusline."""
        # Walk the ladder to the AUTO_FIX rung for a non-whitelisted report.
        report = _report(max_rung=ActionRung.AUTO_FIX, auto_fix=False, state_hash_value="h1")
        run_action_ladder(report)  # statusline
        run_action_ladder(_report(max_rung=ActionRung.AUTO_FIX, auto_fix=False, state_hash_value="h2"))  # slack
        run_action_ladder(_report(max_rung=ActionRung.AUTO_FIX, auto_fix=False, state_hash_value="h3"))  # ticket
        callable_ = MagicMock()
        result = run_action_ladder(
            _report(max_rung=ActionRung.AUTO_FIX, auto_fix=False, state_hash_value="h4"),
            auto_fix_callable=callable_,
        )
        assert result is not None
        # auto_fix=False ⇒ refuses to execute the callable, downgrades.
        assert result.rung == ActionRung.STATUSLINE
        assert result.auto_fix_executed is False
        callable_.assert_not_called()

    def test_ticket_rung_creates_one_internal_followup_without_posting(self) -> None:
        first = _report(detector="pressure_incident", dedup_key="pressure_incident::load", state_hash_value="h1")
        record_firing(first, action=ActionRung.SLACK)

        result = run_action_ladder(
            _report(detector="pressure_incident", dedup_key="pressure_incident::load", state_hash_value="h2")
        )

        assert result is not None
        assert result.rung == ActionRung.TICKET
        assert result.firing.ticket_id is not None
        ticket = Ticket.objects.get(pk=result.firing.ticket_id)
        assert ticket.issue_url == ""
        assert ticket.overlay == "t3-teatree"
        assert ticket._extra().get("source") == "self_improve"
        assert "pressure_incident::load" in ticket.context
        assert ticket.tasks.filter(phase="planning", status="pending").count() == 1
        assert Ticket.objects.count() == 1

        # A changed observation updates the same firing, never creates a second ticket.
        again = run_action_ladder(
            _report(detector="pressure_incident", dedup_key="pressure_incident::load", state_hash_value="h3")
        )
        assert again is not None
        assert again.firing.ticket_id == ticket.pk
        assert Ticket.objects.count() == 1
        assert ticket.tasks.count() == 1

    def test_severe_pressure_can_open_internal_ticket_on_first_detection(self) -> None:
        result = run_action_ladder(
            _report(
                detector="pressure_incident",
                dedup_key="pressure_incident::memory",
                requested_rung=ActionRung.TICKET,
            )
        )
        assert result is not None
        assert result.rung == ActionRung.TICKET
        assert result.firing.ticket_id is not None
        assert Ticket.objects.count() == 1

    def test_auto_fix_report_jumps_straight_to_auto_fix_on_first_firing(self) -> None:
        """An idempotent self-heal must fire on FIRST observation — no graduated climb.

        A persistent stale smell hashes to the same ``state_hash`` every tick, so the
        old one-rung climb (statusline → … ) was suppressed by cool-down at rung 1 and
        could never reach ``auto_fix``. The auto-fix report now targets its ceiling at
        once (#2625 Part B).
        """
        callable_ = MagicMock()
        result = run_action_ladder(
            _report(max_rung=ActionRung.AUTO_FIX, auto_fix=True, state_hash_value="h1"),
            auto_fix_callable=callable_,
        )
        assert result is not None
        assert result.rung == ActionRung.AUTO_FIX
        assert result.auto_fix_executed is True
        callable_.assert_called_once()

    def test_auto_fix_runs_callable_when_whitelisted(self) -> None:
        # Start at AUTO_FIX rung for a whitelisted detector.
        report = _report(max_rung=ActionRung.AUTO_FIX, auto_fix=True, state_hash_value="h1")
        # Manually seed the firing at TICKET so the next call is AUTO_FIX.
        record_firing(report, action=ActionRung.TICKET)
        callable_ = MagicMock()
        result = run_action_ladder(
            _report(max_rung=ActionRung.AUTO_FIX, auto_fix=True, state_hash_value="h2"),
            auto_fix_callable=callable_,
        )
        assert result is not None
        assert result.rung == ActionRung.AUTO_FIX
        assert result.auto_fix_executed is True
        callable_.assert_called_once()

    def test_slack_cap_constant_is_thirty_minutes(self) -> None:
        """Lock the cap so a refactor cannot loosen the non-negotiable guard."""
        assert SLACK_RATE_CAP_SECONDS == 30 * 60

    def test_unattended_alert_pages_once_per_inert_transition(self) -> None:
        report = DetectorReport(
            detector="pressure_incident",
            dedup_key="pressure_incident::cgroup-probe-inert",
            state_hash="inert",
            severity="critical",
            max_rung=ActionRung.SLACK,
            requested_rung=ActionRung.SLACK,
            summary="cgroup RAM floor is unreadable",
            payload={"kind": "cgroup_probe_inert", "requires_delivery": True},
        )
        with patch("teatree.core.notify.notify_user_outcome", return_value=DELIVERED) as send:
            first = run_action_ladder(report, owner_alert=_deliver_owner_alert)
            duplicate = run_action_ladder(report, owner_alert=_deliver_owner_alert)
            SelfImproveFiring.objects.update(resolved_at=timezone.now())
            recurrence = run_action_ladder(report, owner_alert=_deliver_owner_alert)

        assert first is not None
        assert first.rung == ActionRung.SLACK
        assert duplicate is None
        assert recurrence is not None
        assert recurrence.rung == ActionRung.SLACK
        assert send.call_count == 2
        assert send.call_args_list[0].kwargs["idempotency_key"] != send.call_args_list[1].kwargs["idempotency_key"]

    def test_failed_unattended_alert_does_not_claim_slack_delivery(self) -> None:
        report = DetectorReport(
            detector="pressure_incident",
            dedup_key="pressure_incident::cgroup-probe-inert",
            state_hash="inert",
            severity="critical",
            max_rung=ActionRung.SLACK,
            requested_rung=ActionRung.SLACK,
            summary="cgroup RAM floor is unreadable",
            payload={"kind": "cgroup_probe_inert", "requires_delivery": True},
        )
        with patch(
            "teatree.core.notify.notify_user_outcome",
            return_value=blocked(NotifyReason.DELIVERY_FAILED),
        ) as send:
            assert run_action_ladder(report, owner_alert=_deliver_owner_alert) is None
            assert run_action_ladder(report, owner_alert=_deliver_owner_alert) is None
        assert send.call_count == 2
        assert SelfImproveFiring.objects.count() == 0

    def test_critical_alerts_reach_owner_through_real_push_policy(self) -> None:
        backend = MagicMock()
        backend.open_dm.return_value = "D-OWNER"
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000001"}
        backend.get_permalink.return_value = "https://example.slack.com/archives/D-OWNER/p1700000000000001"
        with (
            patch.object(notify_module, "resolve_owner_dm_backend", return_value=(backend, NotifyReason.NONE)),
            patch.object(notify_module, "resolve_user_id", return_value="U-OWNER"),
        ):
            for kind in ("ram_probe_inert", "disk_probe_inert", "outbound_unposted"):
                with self.subTest(kind=kind):
                    report = DetectorReport(
                        detector="lifecycle_incident" if kind == "outbound_unposted" else "pressure_incident",
                        dedup_key=f"probe::{kind}",
                        state_hash="inert",
                        severity="critical",
                        max_rung=ActionRung.SLACK,
                        requested_rung=ActionRung.SLACK,
                        summary="The factory requires attention",
                        payload={"kind": kind, "requires_delivery": True},
                    )
                    assert _deliver_owner_alert(report, None) is True
                    assert _deliver_owner_alert(report, None) is True

        assert backend.post_message.call_count == 3
        assert BotPing.objects.filter(status=BotPing.Status.SENT).count() == 3
