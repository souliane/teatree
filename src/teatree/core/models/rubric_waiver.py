from django.db import transaction
from django.utils import timezone

from teatree.core.models.reviewer_identity import is_non_reviewer_role
from teatree.core.models.rubric import Rubric, RubricError
from teatree.core.models.ticket import Ticket


def waive_criterion(ticket: Ticket, *, text_hash: str, waived_by: str, reason: str) -> dict[str, str]:
    identity = waived_by.strip()
    if not identity or is_non_reviewer_role(identity):
        msg = "an owner identity is required; an agent cannot waive a criterion"
        raise RubricError(msg)
    if not reason.strip():
        msg = "a non-empty waiver reason is required"
        raise RubricError(msg)
    with transaction.atomic():
        rubric = Rubric.objects.active_for_ticket(ticket)
        if rubric is None:
            msg = "the ticket has no rubric"
            raise RubricError(msg)
        criteria = list(rubric.criteria.select_for_update())
        criterion = next((row for row in criteria if row.text_hash == text_hash.strip().lower()), None)
        if criterion is None:
            msg = f"unknown criterion text_hash {text_hash}"
            raise RubricError(msg)
        if len(criteria) == 1:
            msg = "the last criterion cannot be waived"
            raise RubricError(msg)
        if criterion.status == criterion.Status.FAIL:
            msg = "a criterion whose latest grade is FAIL cannot be waived"
            raise RubricError(msg)
        waiver = {
            "text_hash": criterion.text_hash,
            "text": criterion.text,
            "waived_by": identity,
            "waived_at": timezone.now().isoformat(),
            "reason": reason.strip(),
        }
        current = Ticket.objects.select_for_update().get(pk=ticket.pk)
        waivers = list((current.extra or {}).get("rubric_waivers") or [])
        current.merge_extra(set_keys={"rubric_waivers": [*waivers, waiver]})
        criterion.delete()
    return waiver
