"""``ForgottenMergeDetector`` per-detector tests (BLUEPRINT § 5.7 / plan §8)."""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import MergeAudit, MergeClear, SelfImproveFiring
from teatree.core.models.merge_clear import ClearRequest
from teatree.loop.self_improve.actions import run_action_ladder
from teatree.loop.self_improve.detectors import ForgottenMergeDetector


def _issue_clear(
    *,
    pr_id: int = 100,
    slug: str = "souliane/teatree",
    reviewed_sha: str = "deadbeef0123456789",
    reviewer_identity: str = "reviewer@example.com",
    blast_class: str = "logic",
) -> MergeClear:
    request = ClearRequest(
        pr_id=pr_id,
        slug=slug,
        reviewed_sha=reviewed_sha,
        reviewer_identity=reviewer_identity,
        gh_verify_result="green",
        blast_class=blast_class,
    )
    return MergeClear.issue(request)


class ForgottenMergeDetectorTests(TestCase):
    def test_fires_when_smell_present(self) -> None:
        """CLEAR issued > 30 min ago, no MergeAudit ⇒ forgotten merge."""
        clear = _issue_clear(pr_id=200)
        # Backdate the issue_at to make it stale.
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)
        reports = ForgottenMergeDetector().detect()
        assert len(reports) == 1
        assert reports[0].severity == "error"
        assert "200" in reports[0].summary

    def test_does_not_fire_when_smell_absent(self) -> None:
        """Recent CLEAR (under 30 min) ⇒ no smell."""
        _issue_clear(pr_id=201)  # issued_at = now
        assert ForgottenMergeDetector().detect() == []

    def test_does_not_fire_when_merge_audit_exists(self) -> None:
        """A stale CLEAR that was already merged (MergeAudit present) ⇒ no smell."""
        clear = _issue_clear(pr_id=202)
        old = timezone.now() - dt.timedelta(hours=2)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)
        MergeAudit.objects.create(clear=clear, merged_sha="cafef00d1234", required_checks_status="green")
        assert ForgottenMergeDetector().detect() == []

    def test_dedup_within_cooldown(self) -> None:
        clear = _issue_clear(pr_id=203)
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)
        for report in ForgottenMergeDetector().detect():
            run_action_ladder(report)
        for report in ForgottenMergeDetector().detect():
            run_action_ladder(report)
        # Same evidence ⇒ one durable row.
        assert SelfImproveFiring.objects.filter(detector="forgotten_merge").count() == 1
        assert SelfImproveFiring.objects.get(detector="forgotten_merge").action_count == 1

    def test_action_ladder_ceiling(self) -> None:
        """Ceiling is ``slack`` per the issue plan."""
        clear = _issue_clear(pr_id=204)
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)
        reports = ForgottenMergeDetector().detect()
        assert reports
        assert reports[0].max_rung == SelfImproveFiring.Action.SLACK.value

    def test_auto_fix_false(self) -> None:
        assert ForgottenMergeDetector.auto_fix is False

    def test_payload_carries_pr_identity(self) -> None:
        clear = _issue_clear(pr_id=205)
        old = timezone.now() - dt.timedelta(hours=1)
        MergeClear.objects.filter(pk=clear.pk).update(issued_at=old)
        # Detector-specific edge: the payload must carry enough to
        # reconstruct the keystone-merge entry point (pr_id + slug +
        # reviewed_sha) so the user can act on it from the statusline.
        report = ForgottenMergeDetector().detect()[0]
        assert report.payload["pr_id"] == 205
        assert report.payload["slug"] == "souliane/teatree"
        assert report.payload["reviewed_sha"] == "deadbeef0123456789"
