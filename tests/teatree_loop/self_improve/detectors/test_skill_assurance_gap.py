"""Missing and unverified headless skills become bounded, owner-scoped incidents."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import SelfImproveFiring, TaskAttempt, Ticket, TicketTransition
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.detectors.base import ActionRung
from teatree.loop.self_improve.detectors.skill_assurance_gap import SkillAssuranceGapDetector
from teatree.loop.self_improve.persistence import record_firing
from teatree.loop.self_improve.schedule import DeliveryRoutes, Tier, run_tier
from tests.factories import TaskFactory, TicketFactory


def _attempt(
    *, overlay: str, status: str, missing: list[str] | None = None, requested: list[str] | None = None
) -> None:
    task = TaskFactory(ticket=TicketFactory(overlay=overlay))
    TaskAttempt.objects.create(
        task=task,
        exit_code=1 if status == "missing" else 0,
        result={
            "skill_assurance": {
                "requested": ["code"] if requested is None else requested,
                "found": [] if missing else ["code"],
                "injected": [],
                "explicit_load": ["code"],
                "missing": missing or [],
                "evidence": [],
                "status": status,
            }
        },
    )


class SkillAssuranceGapTests(TestCase):
    def test_expired_attempt_tail_does_not_resolve_skill_gap(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        detector = SkillAssuranceGapDetector(overlay_name="t3-teatree")
        firing = record_firing(detector.detect()[0], action=ActionRung.STATUSLINE)
        TaskAttempt.objects.update(started_at=timezone.now() - timedelta(hours=7))

        result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert result.reports == []
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None

    def test_evicted_from_recent_200_attempts_does_not_resolve_skill_gap(self) -> None:
        _attempt(overlay="t3-teatree", status="injection_gap", missing=["code"])
        detector = SkillAssuranceGapDetector(overlay_name="t3-teatree")
        firing = record_firing(detector.detect()[0], action=ActionRung.STATUSLINE)
        task = TaskFactory(ticket=TicketFactory(overlay="t3-teatree"))
        TaskAttempt.objects.bulk_create(
            TaskAttempt(task=task, result={"skill_assurance": {"requested": ["code"], "status": "declared"}})
            for _ in range(201)
        )

        result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert result.reports == []
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None

    def test_manual_closure_ignores_old_attempt_but_new_gap_reopens(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        detector = SkillAssuranceGapDetector(overlay_name="t3-teatree")
        firing = record_firing(detector.detect()[0], action=ActionRung.STATUSLINE)
        SelfImproveFiring.objects.filter(pk=firing.pk).update(resolved_at=timezone.now())

        assert detector.detect() == []

        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        assert len(detector.detect()) == 1

    def test_single_unrepaired_gap_escalates_to_ticket_after_two_hours(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        TaskAttempt.objects.update(started_at=timezone.now() - timedelta(hours=2, minutes=1))

        reports = SkillAssuranceGapDetector(overlay_name="t3-teatree").detect()

        assert len(reports) == 1
        assert reports[0].requested_rung == ActionRung.TICKET
        assert reports[0].max_rung == ActionRung.TICKET

    def test_single_recent_gap_does_not_ticket_before_two_hours(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])

        reports = SkillAssuranceGapDetector(overlay_name="t3-teatree").detect()

        assert len(reports) == 1
        assert reports[0].requested_rung == ActionRung.SLACK

    def test_terminal_repair_ticket_closes_only_linked_gap(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        detector = SkillAssuranceGapDetector(overlay_name="t3-teatree")
        firing = record_firing(detector.detect()[0], action=ActionRung.TICKET)
        ticket = TicketFactory(overlay="t3-teatree")
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)
        TicketTransition.objects.create(
            ticket=ticket, from_state=Ticket.State.REVIEW_REQUESTED, to_state=Ticket.State.MERGED
        )
        SelfImproveFiring.objects.filter(pk=firing.pk).update(ticket=ticket)
        TaskAttempt.objects.update(started_at=timezone.now() - timedelta(hours=7))

        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree"),
        )

        assert result.reports == []
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is not None
        assert detector.detect() == []

        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        new_report = detector.detect()[0]
        record_firing(new_report, action=ActionRung.STATUSLINE)
        assert len(detector.detect()) == 1
        assert SelfImproveFiring.objects.get(pk=firing.pk).ticket_id is None

    def test_terminal_ticket_does_not_hide_new_gap_before_scan(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        detector = SkillAssuranceGapDetector(overlay_name="t3-teatree")
        firing = record_firing(detector.detect()[0], action=ActionRung.TICKET)
        ticket = TicketFactory(overlay="t3-teatree")
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)
        TicketTransition.objects.create(
            ticket=ticket, from_state=Ticket.State.REVIEW_REQUESTED, to_state=Ticket.State.MERGED
        )
        SelfImproveFiring.objects.filter(pk=firing.pk).update(ticket=ticket)
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])

        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree"),
        )

        assert len(result.reports) == 1
        assert len(result.actions) == 1
        assert result.actions[0].rung == ActionRung.TICKET
        refreshed = SelfImproveFiring.objects.get(pk=firing.pk)
        assert refreshed.resolved_at is None
        assert refreshed.ticket_id != ticket.pk

    def test_terminal_ticket_without_transition_is_not_recovery_proof(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        detector = SkillAssuranceGapDetector(overlay_name="t3-teatree")
        firing = record_firing(detector.detect()[0], action=ActionRung.TICKET)
        ticket = TicketFactory(overlay="t3-teatree")
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)
        SelfImproveFiring.objects.filter(pk=firing.pk).update(ticket=ticket)
        TaskAttempt.objects.update(started_at=timezone.now() - timedelta(hours=7))

        result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert result.reports == []
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None

    def test_missing_skill_alerts_owner_and_scopes_to_source_overlay(self) -> None:
        _attempt(overlay="t3-teatree", status="missing", missing=["code"])
        _attempt(overlay="other", status="missing", missing=["ac-django"])

        detector = SkillAssuranceGapDetector(overlay_name="t3-teatree")
        reports = detector.detect()

        assert len(reports) == 1
        assert reports[0].requested_rung == ActionRung.SLACK
        assert reports[0].payload["requires_delivery"] is True
        assert reports[0].payload["overlay_name"] == "t3-teatree"
        assert reports[0].payload["missing"] == ["code"]
        assert reports[0].dedup_key.startswith(detector.dedup_prefix)

    def test_declared_receipt_is_not_a_gap_but_repeated_unverified_use_queues_repair(self) -> None:
        _attempt(overlay="t3-teatree", status="declared")
        for _ in range(3):
            _attempt(overlay="t3-teatree", status="unverified")

        reports = SkillAssuranceGapDetector(overlay_name="t3-teatree").detect()

        assert len(reports) == 1
        assert reports[0].requested_rung == ActionRung.TICKET
        assert reports[0].payload["count"] == 3
        assert reports[0].payload["cause"] == "application-unverified"

    def test_repeated_missing_skill_queues_repair_after_initial_owner_alert(self) -> None:
        for _ in range(3):
            _attempt(overlay="t3-teatree", status="missing", missing=["code"])

        reports = SkillAssuranceGapDetector(overlay_name="t3-teatree").detect()

        assert len(reports) == 1
        assert reports[0].requested_rung == ActionRung.TICKET
        assert reports[0].payload["cause"] == "skill-missing"
        assert reports[0].payload["count"] == 3

    def test_no_receipt_is_historical_unknown_not_a_fabricated_gap(self) -> None:
        task = TaskFactory(ticket=TicketFactory(overlay="t3-teatree"))
        TaskAttempt.objects.create(task=task, result={"summary": "legacy"})

        assert SkillAssuranceGapDetector(overlay_name="t3-teatree").detect() == []

    def test_no_required_skill_is_not_an_application_gap(self) -> None:
        _attempt(overlay="t3-teatree", status="unverified", requested=[])

        assert SkillAssuranceGapDetector(overlay_name="t3-teatree").detect() == []
