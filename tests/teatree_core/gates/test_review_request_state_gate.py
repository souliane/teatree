"""Review-request REVIEWED-state + evidence gate (PR-08, item 1).

``require_reviewed_state_for_review_request`` is pinned per test by patching the
gate's ``get_effective_settings`` (the spec-coverage gate pattern) so the suite
is deterministic and never depends on the host config.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

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


def _reviewed(db) -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", issue_url="https://x/1", state=Ticket.State.REVIEWED)


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
    def test_refuses_non_reviewed_ticket(self, db) -> None:
        t = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.CODED)
        with _gate(required=True):
            refusal = check_reviewed_state(t)
        assert "not REVIEWED" in refusal

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
