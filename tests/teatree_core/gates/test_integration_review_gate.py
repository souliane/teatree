"""Cross-repo integration-review DoD gate on ``mark_delivered`` (PR-08, item 2).

The pure helpers are exercised directly; the FSM wiring is exercised through
``Ticket.mark_delivered`` so a ≥2-repo ticket cannot reach DELIVERED without an
integration-review artifact, while a single-repo ticket (and a covered ≥2-repo
ticket) can. ``require_integration_review`` is pinned per test by patching the
gate's ``get_effective_settings``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from teatree.config import UserSettings
from teatree.core.gates.integration_review_gate import IntegrationReviewError, check_integration_review, distinct_repos
from teatree.core.models import ReviewEvidence, Ticket

_SHA = "a" * 40


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.integration_review_gate.get_effective_settings",
        return_value=UserSettings(require_integration_review=required),
    ):
        yield


def _ticket(db, repos: list[str], **extra: object) -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.RETROSPECTED, repos=repos, extra=dict(extra))


def _integration_evidence(ticket: Ticket, repos: list[str]) -> ReviewEvidence:
    return ReviewEvidence.record(
        ticket=ticket,
        kind=ReviewEvidence.Kind.INTEGRATION_REVIEW,
        reviewer_identity="reviewer-bob",
        verdict="pass",
        head_sha=_SHA,
        repos=repos,
    )


class TestPureHelpers:
    def test_distinct_repos_dedups_and_strips(self, db) -> None:
        t = _ticket(db, ["org/a", " org/a ", "org/b", ""])
        assert distinct_repos(t) == ["org/a", "org/b"]


class TestGateFunction:
    def test_noop_when_setting_off(self, db) -> None:
        t = _ticket(db, ["org/a", "org/b"])
        with _gate(required=False):
            check_integration_review(t)  # no raise

    def test_single_repo_never_fires(self, db) -> None:
        t = _ticket(db, ["org/a"])
        with _gate(required=True):
            check_integration_review(t)  # no raise

    def test_two_repos_without_review_refused(self, db) -> None:
        t = _ticket(db, ["org/a", "org/b"])
        with _gate(required=True), pytest.raises(IntegrationReviewError, match="integration review"):
            check_integration_review(t)

    def test_two_repos_with_covering_review_allowed(self, db) -> None:
        t = _ticket(db, ["org/a", "org/b"])
        _integration_evidence(t, ["org/a", "org/b"])
        with _gate(required=True):
            check_integration_review(t)  # no raise

    def test_partial_review_still_refused(self, db) -> None:
        t = _ticket(db, ["org/a", "org/b", "org/c"])
        _integration_evidence(t, ["org/a", "org/b"])
        with _gate(required=True), pytest.raises(IntegrationReviewError):
            check_integration_review(t)

    def test_override_allows(self, db) -> None:
        t = _ticket(db, ["org/a", "org/b"], integration_review_override={"reason": "hotfix, coordinated manually"})
        with _gate(required=True):
            check_integration_review(t)  # no raise

    def test_refusal_uses_real_record_evidence_flag(self, db) -> None:
        # The remediation must hand a command the CLI accepts: `record-evidence`
        # takes `--repos <a,b>` comma-separated (management/commands/review.py),
        # NOT a repeated `--repo <r>` per repo.
        t = _ticket(db, ["org/a", "org/b"])
        with _gate(required=True), pytest.raises(IntegrationReviewError) as excinfo:
            check_integration_review(t)
        msg = str(excinfo.value)
        assert "--repos org/a,org/b" in msg
        assert "--repo org/a --repo org/b" not in msg


class TestFsmWiring:
    def test_two_repo_ticket_cannot_deliver_without_review(self, db) -> None:
        t = _ticket(db, ["org/a", "org/b"])
        with _gate(required=True), pytest.raises(IntegrationReviewError):
            t.mark_delivered()
        t.refresh_from_db()
        assert t.state == Ticket.State.RETROSPECTED

    def test_two_repo_ticket_delivers_with_review(self, db) -> None:
        t = _ticket(db, ["org/a", "org/b"])
        _integration_evidence(t, ["org/a", "org/b"])
        with _gate(required=True):
            t.mark_delivered()
            t.save()
        t.refresh_from_db()
        assert t.state == Ticket.State.DELIVERED

    def test_single_repo_ticket_delivers_normally(self, db) -> None:
        # The normal single-repo flow is never blocked.
        t = _ticket(db, ["org/a"])
        with _gate(required=True):
            t.mark_delivered()
            t.save()
        t.refresh_from_db()
        assert t.state == Ticket.State.DELIVERED
