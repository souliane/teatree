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

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope, validate_result_keys
from teatree.agents.result_schema import RESULT_JSON_SCHEMA, check_evidence
from teatree.core.modelkit.diff_scope import ChangedFileSet
from teatree.core.models import AutoReviewDispatch, MRReviewLock, ReviewVerdict, Session, Task, Ticket
from teatree.core.models.phase_landing import phase_landing_evidence

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
