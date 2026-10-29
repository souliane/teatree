"""``t3 <overlay> ticket merge`` hands ``--no-squash`` to the keystone, and squashes by default."""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.merge_quality_gate import check_merge_quality_verdict
from teatree.core.models import CriticDispatch, MergeClear, Task, Ticket

_MERGE_TICKET_PR = "teatree.core.management.commands._merge_keystone_commands.merge_ticket_pr"


def _clear() -> MergeClear:
    return MergeClear.objects.create(
        pr_id=7,
        slug="souliane/teatree",
        reviewed_sha="a" * 40,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


class TestNoSquashFlag(TestCase):
    def test_no_squash_reaches_the_keystone(self) -> None:
        clear = _clear()
        with patch(_MERGE_TICKET_PR) as merge_pr:
            cast("dict", call_command("ticket", "merge", str(clear.pk), no_squash=True))

        assert merge_pr.call_args.kwargs["squash"] is False

    def test_the_keystone_squashes_by_default(self) -> None:
        clear = _clear()
        with patch(_MERGE_TICKET_PR) as merge_pr:
            call_command("ticket", "merge", str(clear.pk))

        assert merge_pr.call_args.kwargs["squash"] is True


class TestCriticWaitRouting(TestCase):
    def test_live_critic_wait_is_not_escalated_but_failed_critic_is(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear()
        claim = CriticDispatch.enqueue(ticket=ticket, transition="merge", head_sha=clear.reviewed_sha, contract="c")
        assert claim is not None
        assert claim.task is not None

        def gated_merge(**_: object) -> None:
            check_merge_quality_verdict(ticket, clear.reviewed_sha)

        with (
            patch(_MERGE_TICKET_PR, side_effect=gated_merge),
            patch("teatree.core.management.commands._merge_keystone_commands.merge_clear_refusal", return_value=None),
            patch("teatree.config.resolution.worker_is_quiescing", return_value=False),
            patch("teatree.core.gates.merge_quality_gate.flock_is_held", return_value=True),
        ):
            waiting = cast("dict", call_command("ticket", "merge", str(clear.pk)))
            claim.task.status = Task.Status.FAILED
            claim.task.save(update_fields=["status"])
            refused = cast("dict", call_command("ticket", "merge", str(clear.pk)))

        assert waiting["merged"] is False
        assert waiting["escalation_kind"] == "awaiting_critic"
        assert waiting.get("escalated") is None
        assert refused["merged"] is False
        assert refused["escalated"] is True
        assert refused["escalation_kind"] == ""
