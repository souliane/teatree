"""The sweep's ownership floor: the owner signal, and the merge gates it protects.

An own PR no ticket owns is refused by the sweep; the same PR with its ledger row recorded
(by the reconciler's head-branch adoption) reaches the real gates, which then bind.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates.merge_quality_gate import MergeQualityVerdictError, assert_merge_quality_verdict
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.ticket_gates import assert_ticket_scoped_gates
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import CriticDispatch, PullRequest, Session, Task, Ticket, Worktree
from teatree.core.models.review_target import review_target_for_task
from teatree.loop.manual_pr_reconcile import reconcile_manual_prs
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.pr_sweep_adapters import OWNER_ESCALATION_FLAG_REASONS, SlackMergeNotifier
from teatree.loop.scanners.pr_sweep_types import NO_OWNING_TICKET_REASON

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 5170
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_BRANCH = "5145-merge-gate-b-sweep-refuses-unresolved-ticket"
_HEAD = "c" * 40


class TestTheOwnerLearnsOnce(TestCase):
    def test_no_owning_ticket_is_an_owner_audience_flag(self) -> None:
        assert NO_OWNING_TICKET_REASON in OWNER_ESCALATION_FLAG_REASONS

    def test_flag_text_names_the_two_remedies(self) -> None:
        with patch("teatree.core.notify.notify_user") as notify:
            SlackMergeNotifier(backend=None).flag(slug=_SLUG, pr_id=_PR_ID, reason=NO_OWNING_TICKET_REASON, url=_PR_URL)

        text = notify.call_args.args[0]
        assert "worktree adopt" in text
        assert "ticket clear" in text
        assert notify.call_args.kwargs["audience"] is NotifyAudience.OWNER_ESCALATION

    def test_repeated_flags_for_one_pr_reuse_one_idempotency_key(self) -> None:
        with patch("teatree.core.notify.notify_user") as notify:
            for _ in range(2):
                SlackMergeNotifier(backend=None).flag(
                    slug=_SLUG, pr_id=_PR_ID, reason=NO_OWNING_TICKET_REASON, url=_PR_URL
                )

        keys = {call.kwargs["idempotency_key"] for call in notify.call_args_list}
        assert keys == {f"pr-sweep-flag:{_SLUG}#{_PR_ID}:{NO_OWNING_TICKET_REASON}"}


def _refs_only_signal() -> ScanSignal:
    return ScanSignal(
        kind="my_pr.open",
        summary=f"PR #{_PR_ID} open",
        payload={
            "url": _PR_URL,
            "iid": _PR_ID,
            "title": "x",
            "status": "success",
            "raw": {"description": "Refs #5145", "source_branch": _BRANCH},
        },
    )


class TestGatesBindAfterAdoption(TestCase):
    """Each gate below returned ``None`` or passed silently while the PR had no ledger row."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=f"https://github.com/{_SLUG}/issues/5145",
            state=Ticket.State.WORK_STARTED,
        )
        Worktree.objects.create(ticket=self.ticket, overlay="t3-teatree", repo_path=_SLUG, branch=_BRANCH)
        session = Session.objects.create(ticket=self.ticket, agent_id="reviewer")
        self.reviewing = Task.objects.create(ticket=self.ticket, session=session, phase="reviewing")

    def test_a_ledgerless_pr_is_outside_every_gate(self) -> None:
        assert resolve_gated_ticket(slug=_SLUG, pr_id=_PR_ID) is None
        assert_ticket_scoped_gates(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert_merge_quality_verdict(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert review_target_for_task(self.reviewing) is None

    def test_gates_bind_once_the_reconciler_records_the_row(self) -> None:
        assert reconcile_manual_prs([_refs_only_signal()]) == 1

        assert PullRequest.objects.get(url=_PR_URL).ticket == self.ticket
        with pytest.raises(MergePreconditionError, match=f"ticket {self.ticket.pk}"):
            assert_ticket_scoped_gates(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        with pytest.raises(MergeQualityVerdictError):
            assert_merge_quality_verdict(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert CriticDispatch.objects.filter(ticket=self.ticket, transition="merge", head_sha=_HEAD).exists()
        target = review_target_for_task(self.reviewing)
        assert target is not None
        assert (target.slug, target.pr_id) == (_SLUG, _PR_ID)


class TestDocsNameTheOwnershipFloor:
    _ROOT = Path(__file__).resolve().parents[3]

    @pytest.mark.parametrize("doc", ["BLUEPRINT.md", "docs/blueprint/rubric-done-gate.md"])
    def test_the_doc_names_the_refusal_and_the_reconciler_heal(self, doc: str) -> None:
        text = (self._ROOT / doc).read_text(encoding="utf-8")

        assert NO_OWNING_TICKET_REASON in text
        assert "ticket_owning_pr_branch" in text
