"""Ticket-detail drawer + the legal-FSM-transition action POST (#3162).

No free drag-to-transition: FSM transitions are guarded methods with gates and
side effects, so the drawer offers only the legal transitions and the POST calls
the guarded model method (never a ``state=`` assignment). A ``TransitionNotAllowed``
— e.g. a stale menu racing a state change — is surfaced, not swallowed.
"""

from typing import TYPE_CHECKING

from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST
from django_fsm import TransitionNotAllowed

from teatree.core.models.ticket import Ticket
from teatree.dash import audit
from teatree.dash.ticket_detail import build_ticket_detail, legal_transition_names
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor, error_page, is_htmx

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


def _drawer(request: "HttpRequest", ticket_id: int, *, error: str = "", status: int = 200) -> "HttpResponse":
    context = {"detail": build_ticket_detail(ticket_id), "drawer_error": error}
    return render(request, "dash/partials/_drawer.html", context, status=status)


def _refused(request: "HttpRequest", ticket_id: int, reason: str) -> "HttpResponse":
    """A refused transition — the drawer carrying its reason, or a navigable page."""
    if not is_htmx(request):
        return error_page(request, reason, back="dash:board")
    return _drawer(request, ticket_id, error=reason, status=400)


@require_loopback_or_staff
@require_POST
def ticket_transition(request: "HttpRequest", ticket_id: int) -> "HttpResponse":
    """POST a single legal FSM transition, executed via the guarded model method.

    An htmx request gets the refreshed DRAWER: the transition happened inside it, and
    redirecting to the board closed the very panel the operator was working in. The
    board behind it re-renders on its own 4s poll. A non-htmx client keeps the redirect.
    """
    action = request.POST.get("action", "").strip()
    try:
        ticket = Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist as exc:
        msg = f"no ticket {ticket_id}"
        raise Http404(msg) from exc

    if action not in legal_transition_names(ticket):
        return _refused(request, ticket_id, f"transition {action!r} is not legal from state {ticket.state!r}")

    before = str(ticket.state)
    try:
        with transaction.atomic():
            getattr(ticket, action)()
            ticket.save()
    except TransitionNotAllowed as exc:
        return _refused(request, ticket_id, f"transition refused: {exc}")
    audit.record(
        actor=actor(request),
        action=f"ticket:{action}",
        target=str(ticket_id),
        before=before,
        after=str(ticket.state),
    )
    if not is_htmx(request):
        return redirect("dash:board")
    return _drawer(request, ticket_id)
