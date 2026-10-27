import datetime as dt
from dataclasses import dataclass, replace
from typing import ClassVar
from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.admission.pr_start_cap import OPEN_PR_START_CAP
from teatree.core.management.commands.loop_self_improve import _deliver_owner_wait, _owner_wait
from teatree.core.models import BotPing, IncomingEvent, PullRequest, SelfImproveFiring, Task, Ticket
from teatree.core.notify_types import NotifyOutcome, NotifyReason
from teatree.loop.self_improve import (
    ActionRung,
    BudgetVerdict,
    DetectorReport,
    Tier,
    record_firing,
    run_action_ladder,
    run_tier,
)
from teatree.loop.self_improve import actions as actions_module
from teatree.loop.self_improve.detectors.base import DetectorScan
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.repair_first import OwnerAlertDelivery, OwnerWaitChannel, RepairMarker
from teatree.loop.self_improve.schedule import DeliveryRoutes
from tests.factories import TaskFactory


@dataclass
class _Detector:
    name: ClassVar[str] = "repair_first_test"
    tier: ClassVar[str] = Tier.CHEAP
    severity: ClassVar[str] = "warn"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False
    report: DetectorReport | None
    complete: bool = True

    def detect_checked(self) -> DetectorScan:
        return DetectorScan([self.report] if self.report is not None else [], complete=self.complete)

    def detect(self) -> list[DetectorReport]:
        return self.detect_checked().reports

    def scan(self) -> list[object]:
        return []


class _AlwaysOnDetector(_Detector):
    always_on: ClassVar[bool] = True


class _TwoIncidentDetector(_Detector):
    def detect_checked(self) -> DetectorScan:
        assert self.report is not None
        return DetectorScan([self.report, replace(self.report, dedup_key="repair_first_test::other")])


def _incident(kind: str = "repairable_incident", *, state_hash: str = "first") -> DetectorReport:
    return DetectorReport(
        detector="repair_first_test",
        dedup_key="repair_first_test::incident",
        state_hash=state_hash,
        severity="warn",
        max_rung=ActionRung.TICKET,
        summary="A repairable incident",
        payload={"kind": kind},
        requested_rung=ActionRung.SLACK,
    )


def _detector_now() -> dt.datetime:
    return timezone.now()


class RepairFirstTests(TestCase):
    def test_real_lifecycle_incident_alerts_after_wait_while_pr_cap_holds(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        for index in range(OPEN_PR_START_CAP):
            PullRequest.objects.record_opened(
                ticket=Ticket.objects.create(),
                url=f"https://github.com/example-org/example-repo/pull/{index + 1600}",
            )
        start = timezone.now()
        messages: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda text, _key: (messages.append(text), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=60),
            ),
        )
        detector = LifecycleIncidentDetector(now=_detector_now, overlay_name="t3-teatree")
        outcomes = []
        for minutes in (0, 31, 60, 90):
            with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=minutes)):
                outcomes.append(
                    run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=routes)
                )
        firing = SelfImproveFiring.objects.get(detector="lifecycle_incident")
        assert firing.ticket is None
        assert RepairMarker.from_payload(firing.payload) is not None
        assert [len(result.owner_waits) for result in outcomes] == [0, 0, 1, 0]
        assert len(messages) == 1
        assert "ticket deferred" in messages[0]
        pr = PullRequest.objects.order_by("pk").first()
        assert pr is not None
        pr.mark_closed()
        pr.save(update_fields=["state"])
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=91)):
            resumed = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=routes)
        assert resumed.actions[0].firing.ticket_id is not None
        assert resumed.owner_waits == []

    def test_ticket_creation_failure_records_waiting_incident_and_alerts_once(self) -> None:
        start = timezone.now()
        messages: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda text, _key: (messages.append(text), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        with patch.object(
            actions_module, "_record_ticket_followup", side_effect=ImproperlyConfigured("owner unavailable")
        ):
            outcomes = []
            for minutes in (0, 30, 60):
                with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=minutes)):
                    outcomes.append(
                        run_tier(
                            Tier.CHEAP,
                            detectors=[_Detector(_incident())],
                            budget=BudgetVerdict.allow(),
                            delivery=routes,
                        )
                    )
        firing = SelfImproveFiring.objects.get(detector="repair_first_test")
        assert firing.ticket is None
        assert RepairMarker.from_payload(firing.payload) is not None
        assert [len(result.owner_waits) for result in outcomes] == [0, 1, 0]
        assert len(messages) == 1

    def test_open_repair_incident_keeps_first_wait_start_on_statusline_rerecord(self) -> None:
        repair_ticket = Ticket.objects.create(overlay="t3-teatree", extra={"source": "self_improve"})
        repair_task = TaskFactory(ticket=repair_ticket)
        repair_task.fail(reason="ProcessError: repair worker exited", by_holder=True)
        detector = LifecycleIncidentDetector(now=_detector_now, overlay_name="t3-teatree")
        start = timezone.now()
        messages: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda text, _key: (messages.append(text), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=60),
            ),
        )
        for minutes in (0, 5, 31, 60):
            with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=minutes)):
                result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=routes)
            if minutes == 5:
                marker = RepairMarker.from_payload(SelfImproveFiring.objects.get().payload)
                assert marker is not None
                assert marker.began_at == start
            if minutes == 60:
                assert len(result.owner_waits) == 1
        assert len(messages) == 1

    def test_wait_setting_rejects_invalid_values(self) -> None:
        assert _owner_wait(30) == dt.timedelta(minutes=30)
        assert _owner_wait(1440) == dt.timedelta(days=1)
        for invalid in (True, "30", 0, -1, 2.5, None, 1441, 10**13):
            with self.subTest(value=invalid), self.assertLogs("teatree.config.settings_loop_owned", level="ERROR"):
                assert _owner_wait(invalid) == dt.timedelta(minutes=60)

    def test_slack_rung_starts_repair_without_owner_message(self) -> None:
        result = run_action_ladder(_incident(), overlay_name="t3-teatree", owner_alert=lambda *_: True)

        assert result is not None
        assert result.rung == ActionRung.TICKET
        firing = SelfImproveFiring.objects.get(pk=result.firing.pk)
        assert firing.ticket is not None
        assert firing.ticket.kind == Ticket.Kind.FIX
        assert Task.objects.filter(ticket=firing.ticket, phase="planning").exists()
        assert RepairMarker.from_payload(firing.payload) is not None

    def test_one_delayed_message_names_repair_and_task(self) -> None:
        start = timezone.now()
        deliveries: list[tuple[str, str]] = []
        channel = OwnerWaitChannel(
            deliver=lambda text, key: (deliveries.append((text, key)), OwnerAlertDelivery(sent=True))[1],
            quiet_now=lambda: False,
            wait=dt.timedelta(minutes=60),
        )
        routes = DeliveryRoutes(overlay_name="t3-teatree", owner_wait=channel)
        with patch("django.utils.timezone.now", return_value=start):
            first = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        assert first.actions[0].rung == ActionRung.TICKET
        assert deliveries == []
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=59)):
            early = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        assert early.owner_waits == []
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=60)):
            due = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=90)):
            later = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        assert len(due.owner_waits) == 1
        assert later.owner_waits == []
        assert len(deliveries) == 1
        assert f"Repair: ticket {first.actions[0].firing.ticket.pk}" in deliveries[0][0]
        assert "planning pending" in deliveries[0][0]
        assert deliveries[0][1].startswith("self-improve:still-open:")

    def test_delayed_message_uses_real_push_egress_as_own_root(self) -> None:
        backend = MagicMock()
        backend.open_dm.return_value = "D-OWNER"
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000001"}
        backend.get_permalink.return_value = "https://example.slack.com/archives/D-OWNER/p1700000000000001"
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref="D-OWNER",
            thread_ref="1699999999.000001",
            idempotency_key="slack:active-owner-thread",
        )
        start = timezone.now()
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=_deliver_owner_wait,
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=60),
            ),
        )
        with (
            patch.object(notify_module, "resolve_owner_dm_backend", return_value=(backend, NotifyReason.NONE)),
            patch.object(notify_module, "resolve_user_id", return_value="U-OWNER"),
            patch("django.utils.timezone.now", return_value=start),
        ):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        with (
            patch.object(notify_module, "resolve_owner_dm_backend", return_value=(backend, NotifyReason.NONE)),
            patch.object(notify_module, "resolve_user_id", return_value="U-OWNER"),
            patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=60)),
        ):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        assert backend.post_message.call_count == 1
        assert backend.post_message.call_args.kwargs["thread_ts"] == ""
        assert BotPing.objects.filter(status=BotPing.Status.SENT).count() == 1

    def test_existing_delivery_receipt_completes_wait_marker(self) -> None:
        start = timezone.now()
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=_deliver_owner_wait,
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        with patch("django.utils.timezone.now", return_value=start):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        with (
            patch.object(
                notify_module,
                "notify_user_outcome",
                return_value=NotifyOutcome(sent=False, reason=NotifyReason.ALREADY_SENT),
            ),
            patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=30)),
        ):
            due = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        assert len(due.owner_waits) == 1
        marker = RepairMarker.from_payload(SelfImproveFiring.objects.get().payload)
        assert marker is not None
        assert marker.notified_at is not None

    def test_resolved_or_unconfirmed_incident_is_not_messaged(self) -> None:
        start = timezone.now()
        deliveries: list[str] = []
        channel = OwnerWaitChannel(
            deliver=lambda _text, key: (deliveries.append(key), OwnerAlertDelivery(sent=True))[1],
            quiet_now=lambda: False,
            wait=dt.timedelta(minutes=30),
        )
        routes = DeliveryRoutes(overlay_name="t3-teatree", owner_wait=channel)
        with patch("django.utils.timezone.now", return_value=start):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=31)):
            run_tier(Tier.CHEAP, detectors=[_Detector(None)], budget=BudgetVerdict.skip("budget"), delivery=routes)
            run_tier(
                Tier.CHEAP, detectors=[_Detector(None, complete=False)], budget=BudgetVerdict.allow(), delivery=routes
            )
            run_tier(Tier.CHEAP, detectors=[_Detector(None)], budget=BudgetVerdict.allow(), delivery=routes)
        assert deliveries == []
        assert SelfImproveFiring.objects.get(detector="repair_first_test").resolved_at is not None

    def test_covered_absence_resolves_before_owner_wait_sweep(self) -> None:
        start = timezone.now()
        deliveries: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda text, _key: (deliveries.append(text), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        with patch("django.utils.timezone.now", return_value=start):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=31)):
            due = run_tier(Tier.CHEAP, detectors=[_Detector(None)], budget=BudgetVerdict.allow(), delivery=routes)
        assert SelfImproveFiring.objects.get(detector="repair_first_test").resolved_at is not None
        assert due.owner_waits == []
        assert deliveries == []

    def test_reopened_incident_resets_wait_generation(self) -> None:
        start = timezone.now()
        deliveries: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda _text, key: (deliveries.append(key), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        with patch("django.utils.timezone.now", return_value=start):
            first = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=10)):
            run_tier(Tier.CHEAP, detectors=[_Detector(None)], budget=BudgetVerdict.allow(), delivery=routes)
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=20)):
            reopened = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=31)):
            early = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=50)):
            due = run_tier(
                Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        assert reopened.actions[0].firing.pk == first.actions[0].firing.pk
        assert early.owner_waits == []
        assert len(due.owner_waits) == 1
        assert len(deliveries) == 1

    def test_reopened_incident_after_a_message_gets_a_second_message_under_a_new_key(self) -> None:
        start = timezone.now()
        keys: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda _text, key: (keys.append(key), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        for minutes, report in ((0, _incident()), (30, _incident()), (40, None), (50, _incident()), (80, _incident())):
            with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=minutes)):
                run_tier(Tier.CHEAP, detectors=[_Detector(report)], budget=BudgetVerdict.allow(), delivery=routes)
        assert len(keys) == 2
        assert keys[0] != keys[1]

    def test_incidents_begun_together_get_distinct_message_keys(self) -> None:
        start = timezone.now()
        keys: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda _text, key: (keys.append(key), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        for minutes in (0, 30):
            with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=minutes)):
                run_tier(
                    Tier.CHEAP,
                    detectors=[_TwoIncidentDetector(_incident())],
                    budget=BudgetVerdict.allow(),
                    delivery=routes,
                )
        assert len(keys) == 2
        assert len(set(keys)) == 2

    def test_red_budget_still_sweeps_confirmed_always_on_incident(self) -> None:
        start = timezone.now()
        deliveries: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda _text, key: (deliveries.append(key), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        with patch("django.utils.timezone.now", return_value=start):
            run_tier(
                Tier.CHEAP, detectors=[_AlwaysOnDetector(_incident())], budget=BudgetVerdict.allow(), delivery=routes
            )
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=30)):
            red = run_tier(
                Tier.CHEAP,
                detectors=[_AlwaysOnDetector(_incident())],
                budget=BudgetVerdict.skip("ram"),
                delivery=routes,
            )
        assert red.skipped
        assert len(red.owner_waits) == 1
        assert len(deliveries) == 1

    def test_failed_action_cannot_confirm_a_delayed_message(self) -> None:
        start = timezone.now()
        deliveries: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda _text, key: (deliveries.append(key), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        with patch("django.utils.timezone.now", return_value=start):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        with (
            patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=30)),
            patch("teatree.loop.self_improve.schedule.run_action_ladder", side_effect=RuntimeError),
        ):
            failed = run_tier(
                Tier.CHEAP,
                detectors=[_Detector(_incident(state_hash="changed"))],
                budget=BudgetVerdict.allow(),
                delivery=routes,
            )
        assert failed.degraded_scans == [("repair_first_test", "detector_raised")]
        assert failed.owner_waits == []
        assert deliveries == []

    def test_probe_inert_keeps_immediate_message_and_repair_kind_defers(self) -> None:
        calls: list[str] = []
        probe = run_action_ladder(
            _incident("ram_probe_inert"),
            owner_alert=lambda *_: (calls.append("sent"), True)[1],
        )
        assert probe is not None
        assert probe.rung == ActionRung.SLACK
        assert probe.firing.ticket is None
        assert calls == ["sent"]
        probe.firing.delete()
        repair = run_action_ladder(_incident("repair_task_failed"), overlay_name="t3-teatree")
        assert repair is not None
        assert repair.rung == ActionRung.STATUSLINE
        assert repair.firing.ticket is None
        assert RepairMarker.from_payload(repair.firing.payload) is not None

    def test_repair_of_repair_waits_then_notifies_without_second_ticket(self) -> None:
        start = timezone.now()
        messages: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda text, _key: (messages.append(text), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        report = _incident("repair_task_failed")
        with patch("django.utils.timezone.now", return_value=start):
            first = run_tier(Tier.CHEAP, detectors=[_Detector(report)], budget=BudgetVerdict.allow(), delivery=routes)
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=30)):
            due = run_tier(Tier.CHEAP, detectors=[_Detector(report)], budget=BudgetVerdict.allow(), delivery=routes)
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=60)):
            later = run_tier(Tier.CHEAP, detectors=[_Detector(report)], budget=BudgetVerdict.allow(), delivery=routes)
        assert [action.rung for action in first.actions] == [ActionRung.STATUSLINE]
        assert Ticket.objects.count() == 0
        assert len(due.owner_waits) == 1
        assert later.owner_waits == []
        assert len(messages) == 1
        assert "no second ticket" in messages[0]

    def test_legacy_firing_is_never_swept_without_marker(self) -> None:
        start = timezone.now()
        record_firing(_incident(), action=ActionRung.STATUSLINE, now=start)
        deliveries: list[str] = []
        routes = DeliveryRoutes(
            overlay_name="t3-teatree",
            owner_wait=OwnerWaitChannel(
                deliver=lambda _text, key: (deliveries.append(key), OwnerAlertDelivery(sent=True))[1],
                quiet_now=lambda: False,
                wait=dt.timedelta(minutes=30),
            ),
        )
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(hours=2)):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        assert deliveries == []

    def test_quiet_hours_and_failed_delivery_retry_same_generation(self) -> None:
        start = timezone.now()
        quiet = True
        keys: list[str] = []

        def deliver(_text: str, key: str) -> OwnerAlertDelivery:
            keys.append(key)
            return OwnerAlertDelivery(sent=len(keys) > 1)

        channel = OwnerWaitChannel(deliver=deliver, quiet_now=lambda: quiet, wait=dt.timedelta(minutes=30))
        routes = DeliveryRoutes(overlay_name="t3-teatree", owner_wait=channel)
        with patch("django.utils.timezone.now", return_value=start):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=30)):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        assert keys == []
        quiet = False
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=31)):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        with patch("django.utils.timezone.now", return_value=start + dt.timedelta(minutes=32)):
            run_tier(Tier.CHEAP, detectors=[_Detector(_incident())], budget=BudgetVerdict.allow(), delivery=routes)
        assert len(keys) == 2
        assert keys[0] == keys[1]
