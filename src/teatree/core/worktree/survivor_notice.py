"""One owner-visible notice when a completed run leaves its checkout occupied."""

from pathlib import Path
from typing import TypedDict, cast

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_cancel_source import cancel_source
from teatree.core.models.task_claim import claim_generation
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.worktree_occupancy import _worktree_at, occupancy_holder, task_holder_id
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.cancel_processes import stop_recorded_writers

_STOP_QUESTION = "Should the verified child writers be stopped?"


class WriterProcess(TypedDict):
    pid: int
    start_ticks: int


class WriterSnapshot(TypedDict):
    namespace: str
    claim_generation: str
    processes: list[WriterProcess]


def _current_writer_snapshot(task: Task, attempt: TaskAttempt | None) -> WriterSnapshot | None:
    if attempt is None or not isinstance(attempt.result, dict):
        return None
    snapshot = attempt.result.get("surviving_writers")
    generation = claim_generation(task)
    if (
        not isinstance(snapshot, dict)
        or not generation
        or snapshot.get("claim_generation") != generation
        or not isinstance(snapshot.get("namespace"), str)
        or not isinstance(snapshot.get("processes"), list)
    ):
        return None
    processes = snapshot["processes"]
    if not processes or any(
        not isinstance(entry, dict) or type(entry.get("pid")) is not int or type(entry.get("start_ticks")) is not int
        for entry in processes
    ):
        return None
    return cast("WriterSnapshot", snapshot)


def page_surviving_checkout(task: Task, path: Path, *, holder_session: str) -> None:
    key = f"checkout-exit-held:{task.pk}:{holder_session}"
    cancelled_by_agent = (
        Task.objects.filter(pk=task.pk, status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED).exists()
        and cancel_source(task) == "agent"
    )
    candidates = TaskAttempt.objects.filter(task=task, result__has_key="surviving_writers").order_by("-pk").first()
    if cancelled_by_agent and _current_writer_snapshot(task, candidates) is not None:
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


def apply_stop_answer(question: DeferredQuestion) -> tuple[int, ...]:
    if (
        question.answer_text.strip() != "Stop verified writers"
        or question.resolved_via != DeferredQuestion.ResolvedVia.SLACK
        or not question.dedupe_marker.startswith("checkout-exit-held:")
    ):
        return ()
    target = _stop_target(question)
    if target is None:
        return ()
    task, path = target
    attempt = TaskAttempt.objects.filter(task=task, result__has_key="surviving_writers").order_by("-pk").first()
    snapshot = _current_writer_snapshot(task, attempt)
    if snapshot is None:
        return ()
    candidates = tuple((entry["pid"], entry["start_ticks"]) for entry in snapshot["processes"])
    return stop_recorded_writers(Path(path), candidates, str(snapshot.get("namespace") or ""))


def _stop_target(question: DeferredQuestion) -> tuple[Task, str] | None:
    _, _, identity = question.dedupe_marker.partition("checkout-exit-held:")
    task_id, separator, holder_session = identity.partition(":")
    if not separator or not task_id.isdigit():
        return None
    task = Task.objects.filter(pk=int(task_id), status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED).first()
    if task is None or cancel_source(task) != "agent":
        return None
    path = dispatch_worktree_path(task.ticket)
    worktree = _worktree_at(task.ticket, path) if path else None
    held = occupancy_holder(worktree) if worktree is not None else None
    if held is None or held.holder != task_holder_id(task) or held.holder_session != holder_session:
        return None
    return task, path
