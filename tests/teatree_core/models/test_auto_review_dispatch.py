"""Tests for :class:`AutoReviewDispatch` — the auto-review-dispatch ledger + task factory (#68)."""

import pytest

from teatree.core.models import AutoReviewDispatch, MRReviewLock, Task, Ticket
from teatree.core.models.auto_review_dispatch import build_review_contract

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
URL = f"https://github.com/{SLUG}/pull/6230"


class TestEnqueueCreatesClaimableTask:
    def test_first_enqueue_creates_one_pending_reviewing_task(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert row is not None
        assert row.task is not None
        task = row.task
        assert task.phase == "reviewing"
        assert task.status == Task.Status.PENDING
        assert task.ticket.role == Ticket.Role.REVIEWER
        assert task.ticket.issue_url == URL

    def test_task_execution_reason_carries_the_return_envelope_contract(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert row is not None
        assert row.task is not None
        reason = row.task.execution_reason
        # corr-11: the headless reviewer RETURNS the verdict envelope; it must
        # NOT be told to run the shell-only `t3 <overlay> review record`.
        assert "review_verdict" in reason
        assert "Do NOT run `t3 <overlay> review record`" in reason
        assert HEAD in reason

    def test_blank_slug_or_head_does_not_enqueue(self) -> None:
        assert AutoReviewDispatch.enqueue(slug="", pr_id=1, head_sha=HEAD) is None
        assert AutoReviewDispatch.enqueue(slug=SLUG, pr_id=1, head_sha="") is None
        assert Task.objects.count() == 0

    def test_str_renders_slug_pr_and_short_head(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        assert row is not None
        assert str(row) == f"auto-review<{row.pk}:{SLUG}#6230@{HEAD[:8]}>"


class TestDedupPerHead:
    def test_second_enqueue_same_head_returns_none_and_creates_no_second_task(self) -> None:
        first = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        second = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert first is not None
        assert second is None
        assert AutoReviewDispatch.objects.count() == 1
        assert Task.objects.filter(phase="reviewing").count() == 1

    def test_new_head_while_prior_review_in_flight_does_not_rearm(self) -> None:
        # #1405: the MRReviewLock is keyed on the MR, not the head — a fresh
        # push while the prior review hasn't concluded must not arm a SECOND,
        # concurrent reviewer for the new head.
        first = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        rearmed = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=NEW_HEAD, pr_url=URL, overlay="teatree")

        assert first is not None
        assert rearmed is None
        assert AutoReviewDispatch.objects.count() == 1
        assert Task.objects.filter(phase="reviewing").count() == 1

    def test_new_head_rearms_once_the_prior_review_has_resolved(self) -> None:
        AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        MRReviewLock.resolve(slug=SLUG, pr_id=6230)

        rearmed = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=NEW_HEAD, pr_url=URL, overlay="teatree")

        assert rearmed is not None
        assert rearmed.task is not None
        assert AutoReviewDispatch.objects.count() == 2
        assert Task.objects.filter(phase="reviewing").count() == 2

    def test_re_enqueue_same_head_after_resolve_is_still_a_dedup_no_op(self) -> None:
        # A fresh lock acquire alone isn't enough to rearm — the AutoReviewDispatch
        # row's own (slug, pr_id, head_sha) uniqueness still dedups a re-enqueue on
        # the EXACT same head, even once the lock has resolved and become acquirable.
        AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        MRReviewLock.resolve(slug=SLUG, pr_id=6230)

        second = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert second is None
        assert AutoReviewDispatch.objects.count() == 1
        assert Task.objects.filter(phase="reviewing").count() == 1

    def test_distinct_prs_are_independent(self) -> None:
        AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        other = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6231, head_sha=HEAD, pr_url=URL, overlay="teatree")

        assert other is not None
        assert Task.objects.filter(phase="reviewing").count() == 2


class TestReviewContract:
    def test_contract_instructs_returning_the_verdict_envelope(self) -> None:
        contract = build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL)
        assert "review_verdict" in contract
        assert "merge_safe" in contract
        assert HEAD in contract

    def test_contract_forbids_the_shell_record_cli(self) -> None:
        contract = build_review_contract(slug=SLUG, pr_id=1, head_sha=HEAD, pr_url=URL)
        assert "Do NOT run `t3 <overlay> review record`" in contract


class TestDispatchedTaskReachesTerminalState:
    """The auto-created Ticket + reviewing Task reach DELIVERED on the happy path.

    The whole point of arming the dispatch is that the enqueued unit can run
    to completion: the reviewer claims the ``Task(phase=reviewing)``, records
    its verdict (stamping ``reviewed_sha`` on the reviewer-role ticket), and
    completing the task short-circuits the ticket to ``DELIVERED`` via
    ``mark_reviewed_externally``. An armed dispatch that never reached a
    terminal state would re-pump the same review forever.
    """

    def test_reviewer_completing_task_short_circuits_ticket_to_delivered(self) -> None:
        row = AutoReviewDispatch.enqueue(slug=SLUG, pr_id=6230, head_sha=HEAD, pr_url=URL, overlay="teatree")
        assert row is not None
        assert row.task is not None
        task = row.task
        ticket = task.ticket
        assert ticket.role == Ticket.Role.REVIEWER
        assert ticket.state == Ticket.State.NOT_STARTED

        # The reviewer records the verdict bound to the reviewed head — the
        # ``review record`` CLI stamps ``reviewed_sha`` on the ticket, which
        # ``mark_reviewed_externally`` persists into ``last_review_state``.
        ticket.merge_extra(set_keys={"reviewed_sha": HEAD})

        task.complete()

        ticket.refresh_from_db()
        task.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert task.status == Task.Status.COMPLETED
