"""A crashed critic is decided by its own dispatch budget, not by the ticket-phase budget.

The generic repair budget caps attempts per ``(ticket, phase)`` and stops on two identical
failures. A merge critic is re-armed by the gate under its own bound
(:data:`~teatree.core.models.auto_review_dispatch.MAX_DISPATCH_ATTEMPTS` on
:class:`~teatree.core.models.critic_dispatch.CriticDispatch`), so a crash that still has dispatch
budget is reopened and one at the cap is escalated, whatever the ticket-phase count says. A crash
whose verdict already landed, or whose dispatch moved on to a newer task, is parked without a
question: nothing is left for a person to answer.
"""

from collections.abc import Callable

from teatree.core.modelkit.task_failure_taxonomy import RecoveryStrategy, classify_failure, recovery_strategy
from teatree.core.models import Task
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_verdict import CriticVerdict
from teatree.loop.transient_requeue_disposal import park_live_successor

_CRITIC_PHASE = "critic_reviewing"


def critic_dispatch_halt(task: Task, *, fallback: Callable[[Task], str | None]) -> str | None:
    """``None`` to reopen *task*, else the halt reason; *fallback* decides a task no dispatch governs."""
    if not CriticDispatch.objects.filter(task=task).exists():
        return fallback(task)
    if CriticDispatch.spend_reopen(task):
        return None
    return f"the critic dispatch spent its {MAX_DISPATCH_ATTEMPTS} attempts without recording a verdict."


def park_if_critic_settled(task: Task, *, error: str) -> bool:
    """Park a crashed critic whose work is already settled, with no question; ``True`` if parked."""
    if task.phase != _CRITIC_PHASE or recovery_strategy(classify_failure(error)) is not RecoveryStrategy.RETRY:
        return False
    if not _is_settled(task):
        return False
    park_live_successor(task)
    return True


def _is_settled(task: Task) -> bool:
    dispatch = CriticDispatch.objects.filter(task=task).first()
    if dispatch is None:
        return CriticDispatch.objects.filter(
            ticket_id=task.ticket_id, task__phase=_CRITIC_PHASE, task_id__gt=task.pk
        ).exists()
    return (
        dispatch.state == CriticDispatch.State.RESOLVED
        or CriticVerdict.objects.filter(
            ticket_id=task.ticket_id, transition=dispatch.transition, head_sha=dispatch.head_sha
        ).exists()
    )
