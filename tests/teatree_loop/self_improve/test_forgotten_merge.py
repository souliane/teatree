"""``ForgottenMergeDetector`` per-detector tests (BLUEPRINT § 5.7 / plan §8).

The detector answers the same question as the doctor backlog check, and used to answer
it from its own weaker query — ``audits__isnull=True``, no supersede exclusion, no
liveness — at ``severity="error"``. On the live ledger that produced 8 unresolved
firings, 8/8 false: six PRs that had merged outside the keystone and two that do not
resolve at all. It now reads the canonical population and the shared classifier, so
only a PR the forge reports OPEN is reported.

The forge reader is injected everywhere below. The production default is
:func:`unverified_reader`, so a case that seeded no reader would assert nothing.
"""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import MergeAudit, MergeClear, SelfImproveFiring
from teatree.core.models.merge_clear import ClearRequest
from teatree.loop.self_improve.actions import run_action_ladder
from teatree.loop.self_improve.detectors import ForgottenMergeDetector


def _reads(state: str) -> object:
    def read(pr_url: str) -> str:
        return state

    return read


def _detector(state: str = PrOpenState.OPEN) -> ForgottenMergeDetector:
    return ForgottenMergeDetector(read_state=_reads(state))


def _issue_clear(
    *,
    pr_id: int = 100,
    slug: str = "souliane/teatree",
    reviewed_sha: str = "deadbeef0123456789abcdef0123456789abcdef",
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


def _stale(pr_id: int, *, hours: float = 1.0) -> MergeClear:
    clear = _issue_clear(pr_id=pr_id)
    MergeClear.objects.filter(pk=clear.pk).update(issued_at=timezone.now() - dt.timedelta(hours=hours))
    return clear


class ForgottenMergeDetectorTests(TestCase):
    def test_fires_when_smell_present(self) -> None:
        """CLEAR issued > 30 min ago whose PR is still OPEN ⇒ forgotten merge."""
        _stale(200)
        reports = _detector().detect()
        assert len(reports) == 1
        assert reports[0].severity == "error"
        assert "200" in reports[0].summary

    def test_does_not_fire_when_smell_absent(self) -> None:
        """Recent CLEAR (under 30 min) ⇒ no smell."""
        _issue_clear(pr_id=201)  # issued_at = now
        assert _detector().detect() == []

    def test_does_not_fire_when_merge_audit_exists(self) -> None:
        """A stale CLEAR that was already merged (MergeAudit present) ⇒ no smell."""
        clear = _stale(202, hours=2)
        MergeAudit.objects.create(clear=clear, merged_sha="cafef00d1234", required_checks_status="green")
        assert _detector().detect() == []

    def test_ignores_a_pr_that_already_merged_outside_the_keystone(self) -> None:
        # The 8 live false firings: merged on the forge, so no MergeAudit was ever
        # written. A missing audit is not evidence the merge never happened.
        _stale(4142, hours=187)
        assert _detector(PrOpenState.MERGED).detect() == []

    def test_ignores_a_pr_whose_state_cannot_be_read(self) -> None:
        _stale(4242, hours=187)
        assert _detector(PrOpenState.UNKNOWN).detect() == []

    def test_the_default_reader_can_never_page(self) -> None:
        _stale(4243, hours=187)
        assert ForgottenMergeDetector().detect() == []

    def test_dedup_within_cooldown(self) -> None:
        _stale(203)
        for report in _detector().detect():
            run_action_ladder(report)
        for report in _detector().detect():
            run_action_ladder(report)
        # Same evidence ⇒ one durable row.
        assert SelfImproveFiring.objects.filter(detector="forgotten_merge").count() == 1
        assert SelfImproveFiring.objects.get(detector="forgotten_merge").action_count == 1

    def test_action_ladder_ceiling(self) -> None:
        """Ceiling is ``slack`` per the issue plan."""
        _stale(204)
        reports = _detector().detect()
        assert reports
        assert reports[0].max_rung == SelfImproveFiring.Action.SLACK.value

    def test_auto_fix_false(self) -> None:
        assert ForgottenMergeDetector.auto_fix is False

    def test_payload_carries_pr_identity(self) -> None:
        _stale(205)
        # Detector-specific edge: the payload must carry enough to
        # reconstruct the keystone-merge entry point (pr_id + slug +
        # reviewed_sha) so the user can act on it from the statusline.
        report = _detector().detect()[0]
        assert report.payload["pr_id"] == 205
        assert report.payload["slug"] == "souliane/teatree"
        assert report.payload["reviewed_sha"] == "deadbeef0123456789abcdef0123456789abcdef"
