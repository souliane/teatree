"""The #1405 merge-decision-point consult: refuse a merge while a review is in flight.

``execute_bound_merge`` is the single chokepoint both autonomous merge paths
(the keystone CLEAR path and the solo-overlay bypass) converge on before the
forge squash PUT — the same chokepoint ``assert_review_verdict_gate`` (#2829)
runs at. This adds a second, independent consult: even when a recorded
``merge_safe`` verdict exists at the live head, a merge is refused while the
per-MR :class:`~teatree.core.models.mr_review_lock.MRReviewLock` is actively
held (``review_dispatched`` / ``verdict_pending``, not yet stale) — a
concurrently-dispatched review could still be about to record a HOLD.

Drives the REAL ``merge_ticket_pr`` -> ``execute_bound_merge`` chokepoint (only
the ``gh`` subprocess is stubbed), so the refusal test is the RED-before-fix
anti-vacuity proof: on pre-#1405 code (no lock consult) this scenario merges.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import MergeOutcome, MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeAudit, MergeClear, MRReviewLock, ReviewVerdict, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR = 1405
_HEAD = "c" * 40


def _clear(*, ticket: Ticket | None = None) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=_PR,
        slug=_SLUG,
        reviewed_sha=_HEAD,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


def _seed_merge_safe_verdict() -> None:
    ReviewVerdict.record(
        pr_id=_PR,
        slug=_SLUG,
        reviewed_sha=_HEAD,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity="cold-reviewer",
    )


_GH_PROBES: tuple[tuple[str, str], ...] = (
    ("headRefOid", _HEAD),
    ("isDraft", "false"),
    ("statusCheckRollup", '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'),
    ("baseRefName", "main"),
    ("required_status_checks", '{"contexts": []}'),
    ("state,mergeCommit", '{"state": "OPEN", "mergeCommit": null}'),
)


def _gh_green(argv: list[str]) -> tuple[int, str, str]:
    joined = " ".join(argv)
    for needle, out in _GH_PROBES:
        if needle in joined:
            return (0, out, "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "merged0deadbeef"}', "")
    return (0, "", "")


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


def _merge(clear: MergeClear) -> MergeOutcome:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_green):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


class TestMergeRefusedWhileReviewLockHeld(TestCase):
    def test_refused_while_lock_is_review_dispatched(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket=ticket)
        _seed_merge_safe_verdict()
        lock = MRReviewLock.acquire(slug=_SLUG, pr_id=_PR, holder="t3:reviewer-agent-b")
        assert lock is not None

        with pytest.raises(MergePreconditionError, match="review_dispatched") as excinfo:
            _merge(clear)

        assert "t3:reviewer-agent-b" in str(excinfo.value)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None
        assert not MergeAudit.objects.filter(clear=clear).exists()

    def test_refused_while_lock_is_verdict_pending(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket=ticket)
        _seed_merge_safe_verdict()
        MRReviewLock.acquire(slug=_SLUG, pr_id=_PR, holder="t3:reviewer-agent-b")
        MRReviewLock.mark_verdict_pending(slug=_SLUG, pr_id=_PR)

        with pytest.raises(MergePreconditionError, match="verdict_pending"):
            _merge(clear)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW


class TestMergeProceedsWhenLockIsNotHeld(TestCase):
    def test_proceeds_with_no_lock_row_at_all(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket=ticket)
        _seed_merge_safe_verdict()
        assert MRReviewLock.objects.count() == 0

        outcome = _merge(clear)

        ticket.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED

    def test_proceeds_once_the_lock_has_resolved(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket=ticket)
        _seed_merge_safe_verdict()
        MRReviewLock.acquire(slug=_SLUG, pr_id=_PR, holder="t3:reviewer-agent-b")
        MRReviewLock.resolve(slug=_SLUG, pr_id=_PR)

        outcome = _merge(clear)

        ticket.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED

    def test_expired_unresolved_lock_escalates_then_proceeds(self) -> None:
        # Low finding: an expired-but-unresolved dispatch lock (a slow/crashed
        # reviewer that never recorded a verdict) is NOT silently treated as
        # "no review in flight" — the first attempt refuses (escalation) and
        # reconciles; a subsequent attempt proceeds (bounded, never a lockout).
        import datetime as dt  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket=ticket)
        _seed_merge_safe_verdict()
        MRReviewLock.acquire(slug=_SLUG, pr_id=_PR, holder="t3:reviewer-agent-b", ttl=dt.timedelta(seconds=-1))

        with pytest.raises(MergePreconditionError, match="expired without recording a verdict"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        # The escalation reconciled the stale row → the next attempt proceeds.
        assert MRReviewLock.expired_unresolved_lock_for(slug=_SLUG, pr_id=_PR) is None

        outcome = _merge(clear)
        ticket.refresh_from_db()
        assert outcome.merged_sha
        assert ticket.state == Ticket.State.MERGED
