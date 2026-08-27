"""Did a phase's work LAND? — the evidence a lost lease may not overrule (#3982).

The observed defect: a shipping task pushed its branch, opened its PR and advanced its
ticket to ``in_review``, yet was recorded ``failed`` / ``lease_lost``. ``in_review`` sits
OFF ``Ticket._WORK_STATE_ORDER``, so ``has_completed_phase`` answers False for a ticket
that has demonstrably shipped — these pin the fuller author-ladder answer plus the
shipping artifact (an attached pull request) the issue names as evidence.

A reviewer-role ticket holds no author-ladder position at all, so its landing signal is its
own artifact: a ``ReviewVerdict`` recorded at the head the task reviewed (#4100).

Every call below passes ``trust_phase_artifact=True`` to exercise the FULL predicate —
the narrower ``False`` scope (a stray PR, or another reviewer's verdict, must not excuse a
genuinely deterministic failure) is pinned directly here too, and at the sweep integration
level in ``tests/teatree_loop/test_transient_requeue.py``.
"""

from django.test import TestCase

from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.auto_review_dispatch import AutoReviewDispatch
from teatree.core.models.phase_landing import phase_landing_evidence
from teatree.core.models.review_verdict import ReviewVerdict

_HEAD = "a1b2c3d4" * 5
_OTHER_HEAD = "9f8e7d6c" * 5


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
        assert "in_review" in phase_landing_evidence(task, trust_phase_artifact=True)

    def test_every_state_past_shipped_is_evidence(self) -> None:
        for state in (Ticket.State.SHIPPED, Ticket.State.MERGED, Ticket.State.RETROSPECTED, Ticket.State.DELIVERED):
            assert phase_landing_evidence(_task(phase="shipping", state=state), trust_phase_artifact=True), state

    def test_a_state_behind_the_phase_target_is_not_evidence(self) -> None:
        assert (
            phase_landing_evidence(_task(phase="shipping", state=Ticket.State.REVIEWED), trust_phase_artifact=True)
            == ""
        )
        assert (
            phase_landing_evidence(_task(phase="coding", state=Ticket.State.PLANNED), trust_phase_artifact=True) == ""
        )

    def test_the_short_verb_phase_spelling_normalizes(self) -> None:
        assert phase_landing_evidence(_task(phase="ship", state=Ticket.State.IN_REVIEW), trust_phase_artifact=True)

    def test_an_off_ladder_state_yields_no_evidence(self) -> None:
        # REVIEW_POSTED / IGNORED carry no position on the author ladder, so they can
        # prove nothing about an author phase — the conservative answer is "no evidence".
        assert (
            phase_landing_evidence(_task(phase="shipping", state=Ticket.State.REVIEW_POSTED), trust_phase_artifact=True)
            == ""
        )
        assert (
            phase_landing_evidence(_task(phase="shipping", state=Ticket.State.IGNORED), trust_phase_artifact=True) == ""
        )

    def test_a_free_form_phase_has_no_ladder_target(self) -> None:
        assert (
            phase_landing_evidence(_task(phase="bughunt", state=Ticket.State.DELIVERED), trust_phase_artifact=True)
            == ""
        )

    def test_a_reviewer_ticket_yields_no_author_phase_evidence(self) -> None:
        task = _task(phase="shipping", state=Ticket.State.IN_REVIEW, role=Ticket.Role.REVIEWER)
        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_an_unrecognised_role_yields_no_evidence(self) -> None:
        # A role neither ladder speaks for: the conservative answer leaves the caller's
        # existing failure path untouched rather than reading it as an author.
        task = _task(phase="shipping", state=Ticket.State.IN_REVIEW, role="observer")
        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""


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

        assert pr.url in phase_landing_evidence(task, trust_phase_artifact=True)

    def test_a_closed_pull_request_is_not_evidence(self) -> None:
        task = _task(phase="shipping", state=Ticket.State.REVIEWED)
        self._pr(task.ticket, state=PullRequest.State.CLOSED)

        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_the_artifact_branch_is_shipping_only(self) -> None:
        task = _task(phase="coding", state=Ticket.State.PLANNED)
        self._pr(task.ticket, state=PullRequest.State.OPEN)

        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_untrusted_artifact_is_not_evidence_even_when_open(self) -> None:
        # A stray PR can be opened independently of ship() (the no-orphan pre-push gate,
        # the PendingPullRequest drain). When the caller has NOT established this row's own
        # failure was a lease loss, the artifact must not count as evidence — see the
        # sweep-level pin in tests/teatree_loop/test_transient_requeue.py.
        task = _task(phase="shipping", state=Ticket.State.REVIEWED)
        self._pr(task.ticket, state=PullRequest.State.OPEN)

        assert phase_landing_evidence(task, trust_phase_artifact=False) == ""


class TestReviewVerdictEvidence(TestCase):
    """A reviewer ticket's landing signal is its recorded verdict at the head it reviewed (#4100).

    Reviewing holds most of the recorded lease-loss failures, and the author ladder can say
    nothing about a reviewer-role ticket — so without this branch the landed-work guard
    structurally cannot cover the phase that needs it most.
    """

    def _reviewing_task(self, *, phase: str = "reviewing", reviewed_sha: str = _HEAD, pr_id: int = 7) -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url=f"https://github.com/o/r/pull/{pr_id}",
            extra={"reviewed_sha": reviewed_sha} if reviewed_sha else {},
        )
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def _verdict(self, *, reviewed_sha: str, pr_id: int = 7, slug: str = "o/r") -> ReviewVerdict:
        # No ``ticket=``: no path guarantees the FK — the shell `review record` defaults it
        # away (the envelope path does set it), so a test that stamps it would certify a
        # lookup that misses every shell-recorded verdict.
        return ReviewVerdict.record(
            pr_id=pr_id,
            slug=slug,
            reviewed_sha=reviewed_sha,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
        )

    def test_a_verdict_at_the_reviewed_head_is_evidence_the_review_landed(self) -> None:
        task = self._reviewing_task()
        self._verdict(reviewed_sha=_HEAD)

        assert _HEAD[:8] in phase_landing_evidence(task, trust_phase_artifact=True)

    def test_a_verdict_recorded_under_another_slug_casing_is_the_same_repo(self) -> None:
        # Forge slugs are case-insensitive: reading `Owner/Repo` and `owner/repo` as two repos
        # loses the evidence and records a landed review as a lease-loss failure.
        task = self._reviewing_task()
        self._verdict(reviewed_sha=_HEAD, slug="O/R")

        assert _HEAD[:8] in phase_landing_evidence(task, trust_phase_artifact=True)

    def test_a_verdict_at_another_head_is_not_evidence(self) -> None:
        task = self._reviewing_task()
        self._verdict(reviewed_sha=_OTHER_HEAD)

        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_the_dispatch_contract_supplies_the_head_and_the_pr(self) -> None:
        # The #68 auto-review path binds the task to (slug, pr_id, head_sha), which is the
        # per-task head the ticket's own reviewed_sha cannot promise once a push moves it.
        task = self._reviewing_task(reviewed_sha="")
        AutoReviewDispatch.objects.create(slug="o/r", pr_id=7, head_sha=_HEAD, task=task)
        self._verdict(reviewed_sha=_HEAD)

        assert _HEAD[:8] in phase_landing_evidence(task, trust_phase_artifact=True)

    def test_a_verdict_on_another_pr_at_the_same_head_is_not_evidence(self) -> None:
        task = self._reviewing_task(pr_id=7)
        self._verdict(reviewed_sha=_HEAD, pr_id=8)

        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_a_reviewing_task_with_no_verdict_is_not_evidence(self) -> None:
        assert phase_landing_evidence(self._reviewing_task(), trust_phase_artifact=True) == ""

    def test_an_unresolvable_reviewed_head_is_not_evidence(self) -> None:
        task = self._reviewing_task(reviewed_sha="")
        self._verdict(reviewed_sha=_HEAD)

        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_a_ticket_whose_url_names_no_pr_is_not_evidence(self) -> None:
        task = self._reviewing_task()
        Ticket.objects.filter(pk=task.ticket_id).update(issue_url="https://example.com/not-a-pr")
        task.refresh_from_db()
        self._verdict(reviewed_sha=_HEAD)

        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_the_codex_review_variants_carry_the_same_signal(self) -> None:
        for pr_id, phase in enumerate(("codex_reviewing", "codex_adversarial_reviewing"), start=20):
            task = self._reviewing_task(phase=phase, pr_id=pr_id)
            self._verdict(reviewed_sha=_HEAD, pr_id=pr_id)

            assert phase_landing_evidence(task, trust_phase_artifact=True), phase

    def test_a_non_review_phase_on_a_reviewer_ticket_is_not_evidence(self) -> None:
        task = self._reviewing_task(phase="bughunt")
        self._verdict(reviewed_sha=_HEAD)

        assert phase_landing_evidence(task, trust_phase_artifact=True) == ""

    def test_an_untrusted_caller_never_reads_the_verdict(self) -> None:
        # Same scope as the shipping artifact: only a caller that attributed the failure to
        # the LEASE may let an artifact excuse it — a deterministic reviewing failure stays one.
        task = self._reviewing_task()
        self._verdict(reviewed_sha=_HEAD)

        assert phase_landing_evidence(task, trust_phase_artifact=False) == ""
