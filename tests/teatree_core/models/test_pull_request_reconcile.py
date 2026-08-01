"""``PullRequestQuerySet.reconcile_forge_states`` — the ledger's convergence pass (#3984).

The recorder at the merge chokepoint stops NEW stale rows; this is what corrects the
ones already wrong, whose PR merged out of band while nothing advanced the row.
"""

import pytest

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import PullRequest, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"


def _row(pr_id: int, *, state: str = PullRequest.State.OPEN) -> PullRequest:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
    return PullRequest.objects.create(
        ticket=ticket,
        overlay="t3-teatree",
        url=f"https://github.com/{SLUG}/pull/{pr_id}",
        repo=SLUG,
        iid=str(pr_id),
        state=state,
    )


class TestReconcileForgeStates:
    def test_a_row_whose_pr_merged_out_of_band_converges_to_merged(self) -> None:
        row = _row(1)

        assert PullRequest.objects.reconcile_forge_states(read_state=lambda _url: PrOpenState.MERGED) == 1

        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED

    def test_a_row_whose_pr_was_closed_unmerged_converges_to_closed(self) -> None:
        row = _row(2)

        PullRequest.objects.reconcile_forge_states(read_state=lambda _url: PrOpenState.CLOSED)

        row.refresh_from_db()
        assert row.state == PullRequest.State.CLOSED

    def test_a_genuinely_open_pr_is_left_alone(self) -> None:
        row = _row(3)

        assert PullRequest.objects.reconcile_forge_states(read_state=lambda _url: PrOpenState.OPEN) == 0

        row.refresh_from_db()
        assert row.state == PullRequest.State.OPEN

    def test_an_unreadable_forge_never_settles_a_row(self) -> None:
        """UNKNOWN is the fail-open value every transport error maps to — it must not settle."""
        row = _row(4)

        assert PullRequest.objects.reconcile_forge_states(read_state=lambda _url: PrOpenState.UNKNOWN) == 0

        row.refresh_from_db()
        assert row.state == PullRequest.State.OPEN

    def test_settled_rows_are_never_re_probed(self) -> None:
        """MERGED and CLOSED are terminal, so the pass shrinks to the genuinely-open set."""
        _row(5, state=PullRequest.State.MERGED)
        _row(6, state=PullRequest.State.CLOSED)
        open_row = _row(7)
        probed: list[str] = []

        def _read(url: str) -> str:
            probed.append(url)
            return PrOpenState.MERGED

        PullRequest.objects.reconcile_forge_states(read_state=_read)

        assert probed == [open_row.url]

    def test_is_idempotent(self) -> None:
        _row(8)

        first = PullRequest.objects.reconcile_forge_states(read_state=lambda _url: PrOpenState.MERGED)
        second = PullRequest.objects.reconcile_forge_states(read_state=lambda _url: PrOpenState.MERGED)

        assert (first, second) == (1, 0)

    def test_one_unreadable_row_never_aborts_the_others(self) -> None:
        bad = _row(9)
        good = _row(10)

        def _read(url: str) -> str:
            if url == bad.url:
                msg = "forge exploded"
                raise RuntimeError(msg)
            return PrOpenState.MERGED

        assert PullRequest.objects.reconcile_forge_states(read_state=_read) == 1

        bad.refresh_from_db()
        good.refresh_from_db()
        assert (bad.state, good.state) == (PullRequest.State.OPEN, PullRequest.State.MERGED)

    def test_the_queryset_scopes_the_pass(self) -> None:
        mine = _row(11)
        theirs = _row(12)
        theirs.overlay = "other-overlay"
        theirs.save(update_fields=["overlay"])

        PullRequest.objects.filter(overlay="t3-teatree").reconcile_forge_states(
            read_state=lambda _url: PrOpenState.MERGED
        )

        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert (mine.state, theirs.state) == (PullRequest.State.MERGED, PullRequest.State.OPEN)
