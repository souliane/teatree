import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.models import PullRequest, Ticket
from tests.factories import merge_void

_SLUG = "example-org/example-repo"


def _merged_row(pr_id: int = 41, *, slug: str = _SLUG) -> PullRequest:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.MERGED)
    return PullRequest.objects.create(
        ticket=ticket,
        overlay="t3-teatree",
        url=f"https://github.com/{slug}/pull/{pr_id}",
        repo=slug,
        iid=str(pr_id),
        state=PullRequest.State.MERGED,
    )


def _voided_row(pr_id: int = 41) -> PullRequest:
    row = _merged_row(pr_id)
    merge_void(slug=_SLUG.upper(), pr_id=pr_id)
    row.void_merge()
    row.save(update_fields=["state"])
    return row


class TestPullRequestVoidMerge(TestCase):
    def test_a_recorded_void_closes_the_merged_row(self) -> None:
        row = _voided_row()

        row.refresh_from_db()
        assert row.state == PullRequest.State.CLOSED

    def test_refuses_without_a_void_for_this_pr(self) -> None:
        row = _merged_row()
        merge_void(slug=_SLUG, pr_id=42)
        merge_void(slug=_SLUG, pr_id=41, host_kind="gitlab")

        with pytest.raises(TransitionNotAllowed):
            row.void_merge()

        assert row.state == PullRequest.State.MERGED

    def test_record_forge_merge_leaves_a_voided_pr_closed(self) -> None:
        voided = _voided_row()
        other = _merged_row(42)
        other.state = PullRequest.State.OPEN
        other.save()

        assert PullRequest.objects.record_forge_merge(slug=_SLUG, pr_id=41) == 0
        assert PullRequest.objects.record_forge_merge(slug=_SLUG, pr_id=42) == 1

        voided.refresh_from_db()
        assert voided.state == PullRequest.State.CLOSED
