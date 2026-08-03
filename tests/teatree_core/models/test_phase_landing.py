"""Did a phase's work LAND? — the evidence a lost lease may not overrule (#3982).

The observed defect: a shipping task pushed its branch, opened its PR and advanced its
ticket to ``in_review``, yet was recorded ``failed`` / ``lease_lost``. ``in_review`` sits
OFF ``Ticket._WORK_STATE_ORDER``, so ``has_completed_phase`` answers False for a ticket
that has demonstrably shipped — these pin the fuller author-ladder answer plus the
shipping artifact (an attached pull request) the issue names as evidence.

Every call below passes ``trust_shipping_artifact=True`` to exercise the FULL predicate —
the narrower ``False`` scope (a stray PR must not excuse a genuinely deterministic
failure) is pinned directly here too, and at the sweep integration level in
``tests/teatree_loop/test_transient_requeue.py``.
"""

from django.test import TestCase

from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.phase_landing import phase_landing_evidence


def _task(*, phase: str, state: str, role: str = Ticket.Role.AUTHOR) -> Task:
    ticket = Ticket.objects.create(role=role, state=state)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase)


class TestLadderEvidence(TestCase):
    def test_in_review_is_evidence_that_shipping_landed(self) -> None:
        # The #3982 case: has_completed_phase says False here because IN_REVIEW is off
        # the linear work ladder; the phase's output was nonetheless produced.
        task = _task(phase="shipping", state=Ticket.State.IN_REVIEW)

        assert task.ticket.has_completed_phase("shipping") is False
        assert "in_review" in phase_landing_evidence(task, trust_shipping_artifact=True)

    def test_every_state_past_shipped_is_evidence(self) -> None:
        for state in (Ticket.State.SHIPPED, Ticket.State.MERGED, Ticket.State.RETROSPECTED, Ticket.State.DELIVERED):
            assert phase_landing_evidence(_task(phase="shipping", state=state), trust_shipping_artifact=True), state

    def test_a_state_behind_the_phase_target_is_not_evidence(self) -> None:
        assert (
            phase_landing_evidence(_task(phase="shipping", state=Ticket.State.REVIEWED), trust_shipping_artifact=True)
            == ""
        )
        assert (
            phase_landing_evidence(_task(phase="coding", state=Ticket.State.PLANNED), trust_shipping_artifact=True)
            == ""
        )

    def test_the_short_verb_phase_spelling_normalizes(self) -> None:
        assert phase_landing_evidence(_task(phase="ship", state=Ticket.State.IN_REVIEW), trust_shipping_artifact=True)

    def test_an_off_ladder_state_yields_no_evidence(self) -> None:
        # REVIEW_POSTED / IGNORED carry no position on the author ladder, so they can
        # prove nothing about an author phase — the conservative answer is "no evidence".
        assert (
            phase_landing_evidence(
                _task(phase="shipping", state=Ticket.State.REVIEW_POSTED), trust_shipping_artifact=True
            )
            == ""
        )
        assert (
            phase_landing_evidence(_task(phase="shipping", state=Ticket.State.IGNORED), trust_shipping_artifact=True)
            == ""
        )

    def test_a_free_form_phase_has_no_ladder_target(self) -> None:
        assert (
            phase_landing_evidence(_task(phase="bughunt", state=Ticket.State.DELIVERED), trust_shipping_artifact=True)
            == ""
        )

    def test_a_reviewer_ticket_yields_no_author_phase_evidence(self) -> None:
        task = _task(phase="shipping", state=Ticket.State.IN_REVIEW, role=Ticket.Role.REVIEWER)
        assert phase_landing_evidence(task, trust_shipping_artifact=True) == ""


class TestShippingArtifactEvidence(TestCase):
    def _pr(self, ticket: Ticket, *, state: str) -> PullRequest:
        return PullRequest.objects.create(
            ticket=ticket,
            url=f"https://github.com/o/r/pull/{ticket.pk}",
            repo="o/r",
            iid=str(ticket.pk),
            state=state,
        )

    def test_an_open_pull_request_is_evidence_shipping_landed(self) -> None:
        # The ticket state lagged (the transition never fired) but the phase's artifact
        # exists — re-running shipping would open a SECOND pull request.
        task = _task(phase="shipping", state=Ticket.State.REVIEWED)
        pr = self._pr(task.ticket, state=PullRequest.State.OPEN)

        assert pr.url in phase_landing_evidence(task, trust_shipping_artifact=True)

    def test_a_closed_pull_request_is_not_evidence(self) -> None:
        task = _task(phase="shipping", state=Ticket.State.REVIEWED)
        self._pr(task.ticket, state=PullRequest.State.CLOSED)

        assert phase_landing_evidence(task, trust_shipping_artifact=True) == ""

    def test_the_artifact_branch_is_shipping_only(self) -> None:
        task = _task(phase="coding", state=Ticket.State.PLANNED)
        self._pr(task.ticket, state=PullRequest.State.OPEN)

        assert phase_landing_evidence(task, trust_shipping_artifact=True) == ""

    def test_untrusted_artifact_is_not_evidence_even_when_open(self) -> None:
        # A stray PR can be opened independently of ship() (the no-orphan pre-push gate,
        # the PendingPullRequest drain). When the caller has NOT established this row's own
        # failure was a lease loss, the artifact must not count as evidence — see the
        # sweep-level pin in tests/teatree_loop/test_transient_requeue.py.
        task = _task(phase="shipping", state=Ticket.State.REVIEWED)
        self._pr(task.ticket, state=PullRequest.State.OPEN)

        assert phase_landing_evidence(task, trust_shipping_artifact=False) == ""
