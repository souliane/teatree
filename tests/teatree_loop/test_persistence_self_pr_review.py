"""Direct unit tests for ``handle_self_pr_review`` (#3569).

The self-PR review persistence handler, split out of ``persistence`` to stay under
the module-health LOC cap. Exercised end-to-end through ``persist_agent_actions``
in ``test_persistence_zone_handlers`` too; here it is called directly so the symbol
is referenced by name and its own edge cases are pinned at the unit level.
"""

from django.test import TestCase

from teatree.core.models import Task, Ticket
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence_self_pr_review import handle_self_pr_review

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
        first.complete()
        assert handle_self_pr_review(_action(pr_id=91, head_sha="selfsha-91")) is None
        assert Task.objects.filter(ticket__issue_url=_PR_URL, phase="reviewing").count() == 1

    def test_role_conflict_does_not_claim_marker(self) -> None:
        Ticket.objects.create(issue_url=_PR_URL, overlay="acme", role=Ticket.Role.AUTHOR)
        assert handle_self_pr_review(_action(pr_id=92, head_sha="selfsha-92")) is None
        assert not CodexReviewMarker.objects.filter(slug="o/r", pr_id=92).exists()
