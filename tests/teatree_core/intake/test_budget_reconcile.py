"""``reconcile_holder_pr_rows`` — the budget's own ledger convergence (#3984).

A stale ``open`` row defeats BOTH halves of the #3978 intake fix: the release rule
never fires because it asks for ``MERGED``, and the deadlock alarm stays silent
because it reads the same row as proof of a live attempt. This pass is what stops one
unadvanced field from doing that, and it is bounded by the slot count rather than the
ledger size — only the rows of the tickets actually holding a slot are probed.
"""

import datetime as dt

import pytest

from teatree.core.backend_protocols import PrOpenState
from teatree.core.intake.budget import read_intake_budget, reconcile_holder_pr_rows
from teatree.core.models import ImplementedIssueMarker, PullRequest, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
OVERLAY = "t3-teatree"

#: Collapse the marker settle window so a just-created holder is judged on its evidence
#: rather than on its age — the ``deadlocked`` case needs the evidence branch.
_NO_SETTLE = dt.timedelta(0)


def _held_slot(issue_number: int, *, overlay: str = OVERLAY) -> tuple[ImplementedIssueMarker, PullRequest]:
    issue_url = f"https://github.com/{SLUG}/issues/{issue_number}"
    marker = ImplementedIssueMarker.objects.create(issue_url=issue_url, overlay=overlay)
    ticket = Ticket.objects.create(overlay=overlay, state=Ticket.State.IN_REVIEW, issue_url=issue_url)
    row = PullRequest.objects.create(
        ticket=ticket,
        overlay=overlay,
        url=f"https://github.com/{SLUG}/pull/{issue_number}",
        repo=SLUG,
        iid=str(issue_number),
    )
    return marker, row


class TestReconcileHolderPrRows:
    def test_a_holder_whose_pr_merged_out_of_band_converges(self) -> None:
        marker, row = _held_slot(3978)

        assert reconcile_holder_pr_rows(OVERLAY, read_state=lambda _url: PrOpenState.MERGED) == 1

        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED
        assert ImplementedIssueMarker.objects.find_stale(OVERLAY).completed == (marker.pk,)

    def test_the_deadlock_alarm_stops_reading_a_landed_pr_as_a_live_attempt(self) -> None:
        """The second half of the jam: a stale row classified the holder as mid-flight."""
        _held_slot(3977)
        assert read_intake_budget(OVERLAY, limit=1, settle=_NO_SETTLE).deadlocked is False

        reconcile_holder_pr_rows(OVERLAY, read_state=lambda _url: PrOpenState.MERGED)

        assert read_intake_budget(OVERLAY, limit=1, settle=_NO_SETTLE).deadlocked is True

    def test_only_the_holders_rows_are_probed(self) -> None:
        _held_slot(3984)
        released_marker, _ = _held_slot(3900)
        ImplementedIssueMarker.objects.filter(pk=released_marker.pk).update(
            state=ImplementedIssueMarker.State.COMPLETED
        )
        probed: list[str] = []

        def _read(url: str) -> str:
            probed.append(url)
            return PrOpenState.OPEN

        reconcile_holder_pr_rows(OVERLAY, read_state=_read)

        assert probed == [f"https://github.com/{SLUG}/pull/3984"]

    def test_another_overlays_holders_are_untouched(self) -> None:
        _, theirs = _held_slot(3800, overlay="other-overlay")

        assert reconcile_holder_pr_rows(OVERLAY, read_state=lambda _url: PrOpenState.MERGED) == 0

        theirs.refresh_from_db()
        assert theirs.state == PullRequest.State.OPEN

    def test_no_holders_reads_nothing(self) -> None:
        def _read(url: str) -> str:
            raise AssertionError(url)

        assert reconcile_holder_pr_rows(OVERLAY, read_state=_read) == 0
