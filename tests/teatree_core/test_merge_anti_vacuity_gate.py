"""Anti-vacuity gate wired into the merge precondition path (#1829).

Extends the §17.4.3 merge gate with the anti-vacuity dimension: with
a merge is refused unless the CLEAR's
ticket carries a complete, SHA-bound anti-vacuity attestation. The attestation
binds to the merge-time live head, so a stale-SHA attestation (the bug present
on a later, un-re-attested revision) is treated as absent.

Only the unstoppable external (``gh``) is stubbed; the gate, CLEAR, FSM, and DB
writes are real. The gate is unconditional, so the
suite is deterministic regardless of the host config.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import CriticVerdict, MergeClear, Rubric, Ticket
from tests.factories import waive_rubric
from tests.teatree_core.conftest import seed_merge_safe_verdict
from tests.teatree_core.test_merge_execution import _GhStub

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773 public-repo author gate — exercised by test_merge_execution_author_gate;
    # these pre-date it and target other concerns, so it is a no-op here.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


_SHA = "a" * 40
_OTHER_SHA = "b" * 40


def _clear(ticket: Ticket, *, pr_id: int = 859, head_sha: str = _SHA, waive: bool = True) -> MergeClear:
    # The rubric done-gate runs at this chokepoint; the real path has an independent
    # verifier grade the rubric, so the audited bypass stands in (cf. _seed_sibling_verdict).
    if waive:
        waive_rubric(ticket)
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=pr_id,
        slug="souliane/teatree",
        reviewed_sha=head_sha,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


def _merge(clear: MergeClear) -> object:
    # Seed the #2829 sibling verdict the real ``clear`` path records (the gate
    # is downstream of the anti-vacuity check, so a refuse test is unaffected).
    seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
    if clear.ticket_id is not None:
        CriticVerdict.record_from_envelope(
            ticket=clear.ticket,
            transition="merge",
            head_sha=clear.reviewed_sha,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [
                    {"slug": slug, "status": "pass", "citation": "inspected tests/x.py::test_y and the shipped diff"}
                    for slug in ("test_value", "cleanliness")
                ],
            },
        )
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_GhStub(head=clear.reviewed_sha)):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


class TestMergeAntiVacuityGate(TestCase):
    def test_merge_refused_without_attestation_when_gate_on(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="anti-vacuity"):
            _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert clear.consumed_at is None

    def test_merge_refused_with_stale_sha_attestation(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        ticket.record_anti_vacuity_attestation(_OTHER_SHA, "AC mapped", ["tests/x.py::test_y"])
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="stale"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_REQUESTED

    def test_merge_allowed_with_bound_complete_attestation(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        ticket.record_anti_vacuity_attestation(_SHA, "AC1-3 mapped", ["tests/x.py::test_y"])
        clear = _clear(ticket)
        _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_two_prs_on_one_ticket_merge_after_both_reviews(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        rubric = Rubric.populate(ticket, ["Both PRs have a reviewed regression test"])
        criterion = rubric.criteria.get(ordinal=0)
        criterion.record_grade(
            status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA, rationale="tests/x.py::test_first"
        )
        ticket.refresh_from_db(fields=["extra"])
        assert rubric.is_fully_passed_at(_SHA)
        assert not rubric.is_fully_passed_at(_OTHER_SHA)
        criterion.record_grade(
            status="pass", grader_identity="cold-reviewer", reviewed_sha=_OTHER_SHA, rationale="tests/x.py::test_second"
        )
        ticket.refresh_from_db(fields=["extra"])
        first = _clear(ticket, pr_id=859, head_sha=_SHA, waive=False)
        second = _clear(ticket, pr_id=860, head_sha=_OTHER_SHA, waive=False)
        ticket.record_anti_vacuity_attestation(_SHA, "First PR ACs mapped", ["tests/x.py::test_first"])
        ticket.record_anti_vacuity_attestation(_OTHER_SHA, "Second PR ACs mapped", ["tests/x.py::test_second"])

        first_outcome = _merge(first)
        second_outcome = _merge(second)

        assert first_outcome.merged_sha
        assert second_outcome.merged_sha
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.consumed_at is not None
        assert second.consumed_at is not None
        ticket.refresh_from_db(fields=["extra"])
        assert ticket.extra["anti_vacuity_attestations"] == {}
        assert ticket.extra["rubric_grades_by_head"] == {}
        ticket.record_anti_vacuity_attestation("c" * 40, "Third PR ACs mapped", ["tests/x.py::test_third"])
        assert set(ticket.extra["anti_vacuity_attestations"]) == {"c" * 40}
