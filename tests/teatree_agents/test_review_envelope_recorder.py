"""The reviewing recorder is the rubric's PRODUCER — the done-gate's only automatic grader.

The done-gate is unconditional: an ungraded rubric refuses the merge. Nothing in the
tree graded one automatically, so every ticketed factory PR needed a human to grade by
hand. The reviewer already IS the independent verifier the gate requires (grader !=
maker, one per dispatched head), so its returned envelope carries the grades and THIS
actor — the orchestrator, not the reviewer — stamps them.

Two properties carry the weight, and each has its own pin below. COVERAGE: a verdict
that leaves any criterion PENDING records NOTHING, because a verdict recorded over a
half-graded rubric resolves the review claim and never re-arms, leaving the head
unmergeable forever. ATOMICITY: the verdict, the claim retirement, the lock release and
the grades land in ONE transaction, so a refused grade rolls the verdict back with it.
"""

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.envelope_refusal import is_recorder_refusal
from teatree.core.gates.rubric_gate import RubricNotSatisfiedError, check_rubric_satisfied
from teatree.core.models import (
    AutoReviewDispatch,
    HonestyEscalation,
    MRReviewLock,
    PullRequest,
    ReviewVerdict,
    Rubric,
    RubricCriterion,
    Session,
    Task,
    Ticket,
)
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.types import AdequacySection, PlanAdequacy

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 2241
_HEAD = "3c7e1a95f0b482d6e13470ac95bd28ef6104a7b9"
_BASE = "d" * 40
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_REVIEWER = "cold-reviewer-agent"
_AGENT = "11111111-2222-3333-4444-555555555555"
_CITATION = "unit: tests/teatree_agents/test_review_envelope_recorder.py"
_AC = ["the recorder stamps every returned grade", "an ungraded criterion records nothing"]


def _planned_adequacy(criteria: list[str]) -> PlanAdequacy:
    return PlanAdequacy(
        design=AdequacySection(content="the producer records the reviewer's grades"),
        integration_seams=AdequacySection(content=["src/teatree/agents/review_envelope_recorder.py"]),
        edge_cases=AdequacySection(content=["a PR no ticket owns"]),
        test_strategy=AdequacySection(content="this module"),
        acceptance_criteria=AdequacySection(content=criteria),
    )


def _author_ticket(*, criteria: list[str] | None = _AC, pr_id: int = _PR_ID) -> Ticket:
    """The ticket the PR DELIVERS — a maker-role row the PR ledger points at.

    Distinct from the reviewing task's own ``task.ticket``, which is the reviewer-role
    row keyed by the PR url. That distinction is the whole subject of this module: the
    rubric lives on the ticket the PR delivers, so the recorder must resolve it the way
    the merge gate does rather than reach for the task's own ticket.
    """
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
    Session.objects.create(overlay="t3-teatree", ticket=ticket, agent_id=_AGENT)
    PullRequest.objects.create(
        ticket=ticket, overlay="t3-teatree", url=f"https://github.com/{_SLUG}/pull/{pr_id}", repo=_SLUG, iid=str(pr_id)
    )
    if criteria is not None:
        PlanArtifact.record(
            ticket=ticket,
            plan_text="produce the grades from the reviewing envelope",
            recorded_by="planner",
            base_sha=_BASE,
            adequacy=_planned_adequacy(criteria),
        )
    return ticket


def _reviewing_task_via_dispatch(*, pr_id: int = _PR_ID, head_sha: str = _HEAD) -> Task:
    dispatch = AutoReviewDispatch.enqueue(
        slug=_SLUG,
        pr_id=pr_id,
        head_sha=head_sha,
        pr_url=f"https://github.com/{_SLUG}/pull/{pr_id}",
        overlay="teatree",
    )
    assert dispatch is not None
    task = dispatch.task
    assert task is not None
    task.claim(claimed_by="headless-reviewer")
    return task


def _envelope(
    *,
    grades: list[dict[str, object]] | None = None,
    verdict: str = "merge_safe",
    reviewer: str = _REVIEWER,
    reviewed_sha: str = _HEAD,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "verdict": verdict,
        "reviewed_sha": reviewed_sha,
        "reviewer_identity": reviewer,
        "gh_verify_result": "green",
        "blast_class": "logic",
        "findings": [],
    }
    if grades is not None:
        payload["rubric_grades"] = grades
    return {"summary": "Independent cold review of the pull request.", "review_verdict": payload}


def _envelope_with_raw_grades(payload: object) -> dict[str, object]:
    """An envelope whose ``rubric_grades`` is *payload* verbatim — the malformed-shape seam."""
    envelope = _envelope()
    verdict = envelope["review_verdict"]
    assert isinstance(verdict, dict)
    verdict["rubric_grades"] = payload
    return envelope


def _full_pass() -> list[dict[str, object]]:
    return [{"ordinal": index, "status": "pass", "rationale": _CITATION} for index in range(len(_AC))]


def _criteria(ticket: Ticket) -> list[RubricCriterion]:
    rubric = Rubric.objects.active_for_ticket(ticket)
    assert rubric is not None
    return list(rubric.criteria.all())


class TestTheReviewerGradesTheRubricItVerifies(TestCase):
    def test_returned_grades_land_on_the_gated_ticket_at_the_dispatch_head(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(grades=_full_pass()), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        graded = _criteria(ticket)
        assert [c.status for c in graded] == [RubricCriterion.Status.PASS, RubricCriterion.Status.PASS]
        # The DISPATCH head and the verdict's own grader — never a self-asserted sha.
        assert {c.reviewed_sha for c in graded} == {_HEAD}
        assert {c.grader_identity for c in graded} == {_REVIEWER}
        # The whole point: the merge gate now passes at this head.
        check_rubric_satisfied(ticket, _HEAD, transition="merge")

    def test_a_full_pass_clears_the_honesty_escalation(self) -> None:
        ticket = _author_ticket()
        HonestyEscalation.record(HonestyEscalation.Reason.SHIPPED_INCOMPLETE, session_id=_AGENT)
        assert HonestyEscalation.is_active(_AGENT) is True

        record_result_envelope(_reviewing_task_via_dispatch(), _envelope(grades=_full_pass()), phase="reviewing")

        assert HonestyEscalation.is_active(_AGENT) is False
        assert Rubric.objects.active_for_ticket(ticket) is not None


class TestAnUngradedCriterionRecordsNothing(TestCase):
    """THE anti-vacuity pin: partial coverage must not resolve the review claim.

    A verdict recorded over a half-graded rubric retires the per-head claim and
    releases the lock, so nothing re-arms review — while the done-gate goes on
    refusing the merge for the PENDING criterion. The head is then unmergeable
    forever, with no reviewer left to fix it. The refusal has to come BEFORE the
    write, which is why coverage is a DB question only this actor can answer.
    """

    def test_a_verdict_that_grades_no_criterion_records_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "#0" in attempt.error
        assert "#1" in attempt.error

    def test_a_partial_grade_records_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task, _envelope(grades=[{"ordinal": 0, "status": "pass", "rationale": _CITATION}]), phase="reviewing"
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "#1" in attempt.error
        assert "#0" not in attempt.error

    def test_a_non_list_grades_payload_records_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task, _envelope_with_raw_grades({"ordinal": 0, "status": "pass"}), phase="reviewing"
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert '"rubric_grades" was a dict' in attempt.error


class TestAMalformedGradeItemRecordsNothing(TestCase):
    """A malformed ITEM takes the envelope refusal, never an exception (#4574).

    The container was normalised and the items were not, so a reviewer that returned
    ``["pass"]`` crashed the recorder instead of being refused. The traceback surfaced as
    an ``sdk_error`` no refusal marker matches, so ``transient_requeue`` withheld the
    corrective retry and one dispatch attempt burned per occurrence — three of them
    saturate the head and page the owner with a traceback instead of a diagnosis.
    """

    def _refused(self, payload: object) -> tuple[Ticket, Task, str]:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        attempt = record_result_envelope(task, _envelope_with_raw_grades(payload), phase="reviewing")
        return ticket, task, attempt.error

    def _assert_nothing_recorded(self, ticket: Ticket, task: Task, error: str) -> None:
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert is_recorder_refusal(error)

    def test_a_bare_string_item_is_refused_by_name(self) -> None:
        ticket, task, error = self._refused(["pass", "pass"])

        self._assert_nothing_recorded(ticket, task, error)
        assert "must be an object, not a str" in error

    def test_an_item_naming_the_wrong_status_field_is_refused_by_name(self) -> None:
        # Every criterion is COVERED, so this reaches the stamping loop the coverage
        # check guards — the shape refusal has to come from the normaliser itself.
        ticket, task, error = self._refused([{"ordinal": 0, "grade": "pass"}, {"ordinal": 1, "grade": "pass"}])

        self._assert_nothing_recorded(ticket, task, error)
        assert "needs an ordinal and a status" in error

    def test_a_non_integer_ordinal_is_refused_by_name(self) -> None:
        ticket, task, error = self._refused([{"ordinal": "first", "status": "pass"}])

        self._assert_nothing_recorded(ticket, task, error)
        assert "a grade ordinal must be an integer" in error


class TestARefusedGradeRollsTheVerdictBack(TestCase):
    def test_a_refused_grade_rolls_the_verdict_back(self) -> None:
        # Full coverage, so the pre-write check passes; the SECOND grade is an
        # uncited PASS the guarded factory refuses. Without the shared atomic the
        # verdict is already written and the claim already retired when that
        # refusal lands, so the head can never be re-reviewed.
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task,
            _envelope(
                grades=[
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 1, "status": "pass"},
                ]
            ),
            phase="reviewing",
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        # The guarded factory's own words: the coverage refusal one layer up quotes
        # `"rationale": "<what proves it>"` in its remediation, so the bare token is
        # satisfied by a normaliser that silently DROPPED this grade instead.
        assert "a PASS needs a rationale citing what proves the criterion" in attempt.error

    def test_a_maker_identity_records_neither_verdict_nor_grade(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        record_result_envelope(task, _envelope(grades=_full_pass(), reviewer="merge-loop"), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_a_divergent_self_asserted_head_grades_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        record_result_envelope(task, _envelope(grades=_full_pass(), reviewed_sha="0" * 39 + "1"), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


class TestAFailGradeIsRecordedAndNoBypassOverridesIt(TestCase):
    def test_hold_with_a_fail_grade_is_recorded_and_the_bypass_never_overrides_it(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task,
            _envelope(
                verdict="hold",
                grades=[
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 1, "status": "fail", "rationale": "the recorder ignores the second criterion"},
                ],
            ),
            phase="reviewing",
        )

        assert attempt.error == ""
        recorded = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD)
        assert not recorded.is_merge_safe()
        assert [c.status for c in _criteria(ticket)] == [RubricCriterion.Status.PASS, RubricCriterion.Status.FAIL]

        PlanArtifact.record_bypass(
            ticket=ticket, plan_text="[audited bypass by tests] nothing to declare", recorded_by="a-human"
        )
        with pytest.raises(RubricNotSatisfiedError, match="graded FAIL"):
            check_rubric_satisfied(ticket, _HEAD, transition="merge")

    def test_merge_safe_over_a_fail_grade_records_nothing(self) -> None:
        # The mirror of ``_assert_checks_admit_merge_safe``, one field over: a reviewer
        # that grades a criterion FAIL has said the ticket is not done, so merge_safe
        # contradicts its own envelope. Recorded, it retires the claim and releases the
        # lock, and the done-gate then refuses the merge on the never-overridable FAIL.
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task,
            _envelope(
                grades=[
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 1, "status": "fail", "rationale": "the recorder ignores the second criterion"},
                ]
            ),
            phase="reviewing",
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "#1" in attempt.error
        assert is_recorder_refusal(attempt.error)


class TestOutsideTheGatesSubjectNoGradesAreOwed(TestCase):
    """Byte-for-byte the subject ``ticket_gates`` already SKIPS — nothing re-widened."""

    def test_a_pr_no_ticket_owns_requires_no_grades(self) -> None:
        # No PullRequest row and no MergeClear: a colleague's / dependabot's PR, which
        # the merge gate never grades because there is no ticket to record a bypass on.
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()

    def test_a_rubric_less_ticket_requires_no_grades(self) -> None:
        ticket = _author_ticket(criteria=None)
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert Rubric.objects.active_for_ticket(ticket) is None

    def test_a_malformed_grade_on_a_rubric_less_ticket_still_records_the_verdict(self) -> None:
        # The per-item refusal is scoped to a rubric that EXISTS: with none, no grade is
        # ever stamped, so refusing here would refuse a verdict that owes no grades.
        ticket = _author_ticket(criteria=None)
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope_with_raw_grades(["pass"]), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert Rubric.objects.active_for_ticket(ticket) is None


class TestAPhaseOutsideTheGradedSetOwesNoGrades(TestCase):
    """This seam's OWN phase narrowing, which ``ticket_gates`` knows nothing about."""

    def test_e2e_reviewing_owes_no_grades_because_its_brief_never_shows_them(self) -> None:
        # ``e2e_reviewing`` records through the shell ``t3 <overlay> review record`` from
        # its own skill and its brief carries no rubric block, so requiring coverage
        # there refuses a checklist the agent was never handed.
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(), phase="e2e_reviewing")

        assert attempt.error == ""
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]

    def test_grades_on_a_non_review_phase_are_never_read(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        record_result_envelope(task, {"summary": "coded it", **_envelope(grades=_full_pass())}, phase="coding")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
