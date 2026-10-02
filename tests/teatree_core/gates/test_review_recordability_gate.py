"""The predicate that decides a review is unrecordable BEFORE it is paid for.

53 refusals across 53 freshly-minted tasks recorded the same precondition AFTER the full
Opus review had run. The precondition is two pure DB reads, so it is knowable at dispatch;
these pin that the gate answers from the recorder's OWN resolver, refuses only the review
that is genuinely answerable-for-a-PR-with-no-head, and stays silent everywhere else.
"""

from django.test import TestCase

from teatree.core.gates.review_recordability_gate import unrecordable_review_mint_refusal, unrecordable_review_refusal
from teatree.core.modelkit.task_failure_taxonomy import REVIEW_UNRECORDABLE_PREFIX, FailureKind, classify_failure
from teatree.core.models import AutoReviewDispatch, Session, Task, Ticket

_SLUG = "souliane/teatree"
_PR_ID = 225
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_ISSUE_URL = f"https://github.com/{_SLUG}/issues/{_PR_ID}"
_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"


def _reviewer_ticket(*, issue_url: str = _PR_URL, extra: dict[str, object] | None = None) -> Ticket:
    return Ticket.objects.create(
        issue_url=issue_url,
        overlay="teatree",
        role=Ticket.Role.REVIEWER,
        extra=extra or {},
    )


def _task_for(ticket: Ticket, *, phase: str = "reviewing") -> Task:
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.PENDING)


class TestARecordedHeadDispatchesUnchanged(TestCase):
    def test_a_reviewer_ticket_carrying_its_head_is_not_refused(self) -> None:
        task = _task_for(_reviewer_ticket(extra={"reviewed_sha": _HEAD}))

        assert unrecordable_review_refusal(task, phase="reviewing") is None

    def test_an_armed_dispatch_row_carries_the_head_even_with_empty_ticket_extra(self) -> None:
        dispatch = AutoReviewDispatch.enqueue(
            slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD, pr_url=_PR_URL, overlay="teatree"
        )
        assert dispatch is not None
        task = dispatch.task
        assert task is not None
        task.ticket.extra = {}
        task.ticket.save(update_fields=["extra"])

        assert unrecordable_review_refusal(task, phase="reviewing") is None

    def test_the_mint_seam_admits_a_ticket_that_carries_its_head(self) -> None:
        assert unrecordable_review_mint_refusal(_reviewer_ticket(extra={"reviewed_sha": _HEAD})) is None


class TestAMissingHeadIsRefused(TestCase):
    def setUp(self) -> None:
        self.ticket = _reviewer_ticket()

    def test_a_reviewing_task_with_no_recorded_head_is_refused(self) -> None:
        refusal = unrecordable_review_refusal(_task_for(self.ticket), phase="reviewing")

        assert refusal is not None
        assert refusal.startswith(REVIEW_UNRECORDABLE_PREFIX)

    def test_the_refusal_names_the_pull_request_the_missing_head_and_the_remedy(self) -> None:
        refusal = unrecordable_review_refusal(_task_for(self.ticket), phase="reviewing")

        assert refusal is not None
        assert f"{_SLUG}#{_PR_ID}" in refusal
        assert "no pull request head is recorded" in refusal
        assert "close the reviewer ticket" in refusal

    def test_the_refusal_classifies_as_its_own_named_failure_kind(self) -> None:
        refusal = unrecordable_review_refusal(_task_for(self.ticket), phase="reviewing")

        assert refusal is not None
        assert classify_failure(refusal) == FailureKind.REVIEW_UNRECORDABLE

    def test_a_whitespace_only_head_is_no_head(self) -> None:
        self.ticket.extra = {"reviewed_sha": "   "}
        self.ticket.save(update_fields=["extra"])

        assert unrecordable_review_refusal(_task_for(self.ticket), phase="reviewing") is not None

    def test_the_mint_seam_refuses_the_same_ticket_with_the_same_statement(self) -> None:
        mint_refusal = unrecordable_review_mint_refusal(self.ticket)
        dispatch_refusal = unrecordable_review_refusal(_task_for(self.ticket), phase="reviewing")

        assert mint_refusal == dispatch_refusal


class TestThePhaseScopeIsNotOverWide(TestCase):
    """The ``codex_*`` and ``e2e_reviewing`` lanes record through the shell, never ``ticket.extra``."""

    def setUp(self) -> None:
        self.ticket = _reviewer_ticket()

    def test_a_codex_reviewing_task_on_the_same_headless_ticket_is_untouched(self) -> None:
        task = _task_for(self.ticket, phase="codex_reviewing")

        assert unrecordable_review_refusal(task, phase="codex_reviewing") is None

    def test_an_e2e_reviewing_task_on_the_same_headless_ticket_is_untouched(self) -> None:
        task = _task_for(self.ticket, phase="e2e_reviewing")

        assert unrecordable_review_refusal(task, phase="e2e_reviewing") is None

    def test_a_coding_task_is_untouched(self) -> None:
        assert unrecordable_review_refusal(_task_for(self.ticket, phase="coding"), phase="coding") is None


class TestATaskAnswerableForNoPullRequestKeepsItsCompletion(TestCase):
    def test_an_author_reviewing_task_keyed_by_an_issue_url_is_not_refused(self) -> None:
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, overlay="teatree", role=Ticket.Role.AUTHOR)

        assert unrecordable_review_refusal(_task_for(ticket), phase="reviewing") is None

    def test_the_mint_seam_likewise_admits_a_ticket_that_is_no_pull_request(self) -> None:
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, overlay="teatree", role=Ticket.Role.AUTHOR)

        assert unrecordable_review_mint_refusal(ticket) is None
