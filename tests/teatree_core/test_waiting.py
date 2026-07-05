"""Tests for the durable waiting-on-you gatherer (PR-21)."""

from datetime import timedelta

import pytest

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.merge_clear import ClearRequest, MergeClear
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_assignment import ReviewAssignment, ReviewIntent
from teatree.core.models.ticket import Ticket
from teatree.core.models.waiting_item import WaitingItem
from teatree.core.waiting import WaitingKind, format_age, gather_waiting


def _kinds(overlay: str = "") -> list[str]:
    return [entry.kind for entry in gather_waiting(overlay)]


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestGatherWaiting:
    def test_empty_when_nothing_pending(self) -> None:
        assert gather_waiting("") == []

    def test_pending_question_is_gathered(self) -> None:
        DeferredQuestion.record("what region should I deploy to?")
        entries = gather_waiting("")
        assert [e.kind for e in entries] == [WaitingKind.QUESTION]
        assert "region" in entries[0].ref

    def test_manual_item_is_gathered(self) -> None:
        WaitingItem.objects.add("chase the finance sign-off")
        entries = gather_waiting("")
        assert [e.kind for e in entries] == [WaitingKind.MANUAL]
        assert entries[0].ref == "chase the finance sign-off"

    def test_pending_review_request_is_gathered(self) -> None:
        ReviewAssignment.record(
            ReviewIntent(
                mr_url="https://github.com/o/r/pull/7",
                user_id="u1",
                channel="C1",
                slack_ts="1.1",
                trigger="reaction",
                overlay="teatree",
            )
        )
        entries = gather_waiting("teatree")
        assert [e.kind for e in entries] == [WaitingKind.REVIEW_REQUEST]
        assert entries[0].url == "https://github.com/o/r/pull/7"

    def test_approved_pr_without_clear_is_merge_authorization(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree", issue_url="https://x/41")
        pr = PullRequest.objects.create(
            ticket=ticket, overlay="teatree", url="https://github.com/o/r/pull/9", repo="o/r", iid="9"
        )
        pr.request_review()
        pr.approve()
        pr.save()
        entries = gather_waiting("teatree")
        assert [e.kind for e in entries] == [WaitingKind.MERGE_AUTHORIZATION]
        assert entries[0].url == "https://github.com/o/r/pull/9"


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestResolvingClearsEntryByConstruction:
    def test_answering_a_question_removes_its_entry(self) -> None:
        question = DeferredQuestion.record("deploy now?")
        assert WaitingKind.QUESTION in _kinds()  # present before
        DeferredQuestion.consume(question.pk, answer="yes")
        assert WaitingKind.QUESTION not in _kinds()  # absent after

    def test_covering_clear_removes_the_merge_authorization_entry(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree", issue_url="https://x/42")
        pr = PullRequest.objects.create(
            ticket=ticket, overlay="teatree", url="https://github.com/o/r/pull/10", repo="o/r", iid="10"
        )
        pr.request_review()
        pr.approve()
        pr.save()
        assert WaitingKind.MERGE_AUTHORIZATION in _kinds("teatree")  # present before

        MergeClear.issue(
            ClearRequest(
                pr_id=10,
                slug="o/r",
                reviewed_sha="a" * 40,
                reviewer_identity="cold-reviewer",
                ticket=ticket,
            )
        )
        assert WaitingKind.MERGE_AUTHORIZATION not in _kinds("teatree")  # absent after

    def test_resolving_a_manual_item_removes_its_entry(self) -> None:
        item = WaitingItem.objects.add("call the bank")
        assert WaitingKind.MANUAL in _kinds()
        WaitingItem.objects.resolve(item.pk)
        assert WaitingKind.MANUAL not in _kinds()


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestOverlayScoping:
    def test_specific_overlay_excludes_other_overlays_pr(self) -> None:
        ticket = Ticket.objects.create(overlay="other", issue_url="https://x/43")
        pr = PullRequest.objects.create(
            ticket=ticket, overlay="other", url="https://github.com/o/r/pull/11", repo="o/r", iid="11"
        )
        pr.request_review()
        pr.approve()
        pr.save()
        assert _kinds("teatree") == []
        assert _kinds("other") == [WaitingKind.MERGE_AUTHORIZATION]

    def test_empty_overlay_includes_every_overlay(self) -> None:
        ticket = Ticket.objects.create(overlay="other", issue_url="https://x/44")
        pr = PullRequest.objects.create(
            ticket=ticket, overlay="other", url="https://github.com/o/r/pull/12", repo="o/r", iid="12"
        )
        pr.request_review()
        pr.approve()
        pr.save()
        assert _kinds("") == [WaitingKind.MERGE_AUTHORIZATION]


class TestFormatAge:
    def test_days(self) -> None:
        assert format_age(timedelta(days=2, hours=3)) == "2d"

    def test_hours(self) -> None:
        assert format_age(timedelta(hours=5)) == "5h"

    def test_minutes(self) -> None:
        assert format_age(timedelta(minutes=7)) == "7m"

    def test_just_now(self) -> None:
        assert format_age(timedelta(seconds=10)) == "now"
