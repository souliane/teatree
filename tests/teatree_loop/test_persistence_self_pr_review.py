"""Direct unit tests for ``handle_self_pr_review`` (#3569).

The self-PR review persistence handler, split out of ``persistence`` to stay under
the module-health LOC cap. Exercised end-to-end through ``persist_agent_actions``
in ``test_persistence_zone_handlers`` too; here it is called directly so the symbol
is referenced by name and its own edge cases are pinned at the unit level.
"""

from django.test import TestCase

from teatree.core.modelkit.review_state import ReviewState
from teatree.core.models import AutoReviewDispatch, HeadCarryOver, ReviewVerdict, Task, Ticket
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence_self_pr_review import handle_self_pr_review
from tests.teatree_core.conftest import record_review_context_for_test

_PR_URL = "https://github.com/o/r/pull/90"


def _action(*, pr_id: int = 90, head_sha: str = "selfsha-90", variant: str = "claude:review") -> DispatchAction:
    return DispatchAction(
        kind="agent",
        zone="t3:reviewer",
        detail="self-PR review",
        payload={
            "slug": "o/r",
            "pr_id": pr_id,
            "head_sha": head_sha,
            "pr_url": _PR_URL,
            "url": _PR_URL,
            "variant": variant,
            "overlay": "acme",
            "self_pr": True,
        },
    )


class TestHandleSelfPrReview(TestCase):
    def test_self_pr_then_auto_dispatch_reuses_one_review_task(self) -> None:
        task = handle_self_pr_review(_action(head_sha="a" * 40))
        assert task is not None

        row = AutoReviewDispatch.enqueue(slug="o/r", pr_id=90, head_sha="a" * 40, pr_url=_PR_URL, overlay="acme")

        assert row is not None
        assert row.task_id == task.pk
        assert Task.objects.filter(ticket=task.ticket, phase="reviewing").count() == 1

    def test_auto_dispatch_then_self_pr_keeps_one_review_task(self) -> None:
        row = AutoReviewDispatch.enqueue(slug="o/r", pr_id=90, head_sha="a" * 40, pr_url=_PR_URL, overlay="acme")
        assert row is not None

        assert handle_self_pr_review(_action(head_sha="a" * 40)) is None
        assert Task.objects.filter(ticket=row.task.ticket, phase="reviewing").count() == 1

    def test_creates_reviewer_reviewing_task_and_claims_marker(self) -> None:
        task = handle_self_pr_review(_action())
        assert task is not None
        assert task.phase == "reviewing"
        assert task.ticket.role == Ticket.Role.REVIEWER
        assert task.ticket.extra["self_pr_review_variant"] == "claude:review"
        assert CodexReviewMarker.objects.filter(slug="o/r", pr_id=90, head_sha="selfsha-90").count() == 1

    def test_incomplete_payload_is_a_noop(self) -> None:
        assert handle_self_pr_review(_action(head_sha="")) is None
        assert not CodexReviewMarker.objects.filter(slug="o/r", pr_id=90).exists()

    def test_second_call_same_sha_is_deduped(self) -> None:
        first = handle_self_pr_review(_action(pr_id=91, head_sha="selfsha-91"))
        assert first is not None
        record_review_context_for_test(first.ticket)
        first.complete()
        assert handle_self_pr_review(_action(pr_id=91, head_sha="selfsha-91")) is None
        assert Task.objects.filter(ticket__issue_url=_PR_URL, phase="reviewing").count() == 1

    def test_role_conflict_does_not_claim_marker(self) -> None:
        Ticket.objects.create(issue_url=_PR_URL, overlay="acme", role=Ticket.Role.AUTHOR)
        assert handle_self_pr_review(_action(pr_id=92, head_sha="selfsha-92")) is None
        assert not CodexReviewMarker.objects.filter(slug="o/r", pr_id=92).exists()

    def test_reused_ticket_restamps_reviewed_sha_to_the_new_arming_head(self) -> None:
        first = handle_self_pr_review(_action(pr_id=93, head_sha="selfsha-93-a"))
        assert first is not None
        assert first.ticket.extra["reviewed_sha"] == "selfsha-93-a"
        record_review_context_for_test(first.ticket)
        first.complete()
        second = handle_self_pr_review(_action(pr_id=93, head_sha="selfsha-93-b"))
        assert second is not None
        assert second.ticket.pk == first.ticket.pk
        second.ticket.refresh_from_db()
        assert second.ticket.extra["reviewed_sha"] == "selfsha-93-b"

    def test_an_unclaimed_new_head_leaves_the_reviewed_head_and_its_approval(self) -> None:
        extra = {
            "reviewed_sha": "selfsha-94-a",
            "discharged_sha": "selfsha-94-a",
            "last_review_state": ReviewState.APPROVED.value,
        }
        ticket = Ticket.objects.create(issue_url=_PR_URL, overlay="acme", role=Ticket.Role.REVIEWER, extra=dict(extra))
        assert CodexReviewMarker.claim(slug="o/r", pr_id=94, head_sha="selfsha-94-b", variant="claude:review")

        assert handle_self_pr_review(_action(pr_id=94, head_sha="selfsha-94-b")) is None

        ticket.refresh_from_db()
        assert ticket.extra == extra


class TestACarriedHeadArmsNoReview(TestCase):
    """A head whose records followed an unchanged patch (#5450) was reviewed already; a changed one is reviewed."""

    _OLD = "a" * 40
    _NEW = "b" * 40

    def _carry_row(self, outcome: HeadCarryOver.Outcome, *, review_verdicts: int = 1) -> None:
        HeadCarryOver.objects.create(
            slug="o/r",
            pr_id=95,
            from_head=self._OLD,
            to_head=self._NEW,
            outcome=outcome,
            carried={"review_verdicts": review_verdicts},
        )

    def test_a_carried_head_creates_no_marker_and_no_task(self) -> None:
        assert CodexReviewMarker.claim(slug="o/r", pr_id=95, head_sha=self._OLD, variant="claude:review")
        CodexReviewMarker.mark_resolved(slug="o/r", pr_id=95, head_sha=self._OLD)
        self._carry_row(HeadCarryOver.Outcome.CARRIED)

        assert handle_self_pr_review(_action(pr_id=95, head_sha=self._NEW)) is None

        assert not CodexReviewMarker.objects.filter(slug="o/r", pr_id=95, head_sha=self._NEW).exists()
        assert not Task.objects.filter(ticket__issue_url=_PR_URL.replace("90", "95")).exists()

    def test_a_carried_head_arms_a_variant_not_completed_at_the_old_head(self) -> None:
        assert CodexReviewMarker.claim(slug="o/r", pr_id=95, head_sha=self._OLD, variant="codex:review")
        CodexReviewMarker.mark_resolved(slug="o/r", pr_id=95, head_sha=self._OLD)
        self._carry_row(HeadCarryOver.Outcome.CARRIED)

        task = handle_self_pr_review(_action(pr_id=95, head_sha=self._NEW))

        assert task is not None
        assert task.phase == "reviewing"

    def test_a_head_the_patch_left_unchanged_that_no_verdict_followed_is_armed(self) -> None:
        self._carry_row(HeadCarryOver.Outcome.CARRIED, review_verdicts=0)

        task = handle_self_pr_review(_action(pr_id=95, head_sha=self._NEW))

        assert task is not None
        assert CodexReviewMarker.objects.filter(slug="o/r", pr_id=95, head_sha=self._NEW).count() == 1

    def test_a_head_whose_patch_changed_is_armed(self) -> None:
        self._carry_row(HeadCarryOver.Outcome.REFUSED)

        task = handle_self_pr_review(_action(pr_id=95, head_sha=self._NEW))

        assert task is not None
        assert CodexReviewMarker.objects.filter(slug="o/r", pr_id=95, head_sha=self._NEW).count() == 1

    def test_a_head_the_forge_could_not_vouch_for_is_armed(self) -> None:
        ReviewVerdict.record(
            pr_id=96, slug="o/r", reviewed_sha=self._OLD, verdict="merge_safe", reviewer_identity="cold-reviewer"
        )

        task = handle_self_pr_review(_action(pr_id=96, head_sha=self._NEW))

        assert task is not None
        assert not HeadCarryOver.objects.filter(pr_id=96).exists()
