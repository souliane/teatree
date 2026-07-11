"""Ticket-detail drawer + the legal-FSM-transition action POST (#3162).

No free drag-to-transition: FSM transitions are guarded methods with gates and
side effects, so the drawer offers only the legal transitions and the POST calls
the guarded model method (never a ``state=`` assignment). A ``TransitionNotAllowed``
— e.g. a stale menu racing a state change — is surfaced, not swallowed.
"""

from typing import TYPE_CHECKING

from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST
from django_fsm import TransitionNotAllowed

from teatree.core.models.ticket import Ticket
from teatree.dash import audit
from teatree.dash.ticket_detail import build_ticket_detail, legal_transition_names
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_loopback_or_staff
@require_GET
def ticket_drawer(request: "HttpRequest", ticket_id: int) -> "HttpResponse":
    """The per-ticket detail drawer: history, lifecycle Mermaid, tasks, actions menu."""
    try:
        detail = build_ticket_detail(ticket_id)
    except Ticket.DoesNotExist as exc:
        msg = f"no ticket {ticket_id}"
        raise Http404(msg) from exc
    return render(request, "dash/partials/_drawer.html", {"detail": detail})


@require_loopback_or_staff
@require_POST
def ticket_transition(request: "HttpRequest", ticket_id: int) -> "HttpResponse":
    """POST a single legal FSM transition, executed via the guarded model method."""
    action = request.POST.get("action", "").strip()
    try:
        ticket = Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist as exc:
        msg = f"no ticket {ticket_id}"
        raise Http404(msg) from exc

    if action not in legal_transition_names(ticket):
        return HttpResponseBadRequest(f"transition {action!r} is not legal from state {ticket.state!r}")

    before = str(ticket.state)
    try:
        with transaction.atomic():
            getattr(ticket, action)()
            ticket.save()
    except TransitionNotAllowed as exc:
        return HttpResponseBadRequest(f"transition refused: {exc}")
    audit.record(
        actor=actor(request),
        action=f"ticket:{action}",
        target=str(ticket_id),
        before=before,
        after=str(ticket.state),
    )
    return redirect("dash:board")
