from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task
from teatree.core.worktree.survivor_notice import page_surviving_checkout
from tests.factories import TaskAttemptFactory, TaskFactory


class SurvivorNoticeTests(TestCase):
    def test_agent_cancel_offers_stop_as_one_owner_card(self) -> None:
        task = TaskFactory()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        TaskAttemptFactory(task=task, result={"cancel_source": "agent"})
        with (
            patch("teatree.core.worktree.survivor_notice.DeferredQuestion.record") as card,
            patch("teatree.core.worktree.survivor_notice.notify_user") as notice,
        ):
            page_surviving_checkout(task, Path("/checkout"), holder_session="session")

        notice.assert_not_called()
        card.assert_called_once()
        assert card.call_args.kwargs["dedupe_marker"] == f"checkout-exit-held:{task.pk}:session"
        options = card.call_args.kwargs["card"].options
        assert [option.label for option in options] == ["Keep checkout held", "Stop verified writers"]
        assert card.call_args.kwargs["card"].problems(card.call_args.args[0]) == []

    def test_owner_cancel_sends_one_idempotent_survivor_notice(self) -> None:
        task = TaskFactory()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        TaskAttemptFactory(task=task, result={"cancel_source": "owner"})
        with (
            patch("teatree.core.worktree.survivor_notice.DeferredQuestion.record") as card,
            patch("teatree.core.worktree.survivor_notice.notify_user") as notice,
        ):
            page_surviving_checkout(task, Path("/checkout"), holder_session="session")

        card.assert_not_called()
        notice.assert_called_once()
        assert notice.call_args.kwargs["idempotency_key"] == f"checkout-exit-held:{task.pk}:session"
