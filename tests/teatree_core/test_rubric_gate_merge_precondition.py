"""Rubric->verifier done-gate wired into the merge precondition path (#2241).

Extends the §17.4.3 keystone-merge gate with the rubric dimension: with
``require_rubric_verification`` on, a merge is refused unless the CLEAR's ticket
carries a rubric that is fully PASS — every criterion graded PASS by an INDEPENDENT
verifier (grader != maker) bound to the merge-time live head SHA. It is fail-closed:
an empty / ungraded / failed / maker-graded / stale-SHA rubric refuses the merge.

The anti-vacuous pairing is :meth:`test_merge_refused_on_failed_criterion` (#1, must
go RED before the gate is wired) + :meth:`test_merge_allowed_when_all_pass_at_head`
(#6) — a gate that passes against an unsatisfied rubric guards nothing.

Only the unstoppable external (``gh``) is stubbed; the gate, CLEAR, FSM, and DB
writes are real. ``require_rubric_verification`` is pinned per test so the suite is
deterministic regardless of the host config.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeClear, Rubric, RubricCriterion, Ticket
from tests.teatree_core.test_merge_execution import _GhStub

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773 public-repo author gate — exercised by test_merge_execution_author_gate;
    # these pre-date it and target other concerns, so it is a no-op here.
    monkeypatch.setattr("teatree.core.merge.execution.assert_public_repo_author_trusted", lambda **_: None)


_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_GRADER = "cold-reviewer"


def _clear(ticket: Ticket) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=2241,
        slug="souliane/teatree",
        reviewed_sha=_SHA,
        reviewer_identity=_GRADER,
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch(
        "teatree.core.gates.rubric_gate.get_effective_settings",
        return_value=UserSettings(require_rubric_verification=required),
    ):
        yield


def _merge(clear: MergeClear) -> object:
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_GhStub(head=_SHA)):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


def _passing_rubric(ticket: Ticket, *, sha: str = _SHA, grader: str = _GRADER) -> Rubric:
    rubric = Rubric.populate(ticket, ["AC1", "AC2"])
    for criterion in rubric.criteria.all():
        criterion.record_grade(status="pass", grader_identity=grader, reviewed_sha=sha)
    return rubric


class TestRubricGateMergePrecondition(TestCase):
    def test_merge_allowed_when_all_pass_at_head(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _passing_rubric(ticket)
        clear = _clear(ticket)
        with _gate(required=True):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_merge_refused_on_failed_criterion(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        rubric = _passing_rubric(ticket)
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity=_GRADER, reviewed_sha=_SHA)
        clear = _clear(ticket)
        with _gate(required=True), pytest.raises(MergePreconditionError, match="FAIL"):
            _merge(clear)
        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert clear.consumed_at is None

    def test_merge_refused_on_ungraded_criterion(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        rubric = Rubric.populate(ticket, ["AC1", "AC2"])
        rubric.criteria.get(ordinal=0).record_grade(status="pass", grader_identity=_GRADER, reviewed_sha=_SHA)
        clear = _clear(ticket)
        with _gate(required=True), pytest.raises(MergePreconditionError, match="ungraded"):
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
        with _gate(required=True), pytest.raises(MergePreconditionError, match="maker"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_refused_on_stale_sha_grade(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        _passing_rubric(ticket, sha=_OTHER_SHA)
        clear = _clear(ticket)
        with _gate(required=True), pytest.raises(MergePreconditionError, match="stale"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_refused_on_empty_rubric(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        Rubric.objects.create(ticket=ticket)
        clear = _clear(ticket)
        with _gate(required=True), pytest.raises(MergePreconditionError, match="no criteria"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_refused_when_no_rubric_recorded(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with _gate(required=True), pytest.raises(MergePreconditionError, match="no rubric"):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_merge_unaffected_when_gate_off(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = _clear(ticket)
        with _gate(required=False):
            _merge(clear)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
