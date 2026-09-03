"""Headless reviewer records its verdict via the result envelope (corr-11).

The verdict is recorded through the RESULT ENVELOPE by maker≠checker design: the
reviewer RETURNS a typed ``review_verdict`` and the orchestrator
(``record_result_envelope`` — a DIFFERENT actor) records the ``ReviewVerdict``
server-side, resolving the per-MR :class:`MRReviewLock`. Routing the recording
through the orchestrator (never a reviewer-side
``t3 review record``) is what keeps the maker (the review sub-agent) from also
being the checker (the actor that persists the verdict). These tests drive the
orchestrator path directly with a returned envelope and assert the verdict lands
and the lock releases.
"""

import datetime as dt
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents import attempt_recorder
from teatree.agents.attempt_recorder import record_result_envelope, validate_result_keys
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, check_evidence
from teatree.core.modelkit.diff_scope import ChangedFileSet
from teatree.core.modelkit.forge_readability import CHECKS_UNREADABLE, LiveChecksRead
from teatree.core.models import (
    AutoReviewDispatch,
    CodexReviewMarker,
    DeferredQuestion,
    MRReviewLock,
    ReviewVerdict,
    Session,
    Task,
    TaskAttempt,
    Ticket,
)
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS
from teatree.core.models.phase_landing import phase_landing_evidence
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence_self_pr_review import handle_self_pr_review

if TYPE_CHECKING:
    from collections.abc import Callable

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 4242
_HEAD = "1f4b9c2ad0e7f61c83b25d90ac174e5f60a1b2c3"
_OTHER_HEAD = "f89874729bb0a41ce6d5713a2c0e9f38b7a1d4e5"


def _reviewing_task_via_dispatch(*, pr_id: int = _PR_ID) -> tuple[Task, AutoReviewDispatch]:
    dispatch = AutoReviewDispatch.enqueue(
        slug=_SLUG,
        pr_id=pr_id,
        head_sha=_HEAD,
        pr_url=f"https://github.com/{_SLUG}/pull/{pr_id}",
        overlay="teatree",
    )
    assert dispatch is not None
    task = dispatch.task
    assert task is not None
    task.claim(claimed_by="headless-reviewer")
    return task, dispatch


def _verdict_envelope(
    *,
    verdict: str = "merge_safe",
    reviewer: str = "cold-reviewer-agent",
    reviewed_sha: str = _HEAD,
) -> dict[str, object]:
    return {
        "summary": "Completed an independent cold review of the pull request.",
        "review_verdict": {
            "verdict": verdict,
            "reviewed_sha": reviewed_sha,
            "reviewer_identity": reviewer,
            "gh_verify_result": "green",
            "findings": [],
        },
    }


def _reviewing_task_on_reviewer_ticket(*, pr_id: int = _PR_ID, reviewed_sha: str = _HEAD) -> Task:
    """A reviewing task whose PR target is the reviewer ticket itself — no dispatch row."""
    ticket = Ticket.objects.create(
        issue_url=f"https://github.com/{_SLUG}/pull/{pr_id}",
        overlay="teatree",
        role=Ticket.Role.REVIEWER,
        extra={"reviewed_sha": reviewed_sha},
    )
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.PENDING)


def _verdict_envelope_without_reviewed_sha() -> dict[str, object]:
    envelope = _verdict_envelope()
    verdict = envelope["review_verdict"]
    assert isinstance(verdict, dict)
    del verdict["reviewed_sha"]
    return envelope


class TestHeadlessReviewerRecordsVerdictWithoutBash(TestCase):
    def test_returned_envelope_records_verdict_and_releases_lock(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        held = MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID)
        assert held.state == MRReviewLock.State.REVIEW_DISPATCHED

        record_result_envelope(task, _verdict_envelope(), phase="reviewing")

        recorded = ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).first()
        assert recorded is not None
        assert recorded.is_merge_safe()
        assert recorded.reviewer_identity == "cold-reviewer-agent"

        held.refresh_from_db()
        assert held.state == MRReviewLock.State.RESOLVED

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_hold_verdict_also_records_and_releases_lock(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        record_result_envelope(task, _verdict_envelope(verdict="hold"), phase="reviewing")

        recorded = ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).first()
        assert recorded is not None
        assert recorded.verdict == ReviewVerdict.Verdict.HOLD
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.RESOLVED

    def test_maker_reviewer_identity_is_refused_and_lock_stays_held(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        record_result_envelope(task, _verdict_envelope(reviewer="coding-agent"), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_reviewing_task_without_verdict_envelope_fails_loudly(self) -> None:
        # #3654: this used to COMPLETE, which is how 138 reviewing tasks finished
        # having recorded nothing while every open PR stayed unmergeable.
        task, _ = _reviewing_task_via_dispatch()
        attempt = record_result_envelope(task, {"summary": "Reviewed.", "decisions": ["looks good"]}, phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "review_verdict" in attempt.error


class TestVerdictBindsToTheDispatchHead(TestCase):
    """The head the writer records at is the head the landed-work guard looks up (#4126).

    The guard (``phase_landing_evidence``) reads the verdict at the DISPATCH head, so a
    verdict written at a reviewer's self-asserted SHA was unreachable: the reviewing row
    stayed ``failed`` and was re-dispatched — the waste #4100 exists to stop.
    """

    def test_a_verdict_at_the_dispatch_head_is_landing_evidence(self) -> None:
        task, _ = _reviewing_task_via_dispatch()

        record_result_envelope(task, _verdict_envelope(), phase="reviewing")

        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert _HEAD[:8] in phase_landing_evidence(task, trust_phase_artifact=True)

    def test_a_self_asserted_head_that_abbreviates_the_dispatch_head_records_at_the_full_head(self) -> None:
        task, _ = _reviewing_task_via_dispatch()

        record_result_envelope(task, _verdict_envelope(reviewed_sha=_HEAD[:12]), phase="reviewing")

        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert _HEAD[:8] in phase_landing_evidence(task, trust_phase_artifact=True)
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_a_divergent_self_asserted_head_is_surfaced_instead_of_silently_recorded(self) -> None:
        task, _ = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _verdict_envelope(reviewed_sha=_OTHER_HEAD), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert _OTHER_HEAD[:8] in attempt.error
        assert _HEAD[:8] in attempt.error


class TestAnUndisclosedHeadIsRefusedLikeADivergentOne(TestCase):
    """#4168: an omitted ``reviewed_sha`` used to be read as agreement with the dispatch head.

    ``merge_safe`` was then recorded at that head with no consistency check performed at
    all — the rule #4158 states ("would vouch for a tree nobody reviewed") enforced only
    against reviewers that disclose a head. The shell path already refuses an empty
    ``--reviewed-sha``; these pin the envelope path to the same answer.
    """

    def test_an_omitted_reviewed_sha_records_nothing(self) -> None:
        task, _ = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _verdict_envelope_without_reviewed_sha(), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "reviewed_sha" in attempt.error

    def test_an_empty_reviewed_sha_records_nothing(self) -> None:
        task, _ = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _verdict_envelope(reviewed_sha="   "), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "reviewed_sha" in attempt.error

    def test_the_refusal_names_the_dispatch_head_the_reviewer_should_have_bound_to(self) -> None:
        task, _ = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _verdict_envelope_without_reviewed_sha(), phase="reviewing")

        assert _HEAD in attempt.error


class TestAnEmittedVerdictIsPersistedOrTheTaskFails(TestCase):
    """#4308: a returned verdict the recorder cannot persist must never complete the task.

    The measured harm: a reviewing task emitted a complete, well-formed envelope, exited 0,
    and no ``ReviewVerdict`` row was written — so a real HOLD existed only inside a
    ``TaskAttempt.result`` no merge guard reads, on a PR that was otherwise CLEAN.
    """

    def test_a_dispatchless_reviewing_task_records_its_verdict_against_its_own_pr(self) -> None:
        task = _reviewing_task_on_reviewer_ticket()

        attempt = record_result_envelope(task, _verdict_envelope(verdict="hold"), phase="reviewing")

        assert attempt.error == ""
        recorded = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD)
        assert recorded.verdict == ReviewVerdict.Verdict.HOLD
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_a_verdict_with_no_resolvable_pr_target_fails_instead_of_completing(self) -> None:
        task = _reviewing_task_on_reviewer_ticket(reviewed_sha="")

        attempt = record_result_envelope(task, _verdict_envelope(), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "no pull request" in attempt.error

    def test_a_verdict_the_read_back_cannot_find_fails_instead_of_completing(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        with patch("teatree.agents.attempt_recorder.ReviewVerdict.record", return_value=None):
            attempt = record_result_envelope(task, _verdict_envelope(), phase="reviewing")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "read-back" in attempt.error
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED


class TestEveryRequiredFieldIsActuallyRefusedWhenOmitted(TestCase):
    """#4168: the schema's ``required`` list and the recorder's refusals are ONE fact.

    They were two, and a field declared required but never enforced reads as a guard while
    guarding nothing — exactly the miss ``reviewed_sha`` was. This walks the declared list
    rather than a hand-copy of it, so adding a name there without teaching the recorder to
    refuse its omission turns red.
    """

    def _required_fields(self) -> list[str]:
        properties = RESULT_JSON_SCHEMA["properties"]
        assert isinstance(properties, dict)
        schema = properties["review_verdict"]
        assert isinstance(schema, dict)
        required = schema["required"]
        assert isinstance(required, list)
        assert required, "an empty required list would pass this vacuously"
        return [str(name) for name in required]

    def test_omitting_any_declared_required_field_records_no_verdict(self) -> None:
        for offset, field in enumerate(self._required_fields()):
            with self.subTest(field=field):
                pr_id = _PR_ID + offset
                task, _ = _reviewing_task_via_dispatch(pr_id=pr_id)
                envelope = _verdict_envelope()
                verdict = envelope["review_verdict"]
                assert isinstance(verdict, dict)
                assert field in verdict, f"the canonical envelope never carried {field!r} — the fixture is stale"
                del verdict[field]

                attempt = record_result_envelope(task, envelope, phase="reviewing")

                assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=pr_id).exists()
                task.refresh_from_db()
                assert task.status == Task.Status.FAILED
                assert field in attempt.error


class TestReviewingEvidenceAcceptsVerdict(TestCase):
    def test_returned_verdict_satisfies_the_reviewing_evidence_gate(self) -> None:
        envelope = _verdict_envelope()
        # No `decisions` field — the verdict alone must clear the gate (corr-11).
        assert "decisions" not in envelope
        assert check_evidence(envelope, "reviewing") == ""
        assert validate_result_keys(envelope) == ""

    def test_reviewing_without_a_verdict_fails_evidence(self) -> None:
        assert "missing required evidence" in check_evidence({"summary": "looked at it"}, "reviewing")

    def test_decisions_alone_no_longer_substitutes_for_the_verdict(self) -> None:
        # #3654: `decisions` was an accepted alternative, so a reviewer that never
        # returned a verdict completed indistinguishably from one that did.
        assert "missing required evidence" in check_evidence({"decisions": ["looks good"]}, "reviewing")

    def test_verdict_outside_the_recorder_vocabulary_fails_evidence(self) -> None:
        envelope = {"review_verdict": {"verdict": "PASS", "reviewer_identity": "cold-reviewer-agent"}}
        assert "missing required evidence" in check_evidence(envelope, "reviewing")


class TestBranchOnlyProbeIsRefused(TestCase):
    """A blocking finding about code the PR does not touch never records (#4251)."""

    def _record_src_finding(self, changed: ChangedFileSet, **verdict_extra: object) -> tuple[Task, str]:
        task, _ = _reviewing_task_via_dispatch()
        verdict: dict[str, object] = {
            "verdict": "hold",
            "reviewed_sha": _HEAD,
            "reviewer_identity": "cold-reviewer-agent",
            "gh_verify_result": "green",
            "findings": [
                {
                    "severity": "high",
                    "summary": "tools_for_phase grants no write/edit, so every dispatch parks",
                    "file": "src/teatree/core/modelkit/phase_tools.py",
                    "line": 41,
                }
            ],
            **verdict_extra,
        }
        envelope: dict[str, object] = {"summary": "Cold review of the pull request.", "review_verdict": verdict}
        with patch(
            "teatree.agents.attempt_recorder.changed_file_set_for_findings",
            return_value=changed,
        ):
            attempt = record_result_envelope(task, envelope, phase="reviewing")
        return task, attempt.error

    def test_a_docs_only_pr_cannot_be_held_on_a_src_finding(self) -> None:
        task, error = self._record_src_finding(ChangedFileSet.known(["skills/review/SKILL.md"]))

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert "t3 review merge-tree" in error
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED

    def test_an_attested_merge_result_retake_records_the_same_finding(self) -> None:
        _, error = self._record_src_finding(ChangedFileSet.known(["skills/review/SKILL.md"]), merge_result_retake=True)

        assert error == ""
        recorded = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID)
        assert recorded.structured_findings[0].file == "src/teatree/core/modelkit/phase_tools.py"

    def test_a_finding_on_a_changed_file_still_records(self) -> None:
        _, error = self._record_src_finding(ChangedFileSet.known(["src/teatree/core/modelkit/phase_tools.py"]))

        assert error == ""
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()

    def test_a_clean_verdict_never_pays_for_the_changed_file_fetch(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        with patch("teatree.core.review.diff_scope_probe.changed_file_set_for") as fetch:
            record_result_envelope(task, _verdict_envelope(), phase="reviewing")

        fetch.assert_not_called()
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()


def _envelope_with(**overrides: object) -> dict[str, object]:
    """A well-formed verdict envelope with *overrides* applied to the verdict itself."""
    verdict: dict[str, object] = {
        "verdict": "merge_safe",
        "reviewed_sha": _HEAD,
        "reviewer_identity": "cold-reviewer-agent",
        "gh_verify_result": "green",
        "findings": [],
        **overrides,
    }
    return {"summary": "Completed an independent cold review of the pull request.", "review_verdict": verdict}


def _contradiction_envelope(**overrides: object) -> dict[str, object]:
    """A ``merge_safe`` verdict over required checks the reviewer itself reports RED.

    The combination §17.8 clause 3 refuses. Whether that refusal is the terminal-eligible
    contradiction is decided by the LIVE read at the reviewed SHA (#4554), which the
    module-scoped ``_live_ci_is_red`` fixture holds at "the forge confirms red" — the case
    every latch test here is about.
    """
    return _envelope_with(gh_verify_result="failed", **overrides)


def _reading(status: str, detail: str) -> "Callable[..., LiveChecksRead]":
    def probe(*, slug: str, head_sha: str) -> LiveChecksRead:
        return LiveChecksRead(status=status, detail=detail)

    return probe


_LIVE_RED = _reading("failed", "failing workflow run(s): test (3.13)")
_LIVE_GREEN = _reading("green", "7 workflow run(s) concluded green")
_LIVE_UNREADABLE = _reading(CHECKS_UNREADABLE, "the workflow-run read failed (rc=1)")


@pytest.fixture(autouse=True)
def _live_ci_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the live read at RED, and off the network, for every test in this module.

    The heads these tests describe genuinely were red, so corroboration is the honest
    default; the cases where the forge says otherwise patch it per test.
    """
    monkeypatch.setattr(attempt_recorder, "live_checks_at", _LIVE_RED)


def _expire_every_claim(*, pr_id: int = _PR_ID) -> None:
    """Push the dispatch claim AND the per-MR lock past their deadlines."""
    past = timezone.now() - dt.timedelta(minutes=1)
    AutoReviewDispatch.objects.filter(slug=_SLUG, pr_id=pr_id).update(deadline=past)
    MRReviewLock.objects.filter(slug=_SLUG, pr_id=pr_id).update(deadline=past)


def _enqueue(*, head_sha: str, pr_id: int = _PR_ID) -> AutoReviewDispatch | None:
    return AutoReviewDispatch.enqueue(
        slug=_SLUG,
        pr_id=pr_id,
        head_sha=head_sha,
        pr_url=f"https://github.com/{_SLUG}/pull/{pr_id}",
        overlay="teatree",
    )


def _exhaust_dispatch(*, head_sha: str = _HEAD, pr_id: int = _PR_ID) -> AutoReviewDispatch:
    """Drive the #68 claim at *head_sha* to its last attempt — the only state the latch acts on."""
    row = _enqueue(head_sha=head_sha, pr_id=pr_id)
    assert row is not None
    while row.attempts < MAX_DISPATCH_ATTEMPTS:
        _expire_every_claim(pr_id=pr_id)
        row = _enqueue(head_sha=head_sha, pr_id=pr_id)
        assert row is not None
    return row


def _exhaust_marker(*, head_sha: str = _HEAD, pr_id: int = _PR_ID) -> CodexReviewMarker:
    """Drive the codex / self-PR claim at *head_sha* to its last attempt.

    Adopts a claim the caller already armed (the production handler takes one when it
    creates the task) rather than claiming afresh, which a live claim would refuse.
    """
    row = CodexReviewMarker.objects.filter(slug=_SLUG, pr_id=pr_id, head_sha=head_sha).first()
    if row is None:
        row = CodexReviewMarker.claim(slug=_SLUG, pr_id=pr_id, head_sha=head_sha, variant="claude:review")
    assert row is not None
    while row.attempts < MAX_DISPATCH_ATTEMPTS:
        _expire_codex_claims(pr_id=pr_id)
        row = CodexReviewMarker.claim(slug=_SLUG, pr_id=pr_id, head_sha=head_sha, variant="claude:review")
        assert row is not None
    return row


def _pending_refusal_questions() -> "list[DeferredQuestion]":
    """Every pending refusal page, matched on the QUESTION TEXT rather than its marker.

    Deliberately not keyed on ``dedupe_marker``: the marker is the thing under test, and a
    query that filtered on it would read "the dedupe was dropped" and "no page was ever
    recorded" as the same empty result.
    """
    return list(
        DeferredQuestion.objects.filter(
            question__startswith="[review-refusal ",
            answered_at__isnull=True,
            dismissed_at__isnull=True,
        )
    )


def _second_reviewing_task_at_the_same_head(dispatch: AutoReviewDispatch) -> Task:
    """Another reviewing task answerable for the SAME PR and head, with no claim of its own.

    Resolves through the reviewer-ticket half of ``review_target_for_task`` (the dispatch
    FK still points at the first task), which is how a real second run at one head reaches
    the recorder once the claim is spent.
    """
    first = dispatch.task
    assert first is not None
    ticket = first.ticket
    ticket.extra = {**(ticket.extra or {}), "reviewed_sha": _HEAD}
    ticket.save(update_fields=["extra"])
    session = Session.objects.create(ticket=ticket, agent_id="second-reviewer")
    return Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.PENDING)


class TestAContradictedChecksVerdictLatchesTheSPENTHead(TestCase):
    """#4522 + #4530: the latch names a spent head's cause; it never shortens the retry.

    ``merge_safe`` + ``gh_verify_result=failed`` is refused (§17.8 clause 3) and nothing is
    recorded, so the head keeps no verdict and the claim is re-acquired once the TTL lapses.
    Latching that on the FIRST refusal was wrong: ``gh_verify_result`` is the reviewer's own
    self-report, so the contradiction is internal to one envelope, and 6 of the 9 heads that
    ever hit it recorded a verdict at the SAME head on a later attempt. The latch fires only
    at ``MAX_DISPATCH_ATTEMPTS``, where the budget is gone anyway.
    """

    def test_a_refusal_with_budget_left_changes_nothing_and_pages_nobody(self) -> None:
        # The recovery path #4530 restored: three of those recoveries were a `hold` over
        # checks that really were red, which is precisely a recordable verdict.
        task, dispatch = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        assert "recording refused" in attempt.error
        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.DISPATCHED
        assert _pending_refusal_questions() == []
        _expire_every_claim()
        assert _enqueue(head_sha=_HEAD) is not None, "a head with retries left must stay re-armable"

    def test_a_refusal_at_the_bound_latches_and_no_later_acquire_takes_the_same_head(self) -> None:
        dispatch = _exhaust_dispatch()
        task = dispatch.task
        assert task is not None
        task.claim(claimed_by="headless-reviewer")

        attempt = record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        assert "recording refused" in attempt.error
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.REFUSED

        _expire_every_claim()
        assert _enqueue(head_sha=_HEAD) is None

    def test_the_latch_is_not_recorded_as_a_verdict_covering_the_tree(self) -> None:
        # REFUSED, never RESOLVED: no verdict covers this head, and a consumer that read
        # the latch as "reviewed" would vouch for a tree nobody could vote on.
        dispatch = _exhaust_dispatch()
        task = dispatch.task
        assert task is not None
        task.claim(claimed_by="headless-reviewer")

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        dispatch.refresh_from_db()
        assert dispatch.state != AutoReviewDispatch.State.RESOLVED
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()

    def test_the_owner_is_paged_once_with_the_cause(self) -> None:
        dispatch = _exhaust_dispatch()
        task = dispatch.task
        assert task is not None
        task.claim(claimed_by="headless-reviewer")

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        questions = _pending_refusal_questions()
        assert len(questions) == 1, [question.dedupe_marker for question in questions]
        assert questions[0].dedupe_marker == f"review-refusal:{_SLUG}#{_PR_ID}@{_HEAD[:12]}"
        assert str(_PR_ID) in questions[0].question

    def test_a_new_head_on_the_same_pr_still_arms_a_fresh_review(self) -> None:
        # The safety valve. The latch binds ONE tree, never the pull request.
        dispatch = _exhaust_dispatch()
        task = dispatch.task
        assert task is not None
        task.claim(claimed_by="headless-reviewer")
        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")
        _expire_every_claim()  # the per-MR lock lapses on its own TTL, never on the refusal

        rearmed = _enqueue(head_sha=_OTHER_HEAD)

        assert rearmed is not None
        assert rearmed.state == AutoReviewDispatch.State.DISPATCHED


class TestTheLatchNeedsALiveConfirmedRed(TestCase):
    """#4554: the terminal is decided against the forge, never against the envelope alone.

    ``gh_verify_result`` is self-asserted, so a refusal decided on it describes the
    reviewer. Only a red the workflow-run read confirms at the reviewed SHA may spend the
    head's terminal; a live green and a live UNREADABLE are distinct outcomes that each
    keep the ordinary retry, because latching an unverified report is the false refusal
    this ticket removes and admitting one would vouch for a tree nobody could vote on.
    """

    def _spent_head(self) -> AutoReviewDispatch:
        dispatch = _exhaust_dispatch()
        task = dispatch.task
        assert task is not None
        task.claim(claimed_by="headless-reviewer")
        return dispatch

    def _assert_refused_but_not_latched(self, dispatch: AutoReviewDispatch, attempt: TaskAttempt) -> None:
        assert "recording refused" in attempt.error
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.DISPATCHED
        assert _pending_refusal_questions() == []

    def test_a_live_confirmed_red_still_latches_the_spent_head(self) -> None:
        dispatch = self._spent_head()

        attempt = record_result_envelope(task_of(dispatch), _contradiction_envelope(), phase="reviewing")

        assert "CONFIRMS red" in attempt.error
        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.REFUSED
        assert len(_pending_refusal_questions()) == 1

    def test_a_live_green_refuses_the_verdict_but_spends_no_terminal(self) -> None:
        dispatch = self._spent_head()

        with patch.object(attempt_recorder, "live_checks_at", _LIVE_GREEN):
            attempt = record_result_envelope(task_of(dispatch), _contradiction_envelope(), phase="reviewing")

        self._assert_refused_but_not_latched(dispatch, attempt)
        assert "concluded green" in attempt.error

    def test_an_unreadable_live_read_spends_no_terminal_either(self) -> None:
        dispatch = self._spent_head()

        with patch.object(attempt_recorder, "live_checks_at", _LIVE_UNREADABLE):
            attempt = record_result_envelope(task_of(dispatch), _contradiction_envelope(), phase="reviewing")

        self._assert_refused_but_not_latched(dispatch, attempt)
        assert "UNVERIFIED" in attempt.error

    def test_the_probe_reads_the_dispatch_head_the_verdict_binds_to(self) -> None:
        dispatch = self._spent_head()
        seen: list[tuple[str, str]] = []

        def probe(*, slug: str, head_sha: str) -> LiveChecksRead:
            seen.append((slug, head_sha))
            return LiveChecksRead(status="failed", detail="failing workflow run(s): test (3.13)")

        with patch.object(attempt_recorder, "live_checks_at", probe):
            record_result_envelope(task_of(dispatch), _contradiction_envelope(), phase="reviewing")

        assert seen == [(_SLUG, _HEAD)]

    def test_a_hold_on_red_checks_never_reaches_the_forge(self) -> None:
        # AC4: the path that recovered 3 of the 9 heads must not gain a network dependency.
        task, dispatch = _reviewing_task_via_dispatch()
        seen: list[str] = []

        def probe(*, slug: str, head_sha: str) -> LiveChecksRead:
            seen.append(head_sha)
            return LiveChecksRead(status="failed", detail="")

        envelope = _contradiction_envelope(
            verdict="hold",
            findings=[{"severity": "high", "summary": "required check `test (3.13)` is red"}],
        )
        with patch.object(attempt_recorder, "live_checks_at", probe):
            attempt = record_result_envelope(task, envelope, phase="reviewing")

        assert attempt.error == ""
        assert seen == []
        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.RESOLVED


def task_of(dispatch: AutoReviewDispatch) -> Task:
    task = dispatch.task
    assert task is not None
    return task


class TestARefusalTouchesOnlyTheClaimThatArmedIt(TestCase):
    """#4530 blocking 2: a refusal is RUN-scoped, so it may not reach the sibling claim.

    Both tables claim per ``(slug, pr_id, head_sha)`` and 328 of 444 dispatch rows share a
    head with a marker row, so "retire every claim on the head" was the common case, not the
    corner. Retiring the sibling consumed a claim whose reviewer had not run and — on the
    #68 ledger — freed the per-MR review lock that claim was holding, which is the #1405
    race the lock exists to prevent. Only a RECORDED verdict, a fact about the tree, retires
    both.
    """

    @staticmethod
    def _marker_armed_task_beside_a_spent_dispatch() -> tuple[Task, AutoReviewDispatch]:
        """A codex-path reviewing task at the same head as a #68 dispatch whose OWN budget is spent.

        Both claims exhausted is the state that makes the reach-across observable. With the
        sibling below its bound the exhaustion guard already refuses to touch it, so a test
        built on a fresh dispatch passes whether or not the refusal is run-scoped — it
        proves the guard, not the scoping. Here the sibling is equally latchable, and the
        ONLY thing keeping this run's refusal off it is that the refusal belongs to the
        marker. Its three reviewers died, which is a different fact from "the last reviewer
        contradicted itself", and the ledger must keep saying so.
        """
        dispatch = _exhaust_dispatch()
        assert dispatch.task is not None
        _exhaust_marker()
        ticket = dispatch.task.ticket
        ticket.extra = {**(ticket.extra or {}), "reviewed_sha": _HEAD, "self_pr_review_variant": "claude:review"}
        ticket.save(update_fields=["extra"])
        session = Session.objects.create(ticket=ticket, agent_id="self-pr-reviewer")
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.PENDING)
        return task, dispatch

    def test_a_codex_path_refusal_leaves_the_dispatch_claim_and_its_lock_alone(self) -> None:
        task, dispatch = self._marker_armed_task_beside_a_spent_dispatch()
        dispatch_task = dispatch.task
        assert dispatch_task is not None

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        marker = CodexReviewMarker.objects.get(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert marker.state == CodexReviewMarker.State.REFUSED, "this run's own claim should latch"

        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.DISPATCHED, (
            "a codex-path refusal relabelled the #68 claim, whose reviewers died for a "
            "different reason than this run's refusal"
        )
        dispatch_task.refresh_from_db()
        assert dispatch_task.status == Task.Status.PENDING
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED, (
            "a codex-path refusal freed the review lock the #68 dispatch was holding"
        )

    def test_a_codex_path_refusal_leaves_the_dispatch_on_the_saturation_ledger(self) -> None:
        # The consequence the operator actually sees: the #68 claim's cause is "three
        # attempts ran out", and a refusal on another path must not overwrite it.
        task, _ = self._marker_armed_task_beside_a_spent_dispatch()
        _expire_every_claim()
        assert AutoReviewDispatch.saturated().count() == 1

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        assert AutoReviewDispatch.saturated().count() == 1, (
            "a codex-path refusal took the #68 claim off the doctor's saturation ledger"
        )

    def test_a_dispatch_path_refusal_leaves_the_marker_claim_alone(self) -> None:
        # The mirror, so the rule is pinned in both directions rather than on one path.
        # The marker is exhausted too, for the same reason as above: an unspent sibling is
        # protected by the bound rather than by the scoping, so it proves the wrong thing.
        dispatch = _exhaust_dispatch()
        task = dispatch.task
        assert task is not None
        task.claim(claimed_by="headless-reviewer")
        marker = _exhaust_marker()

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.REFUSED, "this run's own claim should latch"
        marker.refresh_from_db()
        assert marker.state == CodexReviewMarker.State.DISPATCHED, (
            "a #68-path refusal relabelled the codex claim that belongs to a different run"
        )


class TestOnlyTheChecksContradictionLatches(TestCase):
    """Every other refusal names something the NEXT reviewer could get right (#4522).

    A maker identity, an unknown choice value, a head the reviewer did not bind to — each
    is a defect in one run, not in the tree, so the head keeps its ordinary retry. Latching
    on every ``ReviewVerdictError`` would strand PRs whose only fault was one bad envelope.
    """

    def _assert_did_not_latch(self, dispatch: AutoReviewDispatch) -> None:
        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.DISPATCHED
        assert _pending_refusal_questions() == []
        _expire_every_claim()
        again = _enqueue(head_sha=_HEAD)
        assert again is not None, "an ordinary refusal must leave the head re-armable"

    def test_a_maker_reviewer_identity_does_not_latch(self) -> None:
        task, dispatch = _reviewing_task_via_dispatch()

        record_result_envelope(task, _verdict_envelope(reviewer="coding-agent"), phase="reviewing")

        self._assert_did_not_latch(dispatch)

    def test_a_malformed_envelope_field_does_not_latch(self) -> None:
        # An unknown ``blast_class`` reaches ``ReviewVerdict.record`` and is refused there
        # (the evidence gate does not police that field), so this is a genuine
        # ReviewVerdictError of a non-contradiction class — the discrimination under test.
        task, dispatch = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope_with(blast_class="galactic"), phase="reviewing")

        assert "Unknown blast_class" in attempt.error
        self._assert_did_not_latch(dispatch)

    def test_a_head_mismatch_does_not_latch(self) -> None:
        # Refused UPSTREAM of ``record`` (it never raises), so the latch is unreachable
        # here by construction. Pinned anyway: a later refactor that routed the head check
        # through the recording refusal would otherwise silently latch on a divergent
        # self-assertion, which is a defect in one reviewer, not in the tree.
        task, dispatch = _reviewing_task_via_dispatch()

        record_result_envelope(task, _verdict_envelope(reviewed_sha=_OTHER_HEAD), phase="reviewing")

        self._assert_did_not_latch(dispatch)

    def test_a_hold_on_red_checks_records_normally_and_never_latches(self) -> None:
        # The outcome the brief now asks for: red checks reported as a HOLD are a
        # complete, recordable review, and the claim retires RESOLVED rather than REFUSED.
        task, dispatch = _reviewing_task_via_dispatch()
        envelope = _contradiction_envelope(
            verdict="hold",
            findings=[{"severity": "high", "summary": "required check `test (3.13)` is red"}],
        )

        attempt = record_result_envelope(task, envelope, phase="reviewing")

        assert attempt.error == ""
        recorded = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID)
        assert recorded.verdict == ReviewVerdict.Verdict.HOLD
        dispatch.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.RESOLVED
        assert _pending_refusal_questions() == []


def _reviewing_task_via_codex_marker(*, pr_id: int = _PR_ID, head_sha: str = _HEAD) -> Task:
    """A reviewing task armed by the codex / self-PR claim rather than the #68 ledger.

    Built through the production handler, so what the latch has to close is a real
    :class:`CodexReviewMarker` and not a fixture that resembles one. The recorder reaches
    the same PR and head here through ``review_target_for_task``'s reviewer-ticket half —
    there is no dispatch row to read, which is exactly why a latch written against that
    table alone could not see this run.
    """
    pr_url = f"https://github.com/{_SLUG}/pull/{pr_id}"
    task = handle_self_pr_review(
        DispatchAction(
            kind="agent",
            zone="t3:reviewer",
            detail="self-PR review",
            payload={
                "slug": _SLUG,
                "pr_id": pr_id,
                "head_sha": head_sha,
                "pr_url": pr_url,
                "url": pr_url,
                "variant": "claude:review",
                "overlay": "teatree",
                "self_pr": True,
            },
        )
    )
    assert task is not None
    assert not AutoReviewDispatch.objects.filter(slug=_SLUG, pr_id=pr_id).exists()
    task.claim(claimed_by="headless-reviewer")
    return task


def _codex_task_at_its_last_attempt(*, pr_id: int = _PR_ID, head_sha: str = _HEAD) -> Task:
    """The same codex-path task, with its claim driven to ``MAX_DISPATCH_ATTEMPTS``.

    The latch is deliberately late (#4530), so a recorder test that wants to observe it has
    to spend the budget first — which is itself the point: every attempt before the bound is
    a retry the head is entitled to, and 6 of 9 such heads used one.
    """
    task = _reviewing_task_via_codex_marker(pr_id=pr_id, head_sha=head_sha)
    _exhaust_marker(head_sha=head_sha, pr_id=pr_id)
    return task


def _expire_codex_claims(*, pr_id: int = _PR_ID) -> None:
    """Push every codex / self-PR claim for the PR past its deadline."""
    CodexReviewMarker.objects.filter(slug=_SLUG, pr_id=pr_id).update(deadline=timezone.now() - dt.timedelta(minutes=1))


class TestTheCodexSelfPrPathLatchesTheSameWay(TestCase):
    """#4530: the twin claim carried most of the measured refusals, so it latches too.

    Both tables claim per ``(slug, pr_id, head_sha)`` and both re-arm an un-verdicted head
    once the deadline lapses; 11 of the 18 measured refusal runs were armed by THIS one, so
    #4522's ledger-only latch was a no-op on the majority. It latches on the same terms as
    its twin — at the bound, on its own row, freeing no lock.
    """

    def test_a_refusal_with_budget_left_changes_nothing_on_this_path_either(self) -> None:
        # The recovery path, pinned on the path that carried most of the refusals.
        task = _reviewing_task_via_codex_marker()

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        marker = CodexReviewMarker.objects.get(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert marker.state == CodexReviewMarker.State.DISPATCHED
        assert _pending_refusal_questions() == []
        _expire_codex_claims()
        assert CodexReviewMarker.claim(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD) is not None

    def test_a_refusal_at_the_bound_latches_the_marker_and_no_later_claim_takes_the_head(self) -> None:
        task = _codex_task_at_its_last_attempt()

        attempt = record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        assert "recording refused" in attempt.error
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()

        # Asserted FIRST so a regression reads as "the head re-armed", not a state mismatch.
        _expire_codex_claims()
        assert CodexReviewMarker.claim(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD) is None, (
            "the codex / self-PR claim re-armed a head whose budget was already spent"
        )
        marker = CodexReviewMarker.objects.get(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert marker.state == CodexReviewMarker.State.REFUSED

    def test_a_live_claim_at_another_head_survives_the_refusal(self) -> None:
        """The safety valve, and the reason the latch may never key on the PR.

        This path takes no per-MR lock, so a push landing mid-review claims the new head
        while the old review is still running — two live claims, one pull request. The new
        tree has been told nothing about itself, and latching it would suppress the very
        review the push earned.
        """
        task = _codex_task_at_its_last_attempt()
        pushed = CodexReviewMarker.claim(slug=_SLUG, pr_id=_PR_ID, head_sha=_OTHER_HEAD, variant="claude:review")
        assert pushed is not None

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        pushed.refresh_from_db()
        assert pushed.state == CodexReviewMarker.State.DISPATCHED, (
            "a refusal at one head latched a DIFFERENT head's live claim on the same PR"
        )

    def test_a_new_head_after_a_refusal_arms_normally_on_this_path(self) -> None:
        # The other half of the valve: a push AFTER the latch mints a row of its own.
        task = _codex_task_at_its_last_attempt()
        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        rearmed = CodexReviewMarker.claim(slug=_SLUG, pr_id=_PR_ID, head_sha=_OTHER_HEAD, variant="claude:review")

        assert rearmed is not None
        assert rearmed.state == CodexReviewMarker.State.DISPATCHED
        assert CodexReviewMarker.objects.filter(slug=_SLUG, pr_id=_PR_ID).count() == 2

    def test_the_latch_is_not_recorded_as_a_verdict_covering_the_tree(self) -> None:
        # REFUSED, never RESOLVED: no verdict covers this head, and a consumer that read
        # the latch as "reviewed" would vouch for a tree nobody could vote on.
        task = _codex_task_at_its_last_attempt()

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        marker = CodexReviewMarker.objects.get(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert marker.state != CodexReviewMarker.State.RESOLVED

    def test_the_owner_is_paged_once_on_this_path_too(self) -> None:
        task = _codex_task_at_its_last_attempt()

        record_result_envelope(task, _contradiction_envelope(), phase="reviewing")

        questions = _pending_refusal_questions()
        assert len(questions) == 1, [question.dedupe_marker for question in questions]
        assert questions[0].dedupe_marker == f"review-refusal:{_SLUG}#{_PR_ID}@{_HEAD[:12]}"

    def test_a_hold_on_red_checks_resolves_the_marker_rather_than_latching_it(self) -> None:
        # The recordable shape the brief asks for, and the outcome three of the six real
        # recoveries actually took: red checks reported as a HOLD are a complete review.
        task = _codex_task_at_its_last_attempt()
        envelope = _contradiction_envelope(
            verdict="hold",
            findings=[{"severity": "high", "summary": "required check `test (3.13)` is red"}],
        )

        attempt = record_result_envelope(task, envelope, phase="reviewing")

        assert attempt.error == ""
        marker = CodexReviewMarker.objects.get(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert marker.state == CodexReviewMarker.State.RESOLVED
        assert _pending_refusal_questions() == []

    def test_an_ordinary_refusal_leaves_this_path_re_armable(self) -> None:
        # Same discrimination as on the #68 ledger: a maker identity is a defect in ONE
        # run, so the head keeps its ordinary retry rather than being latched shut.
        task = _reviewing_task_via_codex_marker()

        record_result_envelope(task, _verdict_envelope(reviewer="coding-agent"), phase="reviewing")

        assert _pending_refusal_questions() == []
        _expire_codex_claims()
        assert CodexReviewMarker.claim(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD) is not None
