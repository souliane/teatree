"""A local review discharge is not a forge observation.

``Ticket.mark_reviewed_externally`` records that the factory finished a review pass. It used
to record that as ``ReviewState.APPROVED`` in ``Ticket.extra["last_review_state"]`` — the key
``ReviewerPrsScanner`` keeps its cache of what the FORGE reported. On a colleague MR the
factory posts a review and never approves, so the forge answers PENDING for a requested
reviewer whose ``approved_by`` list is empty. The scanner compared its own discharge against
that PENDING, called it a dismissal, and minted a reviewing task every tick against an
unchanged head.

Measured on the live box before the fix: two reviewer tickets minted 11 and 9 reviewing tasks
with ``reviewed_sha`` equal to the live head throughout, ~2 USD and ~35 turns each, every one
reaped mid-run by the terminal-ticket orphan sweep and recorded ``outcome=success``.

The discharge therefore has its own key. ``last_review_state`` is overwritten with the live
forge value on every scan, so a local act stored there survives exactly one pass — which is
why a marker that merely renames the value is not enough, and is pinned below.

A discharge is a verdict about ONE head, so anything that invalidates that verdict — the head
moving, or the forge dropping the approval — drops it too. Otherwise the at-head dedup
swallows the re-review the dismissal signal exists to request.
"""

from django.test import TestCase

from teatree.core.backend_protocols import ReviewState
from teatree.core.models import Session, Task, Ticket
from teatree.loop.dispatch import dispatch
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.persistence_reviewer import _already_reviewed_at_head
from teatree.loop.scanners.reviewed_pr_head import _discharged_sha
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner, mark_reviewed
from tests.teatree_loop.test_scanners import FakeCodeHost

_URL = "https://gitlab/x/-/merge_requests/7057"
_SHA = "f7967d7163e9fb27183b81936cf9b27a4d010237"


def _discharged_reviewer_ticket(*, url: str = _URL, sha: str = _SHA) -> Ticket:
    """A reviewer ticket the factory has reviewed, discharged through the real transition."""
    ticket = Ticket.objects.create(
        overlay="test",
        role=Ticket.Role.REVIEWER,
        state=Ticket.State.REVIEW_POSTED,
        issue_url=url,
        extra={"reviewed_sha": sha},
    )
    session = Session.objects.create(ticket=ticket, agent_id="t")
    Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)
    ticket.mark_reviewed_externally()
    return ticket


def _scanner_seeing_pending() -> ReviewerPrsScanner:
    """The live shape: the user is a requested reviewer, nobody has approved."""
    return ReviewerPrsScanner(
        host=FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": _URL, "sha": _SHA}],
            review_state_by_url={_URL: ReviewState.PENDING},
        ),
    )


class DischargeIsNotReadBackAsAForgeApproval(TestCase):
    """The burn: a discharge the scanner mistakes for an approval it can then see dismissed."""

    def test_discharged_review_with_pending_forge_state_emits_no_dismissal(self) -> None:
        """THE regression. Unchanged head + PENDING forge state must mint nothing."""
        _discharged_reviewer_ticket()

        signals = _scanner_seeing_pending().scan()

        assert [s.kind for s in signals] == [], (
            "a review the factory posted itself was read back as a forge approval and "
            f"reported dismissed against the live PENDING: {[s.kind for s in signals]}"
        )

    def test_the_discharge_survives_the_scan_that_refreshes_the_forge_cache(self) -> None:
        """The scan writes the live PENDING over ``last_review_state``; the discharge must outlive it."""
        ticket = _discharged_reviewer_ticket()

        _scanner_seeing_pending().scan()
        ticket.refresh_from_db()

        assert _discharged_sha(ticket) == _SHA, (
            "the scan overwrote the discharge, so the ticket drops out of the moved-head "
            "watch and a later push is never re-reviewed"
        )
        assert _already_reviewed_at_head(ticket, _SHA) is True

    def test_a_second_scan_after_the_cache_refresh_still_emits_nothing(self) -> None:
        """Anti-oscillation: the steady state must be quiet, not alternating."""
        _discharged_reviewer_ticket()
        scanner = _scanner_seeing_pending()

        scanner.scan()
        second = scanner.scan()

        assert [s.kind for s in second] == []

    def test_genuine_forge_approval_later_dismissed_still_emits(self) -> None:
        """Anti-over-correction: a real approval that the forge drops must still re-review."""
        mark_reviewed(url=_URL, sha=_SHA, state=ReviewState.APPROVED.value)
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": _URL, "sha": _SHA}],
            review_state_by_url={_URL: ReviewState.DISMISSED},
        )

        signals = ReviewerPrsScanner(host=host).scan()

        assert [s.kind for s in signals] == ["reviewer_pr.approval_dismissed"], (
            "silencing the phantom dismissal must not silence a genuine one"
        )

    def test_a_genuine_dismissal_reaches_persistence_and_creates_a_task(self) -> None:
        """End to end: emitting the signal is worthless if the dedup then swallows it.

        A discharged ticket carries ``discharged_sha == head``, so without invalidating it
        on a dismissal ``_already_reviewed_at_head`` returns True and the re-review this
        signal exists to request is silently dropped.
        """
        ticket = _discharged_reviewer_ticket()
        ticket.merge_extra(set_keys={"last_review_state": ReviewState.APPROVED.value})
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": _URL, "sha": _SHA}],
            review_state_by_url={_URL: ReviewState.DISMISSED},
        )

        signals = ReviewerPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["reviewer_pr.approval_dismissed"]
        persist_agent_actions(dispatch(signals))

        assert Task.objects.filter(ticket=ticket, phase="reviewing", status=Task.Status.PENDING).exists(), (
            "the dismissal was emitted and then swallowed by the at-head dedup"
        )
