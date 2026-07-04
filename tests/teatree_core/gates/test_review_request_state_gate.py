"""Review-request REVIEWED-state + evidence gate (PR-08, item 1).

``require_reviewed_state_for_review_request`` is pinned per test by patching the
gate's ``get_effective_settings`` (the spec-coverage gate pattern) so the suite
is deterministic and never depends on the host config.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from teatree.config import UserSettings
from teatree.core.gates.review_request_state_gate import check_reviewed_state
from teatree.core.models import ReviewEvidence, ReviewVerdict, Ticket

_SHA = "a" * 40


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.review_request_state_gate.get_effective_settings",
        return_value=UserSettings(require_reviewed_state_for_review_request=required),
    ):
        yield


def _ticket(state: str) -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", issue_url="https://x/1", state=state)


def _reviewed(db) -> Ticket:
    return _ticket(Ticket.State.REVIEWED)


def _cold_evidence(ticket: Ticket) -> ReviewEvidence:
    return ReviewEvidence.record(
        ticket=ticket,
        kind=ReviewEvidence.Kind.COLD_REVIEW,
        reviewer_identity="reviewer-bob",
        verdict="merge_safe",
        head_sha=_SHA,
    )


class TestGateOff:
    def test_noop_when_setting_off(self, db) -> None:
        t = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.CODED)
        with _gate(required=False):
            assert check_reviewed_state(t) == ""


class TestGateOn:
    def test_refuses_pre_review_ticket(self, db) -> None:
        t = _ticket(Ticket.State.CODED)
        with _gate(required=True):
            refusal = check_reviewed_state(t)
        assert "before the REVIEWED milestone" in refusal

    def test_refuses_reviewed_ticket_without_evidence(self, db) -> None:
        t = _reviewed(db)
        with _gate(required=True):
            refusal = check_reviewed_state(t)
        assert "no recorded review-evidence artifact" in refusal

    def test_allows_reviewed_ticket_with_cold_evidence(self, db) -> None:
        t = _reviewed(db)
        _cold_evidence(t)
        with _gate(required=True):
            assert check_reviewed_state(t) == ""

    def test_allows_reviewed_ticket_with_review_verdict_bridge(self, db) -> None:
        # The cold-review step records a ReviewVerdict; that satisfies the gate
        # without a separate ReviewEvidence row (the "recordable by the
        # cold-review step" contract).
        t = _reviewed(db)
        ReviewVerdict.record(
            pr_id=7,
            slug="org/repo",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="reviewer-bob",
            ticket=t,
        )
        with _gate(required=True):
            assert check_reviewed_state(t) == ""


class TestGateOnPostReviewProgression:
    """PR-08b wave-2 audit: exercise the ENABLED gate with the realistic broadcast state.

    The FSM advances review → ship → request_review BEFORE the review-request
    broadcast fires, so a canonically-progressed ticket sits in SHIPPED/IN_REVIEW
    (the sibling ``TestReviewRequestPostAntiVacuityGate`` already models the
    broadcast-time state as IN_REVIEW). The old strict ``state == REVIEWED``
    check refused every such ticket when the gate was ENABLED — the gate was
    unusable-when-enabled, and the prior tests never caught it because they
    froze the ticket at the momentary REVIEWED. These tests turn the gate ON and
    exercise the live progressed states.
    """

    def test_allows_in_review_ticket_with_evidence(self, db) -> None:
        # RED against the pre-PR-08b strict ``state == REVIEWED`` gate: a ticket
        # whose FSM already reached IN_REVIEW (the real broadcast-time state)
        # WITH a recorded review-evidence artifact was refused ("not REVIEWED"),
        # so the enabled gate blocked every progressed ticket. It must ALLOW.
        t = _ticket(Ticket.State.IN_REVIEW)
        _cold_evidence(t)
        with _gate(required=True):
            assert check_reviewed_state(t) == ""

    def test_refuses_pre_review_coded_ticket_even_with_evidence(self, db) -> None:
        # The other half of the anti-vacuity pair: a pre-review state is STILL
        # refused when the gate is enabled — even if evidence somehow exists —
        # so the widened predicate did not collapse into "always allow".
        t = _ticket(Ticket.State.CODED)
        _cold_evidence(t)
        with _gate(required=True):
            refusal = check_reviewed_state(t)
        assert "before the REVIEWED milestone" in refusal

    @pytest.mark.parametrize(
        "state",
        [
            Ticket.State.REVIEWED,
            Ticket.State.SHIPPED,
            Ticket.State.IN_REVIEW,
            Ticket.State.MERGED,
            Ticket.State.RETROSPECTED,
            Ticket.State.DELIVERED,
        ],
    )
    def test_allows_every_post_review_state_with_evidence(self, db, state: str) -> None:
        t = _ticket(state)
        _cold_evidence(t)
        with _gate(required=True):
            assert check_reviewed_state(t) == "", f"{state} was refused despite passing review"

    @pytest.mark.parametrize(
        "state",
        [
            Ticket.State.NOT_STARTED,
            Ticket.State.SCOPED,
            Ticket.State.STARTED,
            Ticket.State.PLANNED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            # IGNORED is reachable from any state (incl. pre-review), so it is
            # never a post-review signal — it must be refused.
            Ticket.State.IGNORED,
        ],
    )
    def test_refuses_every_non_post_review_state(self, db, state: str) -> None:
        t = _ticket(state)
        _cold_evidence(t)
        with _gate(required=True):
            refusal = check_reviewed_state(t)
        assert "before the REVIEWED milestone" in refusal, f"{state} was allowed but is not post-review"
