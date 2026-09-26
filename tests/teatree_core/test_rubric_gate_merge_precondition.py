"""Rubric->verifier done-gate wired into the merge precondition path (#2241).

Extends the §17.4.3 keystone-merge gate with the rubric dimension: a merge is refused
unless the CLEAR's ticket carries a rubric that is fully PASS — every criterion graded
PASS by an INDEPENDENT verifier (grader != maker), citing what proves it, bound to the
merge-time live head SHA. It is fail-closed: an empty / ungraded / failed / uncited /
maker-graded / stale-SHA rubric refuses the merge, and no setting relaxes it.

The anti-vacuous pairing is :meth:`test_merge_refused_on_failed_criterion` (#1, must
go RED before the gate is wired) + :meth:`test_merge_allowed_when_all_pass_at_head`
(#6) — a gate that passes against an unsatisfied rubric guards nothing.

Only the unstoppable external (``gh``) is stubbed; the gate, CLEAR, FSM, and DB
writes are real.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeClear, Rubric, RubricCriterion, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.rubric import PHASE_CRITERIA
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
_GRADER = "cold-reviewer"
_CITATION = "unit: tests/teatree_core/test_rubric_gate_merge_precondition.py"


def _clear(ticket: Ticket | None) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=2241,
        slug="souliane/teatree",
        reviewed_sha=_SHA,
        reviewer_identity=_GRADER,
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


def _merge(clear: MergeClear) -> object:
    # Seed the #2829 sibling verdict the real ``clear`` path records (the gate
    # is downstream of the rubric check, so a refuse-on-rubric test is unaffected).
    seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_GhStub(head=_SHA)):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


def _bypass(ticket: Ticket) -> PlanArtifact:
    return PlanArtifact.record_bypass(
        ticket=ticket, plan_text="[audited bypass by alice] urgent hotfix", recorded_by="alice"
    )


def _passing_rubric(ticket: Ticket, *, sha: str = _SHA, grader: str = _GRADER) -> Rubric:
    rubric = Rubric.populate(ticket, ["AC1", "AC2"])
    for criterion in rubric.criteria.all():
        criterion.record_grade(status="pass", grader_identity=grader, reviewed_sha=sha, rationale=_CITATION)
    return rubric


class TestRubricGateMergePrecondition(TestCase):
    def test_merge_allowed_when_all_pass_at_head(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _passing_rubric(ticket)
        clear = _clear(ticket)
        _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_merge_refused_on_failed_criterion(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        rubric = _passing_rubric(ticket)
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity=_GRADER, reviewed_sha=_SHA)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="FAIL"):
            _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_merge_refused_on_ungraded_criterion(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        rubric = Rubric.populate(ticket, ["AC1", "AC2"])
        rubric.criteria.get(ordinal=0).record_grade(
            status="pass", grader_identity=_GRADER, reviewed_sha=_SHA, rationale=_CITATION
        )
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="ungraded"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_refused_when_grader_is_maker(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        rubric = Rubric.populate(ticket, ["AC1"])
        # A maker-graded row bypasses the guarded factory (which refuses it) so the
        # done-gate is proven to refuse a self-attested rubric independently.
        RubricCriterion.objects.filter(rubric=rubric, ordinal=0).update(
            status=RubricCriterion.Status.PASS,
            grader_identity="merge-loop",
            reviewed_sha=_SHA,
        )
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="maker"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_refused_on_stale_sha_grade(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _passing_rubric(ticket, sha=_OTHER_SHA)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="stale"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_refused_on_empty_rubric(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        Rubric.objects.create(ticket=ticket)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="no criteria"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_refused_when_no_rubric_recorded(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="no rubric"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_a_bypassed_ticket_merges_over_pending_phase_criteria(self) -> None:
        # A phase visit seeds a standing criterion onto every ticket's rubric, so an
        # audited bypass that must merge would be blocked by content it never declared.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        Rubric.populate(ticket, [PHASE_CRITERIA["coding"]])
        _bypass(ticket)
        _merge(_clear(ticket))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_a_bypass_never_merges_over_a_recorded_fail(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        rubric = _passing_rubric(ticket)
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity=_GRADER, reviewed_sha=_SHA)
        _bypass(ticket)
        clear = _clear(ticket)
        with pytest.raises(MergePreconditionError, match="FAIL"):
            _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_a_ticketless_clear_has_no_rubric_subject_and_merges(self) -> None:
        # The rubric is FK'd to the ticket, so a CLEAR no ticket owns has nothing to
        # grade and no ticket a bypass could be recorded on — outside the gate's subject.
        clear = _clear(None)
        _merge(clear)
        clear.refresh_from_db()
        assert clear.consumed_at is not None
