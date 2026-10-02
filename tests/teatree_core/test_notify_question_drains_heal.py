"""No surfacing drain posts a provision-failure question whose ticket has since provisioned (#4904)."""

import datetime as dt
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.models import Ticket, Worktree
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
    resurface_question_backlog,
)
from teatree.core.provision.failure_question import record_provision_failure_question
from tests._git_repo import make_git_repo


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-USER"
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000000"}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1800000000000000"
    return backend


class _HealedProvisionQuestion(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ticket = Ticket.objects.create(overlay="test", repos=[], state=Ticket.State.WORK_STARTED)
        self.question = record_provision_failure_question(ticket, "no repos on ticket", retries=6)
        self.live = DeferredQuestion.record("Which DB host?")
        self.ticket = ticket

    def _heal(self) -> None:
        Ticket.objects.filter(pk=self.ticket.pk).update(repos=["backend"])
        checkout = make_git_repo(self.root / "backend")
        Worktree.objects.create(
            ticket=self.ticket, repo_path="backend", branch="x", extra={"worktree_path": str(checkout)}
        )

    def _mirror_and_age(self, *rows: DeferredQuestion) -> None:
        for index, row in enumerate(rows):
            DeferredQuestion.objects.filter(pk=row.pk).update(
                slack_channel="D-USER", slack_ts=f"10{index}.0", created_at=timezone.now() - dt.timedelta(days=3)
            )
            row.refresh_from_db()

    def _posted_the_provision_question(self, backend: MagicMock) -> bool:
        return any("Provision failed" in str(call) for call in backend.post_message.call_args_list)

    def _assert_withdrawn(self) -> None:
        self.question.refresh_from_db()
        assert self.question.resolved_via == DeferredQuestion.ResolvedVia.STALE


class TestTheMirrorDrain(_HealedProvisionQuestion):
    def test_a_healed_row_is_withdrawn_instead_of_posted(self) -> None:
        self._heal()
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            assert drain_unmirrored_deferred_questions(user_id="U_ME") == (1, 1)

        self._assert_withdrawn()
        assert not self._posted_the_provision_question(backend)

    def test_an_unhealed_row_is_still_posted(self) -> None:
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            assert drain_unmirrored_deferred_questions(user_id="U_ME") == (2, 2)

        assert self._posted_the_provision_question(backend)
        self.question.refresh_from_db()
        assert self.question.is_pending

    def test_the_targeted_kick_withdraws_a_healed_row(self) -> None:
        self._heal()

        with patch.object(notify_module, "messaging_from_overlay", return_value=_backend()):
            assert drain_unmirrored_deferred_questions(user_id="U_ME", only_ref=self.question.stable_notify_ref) == (
                0,
                0,
            )

        self._assert_withdrawn()


class TestTheResurfaceDrain(_HealedProvisionQuestion):
    def test_a_healed_row_is_neither_posted_nor_counted(self) -> None:
        self._heal()
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            assert drain_deferred_questions(user_id="U_ME", backend=backend) == (1, 1)

        self._assert_withdrawn()
        assert not self._posted_the_provision_question(backend)


class TestTheDigest(_HealedProvisionQuestion):
    def test_a_healed_row_is_not_counted(self) -> None:
        self._heal()

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True):
            assert resurface_question_backlog() == (True, 1)

        self._assert_withdrawn()


class TestTheReask(_HealedProvisionQuestion):
    def test_a_healed_mirrored_row_is_not_bumped(self) -> None:
        self._mirror_and_age(self.question, self.live)
        self._heal()
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            assert reask_escalated_questions(user_id="U_ME", backend=backend) == (1, 1)

        self._assert_withdrawn()
        assert [call.kwargs["thread_ts"] for call in backend.post_message.call_args_list] == [self.live.slack_ts]
