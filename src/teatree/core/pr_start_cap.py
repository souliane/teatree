"""The fixed live-PR cap shared by admission seats and task claims."""

from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models
from django.db.models import Exists, OuterRef, Q, Value
from django.db.models.functions import Coalesce
from django.db.models.lookups import LessThan

from teatree.core.modelkit.phases import phase_spellings

if TYPE_CHECKING:
    from teatree.core.models.pull_request import PullRequest
    from teatree.core.models.ticket import Ticket

OPEN_PR_START_CAP = 3
_PR_START_PHASES = tuple(
    spelling for phase in ("coding", "testing", "debugging", "e2e", "shipping") for spelling in phase_spellings(phase)
)


def new_pr_start_allowed_q() -> Q:
    ticket = cast("type[Ticket]", apps.get_model("core", "Ticket"))
    pull_request = cast("type[PullRequest]", apps.get_model("core", "PullRequest"))
    open_prs = pull_request.objects.live()
    open_count = open_prs.order_by().values(_all=Value(1)).annotate(total=models.Count("*")).values("total")[:1]
    below_cap = LessThan(Coalesce(models.Subquery(open_count), Value(0)), OPEN_PR_START_CAP)
    own_pr = ticket_has_live_pr()
    might_start_pr = Q(ticket__role=ticket.Role.AUTHOR, phase__in=_PR_START_PHASES)
    return ~might_start_pr | Q(ticket__expedited=True) | Q(own_pr) | Q(below_cap)


def ticket_has_live_pr() -> Exists:
    pull_request = cast("type[PullRequest]", apps.get_model("core", "PullRequest"))
    return Exists(pull_request.objects.live().filter(ticket_id=OuterRef("ticket_id")))


def cap_hold_reason(task_pk: int) -> str | None:
    task = apps.get_model("core", "Task")
    if task.objects.filter(pk=task_pk).filter(new_pr_start_allowed_q()).exists():
        return None
    return f"{OPEN_PR_START_CAP} or more pull requests are open; this task could start another"
