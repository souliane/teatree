"""Ticket-detail drawer, the legal-FSM-transition POST, and the phase-enqueue POST (#3162, #4085).

No free drag-to-transition: FSM transitions are guarded methods with gates and
side effects, so the drawer offers only the legal transitions and the POST calls
the guarded model method (never a ``state=`` assignment). A ``TransitionNotAllowed``
— e.g. a stale menu racing a state change — is surfaced, not swallowed.

:func:`task_action` is the sibling that enqueues WORK rather than moving the FSM: it
creates a phase task through the same seam ``tasks create`` uses, so prioritising a PR
is a click rather than a raw DB query plus a CLI call. It can never record an OUTCOME —
no dashboard control writes a ``ReviewVerdict`` or a ``MergeClear``.
"""

from typing import TYPE_CHECKING

from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST
from django_fsm import TransitionNotAllowed

from teatree.core.models.task_enqueue import TaskEnqueueError, enqueue_phase_task_once
from teatree.core.models.ticket import Ticket
from teatree.dash import audit
from teatree.dash.task_actions import ENQUEUEABLE_PHASES
from teatree.dash.ticket_detail import build_ticket_detail, legal_transition_names
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import actor, error_page, is_htmx

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


def _ticket_or_404(ticket_id: int) -> Ticket:
    try:
        return Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist as exc:
        msg = f"no ticket {ticket_id}"
        raise Http404(msg) from exc


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


def _run_transition(ticket_id: int, action: str) -> tuple[str, str, str]:
    """``(before, after, refusal)`` for *action* — state read and written under ONE lock.

    Reading the state outside the write transaction let a request that opened its menu
    against an older state overwrite a newer one: both requests saw the transition as
    legal and the second ``save()`` clobbered the first. SQLite ignores
    ``select_for_update`` but opens every ``atomic()`` with ``BEGIN IMMEDIATE``, so the
    second writer blocks and then re-reads the committed state.
    """
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
        before = str(ticket.state)
        if action not in legal_transition_names(ticket):
            return before, before, f"transition {action!r} is not legal from state {before!r}"
        try:
            getattr(ticket, action)()
        except TransitionNotAllowed as exc:
            return before, before, f"transition refused: {exc}"
        ticket.save()
    return before, str(ticket.state), ""


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
        before, after, refusal = _run_transition(ticket_id, action)
    except Ticket.DoesNotExist as exc:
        msg = f"no ticket {ticket_id}"
        raise Http404(msg) from exc
    if refusal:
        return _refused(request, ticket_id, refusal)
    audit.record(
        actor=actor(request),
        action=f"ticket:{action}",
        target=str(ticket_id),
        before=before,
        after=after,
    )
    if not is_htmx(request):
        return redirect("dash:board")
    return _drawer(request, ticket_id)


@require_loopback_or_staff
@require_POST
def task_action(request: "HttpRequest", ticket_id: int) -> "HttpResponse":
    """POST a phase task onto the queue for this ticket — the "Review now" / "Ship now" button.

    Only the five phases the drawer offers are accepted, so the endpoint cannot be
    driven to enqueue an arbitrary phase string. A double click is REPORTED (the task
    already queued) rather than duplicated, which is why the guard lives in the seam
    and not in a check the second request could race past.
    """
    ticket = _ticket_or_404(ticket_id)
    phase = request.POST.get("phase", "").strip()
    if phase not in ENQUEUEABLE_PHASES:
        return _refused(request, ticket_id, f"phase {phase!r} is not one of {', '.join(ENQUEUEABLE_PHASES)}")

    who = actor(request)
    reason = request.POST.get("reason", "").strip() or f"Enqueued from the dashboard by {who}: run the {phase} phase."
    try:
        task = enqueue_phase_task_once(ticket=ticket, phase=phase, reason=reason)
    except TaskEnqueueError as exc:
        return _refused(request, ticket_id, str(exc))
    audit.record(
        actor=who,
        action=f"task:enqueue:{phase}",
        target=str(ticket_id),
        after=f"TODO-{task.pk} {task.phase}",
    )
    if not is_htmx(request):
        return redirect("dash:board")
    return _drawer(request, ticket_id)
