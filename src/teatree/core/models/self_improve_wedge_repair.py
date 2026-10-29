"""Repair legacy self-improve planning wedges through ticket transitions."""

from django.db import transaction
from django.db.models import Q
from django_fsm import TransitionNotAllowed

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.plan_adequacy import is_adequate
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.task import Task
from teatree.core.models.task_phase_disposition import PHASE_WEDGE_MARKER
from teatree.core.models.ticket import Ticket


def repair_legacy_self_improve_wedges(*, apply: bool) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {"repairable": [], "repaired": [], "missing_plan": [], "invalid_plan": []}
    ticket_ids = Ticket.objects.filter(state=Ticket.State.NOT_STARTED, extra__source="self_improve").values_list(
        "pk", flat=True
    )
    for ticket_id in ticket_ids:
        try:
            with transaction.atomic():
                ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
                if ticket.state != Ticket.State.NOT_STARTED:
                    continue
                task = (
                    Task.objects.filter(ticket=ticket, phase="planning", status=Task.Status.COMPLETED)
                    .order_by("-pk")
                    .first()
                )
                if task is None:
                    continue
                marker = PHASE_WEDGE_MARKER.format(ticket=ticket.pk, phase="planning", source=task.pk)
                legacy = f"fsm-wedge:{ticket.pk}:planning"
                question = DeferredQuestion.objects.filter(
                    Q(dedupe_marker__in=(marker, legacy)) | Q(tool_use_id__in=(marker, legacy)),
                    answered_at__isnull=True,
                    dismissed_at__isnull=True,
                ).first()
                if question is None:
                    continue
                artifact = PlanArtifact.objects.filter(ticket=ticket).order_by("-recorded_at", "-pk").first()
                if artifact is None:
                    result["missing_plan"].append(ticket.pk)
                    continue
                if not is_adequate(artifact.adequacy):
                    result["invalid_plan"].append(ticket.pk)
                    continue
                result["repairable"].append(ticket.pk)
                if not apply:
                    continue
                ticket.scope()
                ticket.save()
                ticket.start()
                ticket.save()
                ticket.plan(parent_task=task)
                ticket.save()
                question.mark_stale(
                    "planning wedge repaired through the ticket FSM", resolver_id="repair_self_improve_wedges"
                )
                result["repaired"].append(ticket.pk)
        except (InvalidTransitionError, TransitionNotAllowed):
            result["repairable"].remove(ticket_id)
            result["invalid_plan"].append(ticket_id)
    return result
