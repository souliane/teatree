"""An owner answer typed in an attended session closes the card and resumes the parked task, as a tap does (#5096)."""

from collections.abc import Sequence
from dataclasses import dataclass

from teatree.core import notify
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.session_identity import current_session_id
from teatree.loop.question_binding import BoundAnswer, apply_bound_answer


@dataclass(frozen=True, slots=True)
class AttendedOutcome:
    answered: tuple[int, ...]
    already_answered: tuple[int, ...]
    refused: tuple[int, ...]


def answer_attended(question_ids: Sequence[int], text: str) -> AttendedOutcome:
    """Answer each pending owner card through the Slack answer seam; an action card only its own button answers."""
    backend = notify.resolve_owner_dm_backend()[0]
    answered: list[int] = []
    already: list[int] = []
    refused: list[int] = []
    for pk in question_ids:
        row = DeferredQuestion.objects.filter(pk=pk, audience=DeferredQuestion.Audience.OWNER_QUESTION).first()
        if row is None:
            refused.append(pk)
        elif not row.is_pending:
            already.append(pk)
        else:
            bound = BoundAnswer(
                question=row, answer=text, answered_by=current_session_id(), via=DeferredQuestion.ResolvedVia.ATTENDED
            )
            (answered if apply_bound_answer(bound, backend=backend) else refused).append(pk)
    return AttendedOutcome(tuple(answered), tuple(already), tuple(refused))
