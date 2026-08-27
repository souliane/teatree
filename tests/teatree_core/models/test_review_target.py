"""``review_target`` — the one answer to "which PR + head is this review answerable for" (#4308).

The writer and the reader each held half of this and disagreed: a reviewing task with no
:class:`AutoReviewDispatch` resolved to nothing on the write side, so its verdict was
discarded while the task completed exit 0.
"""

import pytest
from django.test import TestCase

from teatree.core.models import AutoReviewDispatch, ReviewVerdict, Session, Task, Ticket
from teatree.core.models.auto_review_dispatch import LOOP_SCANNER_HOLDER
from teatree.core.models.review_target import review_target_for_task, verdict_at

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 4308
_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
_DISPATCH_HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"


def _reviewer_task(*, issue_url: str, extra: dict[str, object] | None = None) -> Task:
    ticket = Ticket.objects.create(
        issue_url=issue_url,
        overlay="teatree",
        role=Ticket.Role.REVIEWER,
        extra=extra or {},
    )
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.PENDING)


class TestReviewTargetResolution(TestCase):
    def test_the_dispatch_row_is_the_most_authoritative_source(self) -> None:
        dispatch = AutoReviewDispatch.enqueue(
            slug=_SLUG,
            pr_id=_PR_ID,
            head_sha=_DISPATCH_HEAD,
            pr_url=f"https://github.com/{_SLUG}/pull/{_PR_ID}",
            overlay="teatree",
        )
        assert dispatch is not None
        task = dispatch.task
        assert task is not None

        target = review_target_for_task(task)

        assert target is not None
        assert (target.slug, target.pr_id, target.head_sha) == (_SLUG, _PR_ID, _DISPATCH_HEAD)
        assert target.lock_holder == LOOP_SCANNER_HOLDER

    def test_a_reviewer_ticket_without_a_dispatch_resolves_to_the_pr_it_is(self) -> None:
        task = _reviewer_task(
            issue_url=f"https://github.com/{_SLUG}/pull/{_PR_ID}",
            extra={"reviewed_sha": _HEAD},
        )

        target = review_target_for_task(task)

        assert target is not None
        assert (target.slug, target.pr_id, target.head_sha) == (_SLUG, _PR_ID, _HEAD)
        # This path took no lock, so it must not free a still-running reviewer's.
        assert target.lock_holder == ""

    def test_a_known_pr_with_no_recorded_head_resolves_with_an_empty_head(self) -> None:
        task = _reviewer_task(issue_url=f"https://github.com/{_SLUG}/pull/{_PR_ID}")

        target = review_target_for_task(task)

        assert target is not None
        assert target.head_sha == ""

    def test_a_task_answerable_for_no_pull_request_resolves_to_none(self) -> None:
        task = _reviewer_task(issue_url=f"https://github.com/{_SLUG}/issues/{_PR_ID}")

        assert review_target_for_task(task) is None


class TestVerdictReadBack(TestCase):
    def _target_with_verdict(self, *, recorded_slug: str) -> Task:
        task = _reviewer_task(
            issue_url=f"https://github.com/{_SLUG}/pull/{_PR_ID}",
            extra={"reviewed_sha": _HEAD},
        )
        ReviewVerdict.record(
            pr_id=_PR_ID,
            slug=recorded_slug,
            reviewed_sha=_HEAD,
            verdict="hold",
            reviewer_identity="cold-reviewer-agent",
        )
        return task

    def test_a_verdict_at_the_target_head_reads_back(self) -> None:
        task = self._target_with_verdict(recorded_slug=_SLUG)
        target = review_target_for_task(task)
        assert target is not None

        recorded = verdict_at(target)

        assert recorded is not None
        assert recorded.verdict == ReviewVerdict.Verdict.HOLD

    def test_a_verdict_recorded_under_a_differently_cased_slug_reads_back(self) -> None:
        # Forge slugs are case-insensitive, and the merge gate and the landed-work guard
        # both resolve them that way — a read-back that did not would report a verdict
        # those consumers CAN see as never persisted.
        task = self._target_with_verdict(recorded_slug=_SLUG.upper())
        target = review_target_for_task(task)
        assert target is not None

        assert verdict_at(target) is not None

    def test_an_unknown_head_can_never_read_back_a_verdict(self) -> None:
        task = _reviewer_task(issue_url=f"https://github.com/{_SLUG}/pull/{_PR_ID}")
        ReviewVerdict.record(
            pr_id=_PR_ID,
            slug=_SLUG,
            reviewed_sha=_HEAD,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer-agent",
        )
        target = review_target_for_task(task)
        assert target is not None

        assert verdict_at(target) is None
