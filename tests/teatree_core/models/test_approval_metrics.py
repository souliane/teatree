"""Per-action-class trust metrics off the SendAudit + DeferredQuestion ledgers (#119).

The metrics feed the dial's auto-re-tighten: a graduated class re-tightens once its
trailing-window decline rate / defect escapes / rework breach. These tests pin the
attribution (which ledger row belongs to which class), the window, and the breach
predicate.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from teatree.core.models import DeferredQuestion, SendAudit
from teatree.core.models.approval_metrics import DECLINE_RATE_THRESHOLD, WINDOW_DAYS, compute_metrics, metrics_breached
from teatree.core.models.approval_policy import DIRECTIVE_ADMIT, ON_BEHALF_POST, OUTER_LOOP_KEEP, PUBLIC_ISSUE_CREATE

pytestmark = pytest.mark.django_db


def _answered(options_hash: str, answer: str) -> None:
    question = DeferredQuestion.record("q?", options_hash=options_hash)
    DeferredQuestion.consume(question.pk, answer=answer)


class TestQuestionMetrics:
    def test_keep_declines_drive_the_decline_rate(self) -> None:
        _answered("outer_loop_keep:1", "kept")  # approval
        _answered("outer_loop_keep:2", "no")  # decline
        metrics = compute_metrics(OUTER_LOOP_KEEP)
        assert metrics.interventions == 2
        assert metrics.resolved == 2
        assert metrics.declines == 1
        assert metrics.decline_rate == pytest.approx(0.5)

    def test_directive_ratify_prefix_maps_to_directive_admit(self) -> None:
        _answered("directive_ratify:5:0", "no, scope it down")
        metrics = compute_metrics(DIRECTIVE_ADMIT)
        assert metrics.interventions == 1
        assert metrics.declines == 1

    def test_a_question_of_another_class_is_not_counted(self) -> None:
        _answered("directive_ratify:5:0", "no")
        assert compute_metrics(OUTER_LOOP_KEEP).interventions == 0

    def test_rows_outside_the_window_are_excluded(self) -> None:
        question = DeferredQuestion.record("q?", options_hash="outer_loop_keep:1")
        DeferredQuestion.consume(question.pk, answer="no")
        DeferredQuestion.objects.filter(pk=question.pk).update(
            created_at=timezone.now() - timedelta(days=WINDOW_DAYS + 1)
        )
        assert compute_metrics(OUTER_LOOP_KEEP).interventions == 0


class TestSendMetrics:
    def test_denied_verdict_is_a_defect_escape(self) -> None:
        SendAudit.objects.create(
            channel="github", action="post_comment", mode="enforce", allowlist_verdict=SendAudit.Verdict.DENIED
        )
        assert compute_metrics(ON_BEHALF_POST).defect_escapes == 1

    def test_redaction_is_rework(self) -> None:
        SendAudit.objects.create(
            channel="slack",
            action="post_comment",
            mode="enforce",
            allowlist_verdict=SendAudit.Verdict.ALLOWED,
            redaction_applied=True,
        )
        assert compute_metrics(ON_BEHALF_POST).rework == 1

    def test_issue_action_maps_to_public_issue_create(self) -> None:
        SendAudit.objects.create(
            channel="github", action="issue_create", mode="enforce", allowlist_verdict=SendAudit.Verdict.DENIED
        )
        assert compute_metrics(PUBLIC_ISSUE_CREATE).defect_escapes == 1
        assert compute_metrics(ON_BEHALF_POST).defect_escapes == 0


class TestBreach:
    def test_a_clean_class_is_not_breached(self) -> None:
        assert metrics_breached(OUTER_LOOP_KEEP) is False

    def test_decline_rate_over_threshold_breaches(self) -> None:
        # One approval, one decline → 0.5 > threshold.
        assert DECLINE_RATE_THRESHOLD < 0.5
        _answered("outer_loop_keep:1", "kept")
        _answered("outer_loop_keep:2", "no")
        assert metrics_breached(OUTER_LOOP_KEEP) is True

    def test_any_defect_escape_breaches(self) -> None:
        SendAudit.objects.create(
            channel="github", action="post_comment", mode="enforce", allowlist_verdict=SendAudit.Verdict.DENIED
        )
        assert metrics_breached(ON_BEHALF_POST) is True
