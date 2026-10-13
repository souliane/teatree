"""One owner-visible notice when a completed run leaves its checkout occupied."""

from pathlib import Path

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_cancel_source import cancel_source
from teatree.core.notify import NotifyKind, notify_user

_STOP_QUESTION = "Should the verified child writers be stopped?"


def page_surviving_checkout(task: Task, path: Path, *, holder_session: str) -> None:
    key = f"checkout-exit-held:{task.pk}:{holder_session}"
    cancelled_by_agent = (
        Task.objects.filter(pk=task.pk, status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED).exists()
        and cancel_source(task) == "agent"
    )
    if cancelled_by_agent:
        card = QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=(
                "An agent cancelled the task.",
                "The runner still sees processes in its checkout.",
                "No process has been signalled.",
            ),
            blocker="Stopping live processes needs the owner's decision.",
            why="The checkout stays occupied while those processes remain.",
            options=(
                CardOption(
                    "Keep checkout held", "Leave the processes running and the checkout reserved.", recommended=True
                ),
                CardOption("Stop verified writers", "Approve stopping runner children with open checkout writes."),
            ),
        )
        DeferredQuestion.record(_STOP_QUESTION, dedupe_marker=key, task_session=task.session, card=card)
        return
    notify_user(
        f"Ticket {task.ticket.pk} remains occupied after task {task.pk} exited; "
        f"inspect live checkout processes at {path}",
        kind=NotifyKind.INFO,
        idempotency_key=key,
        audience=NotifyAudience.OWNER_ESCALATION,
    )
