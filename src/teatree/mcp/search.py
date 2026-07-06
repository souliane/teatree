"""Read-only structured-search queries over the teatree core models.

These are the synchronous query functions the MCP tools expose. Each reuses the
existing model managers (``Ticket.objects.for_overlay`` / ``in_flight`` /
``resolve``, ``Worktree.objects.active``, ``Task.objects.for_overlay``,
``IncomingEvent.objects.unprocessed``) rather than re-deriving query logic in
the protocol layer — the manager is the
single source of truth for what "in-flight" or "active" or "this overlay" means,
and the MCP surface is a thin read over it.

Every function returns already-serialized JSON-safe data so the async server
wrappers only have to cross the sync boundary once (see
:mod:`teatree.mcp.server`). They never mutate — write operations stay on the
FSM-guarded ``t3`` CLI per BLUEPRINT's orchestrator-decides / loop-executes
topology.
"""

from typing import Any

from django.db.models import Count, Q

from teatree.config import get_effective_settings
from teatree.core.factory.factory_score import score as factory_score_compute
from teatree.core.factory.factory_signals import DEFAULT_WINDOW_DAYS, compute_factory_signals
from teatree.core.models import IncomingEvent, PullRequest, ReplyDispatch, Task, Ticket, Worktree
from teatree.mcp.serializers import (
    serialize_incoming_event,
    serialize_pull_request,
    serialize_ticket,
    serialize_worktree,
)

_DEFAULT_TICKET_LIMIT = 50
_DEFAULT_EVENT_LIMIT = 20
_MAX_LIMIT = 200


def _capped(limit: int, default: int) -> int:
    """A bounded row cap for a page request.

    A non-positive request falls back to *default*, and any request is clamped
    to ``_MAX_LIMIT`` so a client cannot ask the read-only server for an
    unbounded page.
    """
    return min(limit if limit and limit > 0 else default, _MAX_LIMIT)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def ticket_search(  # noqa: PLR0913 — search entry-point; each kwarg is a documented, keyword-only filter, mapped 1:1 to the public MCP tool input schema, not an internal design smell.
    *,
    overlay: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    role: str | None = None,
    text: str | None = None,
    in_flight: bool = False,
    limit: int = _DEFAULT_TICKET_LIMIT,
) -> list[dict[str, Any]]:
    """Tickets matching the given filters, newest first.

    ``overlay`` scopes through ``TicketManager.for_overlay`` (which also
    includes legacy empty-overlay rows). ``in_flight=True`` narrows to the
    not-yet-delivered/ignored set via ``TicketManager.in_flight``. ``text`` is a
    case-insensitive substring match across ``issue_url``, ``short_description``
    and the durable per-ticket ``context``.
    """
    queryset = Ticket.objects.in_flight(overlay) if in_flight else Ticket.objects.for_overlay(overlay)
    if state:
        queryset = queryset.filter(state=state)
    if kind:
        queryset = queryset.filter(kind=kind)
    if role:
        queryset = queryset.filter(role=role)
    if text:
        queryset = queryset.filter(
            Q(issue_url__icontains=text) | Q(short_description__icontains=text) | Q(context__icontains=text),
        )
    rows = queryset.order_by("-pk")[: _capped(limit, _DEFAULT_TICKET_LIMIT)]
    return [serialize_ticket(ticket) for ticket in rows]


def worktree_status(
    *,
    ticket: str | None = None,
    overlay: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Worktrees for a ticket, or the overlay's worktrees when no ticket is given.

    ``ticket`` is resolved through the canonical ``TicketManager.resolve`` (pk,
    bare issue number, or full issue URL); an unresolvable reference returns an
    empty list rather than raising, so the long-running server never crashes on
    a bad argument. Without a ticket, ``active_only`` (default) lists the
    overlay's in-flight worktrees via ``WorktreeManager.active``.
    """
    if ticket:
        try:
            resolved = Ticket.objects.resolve(ticket)
        except Ticket.DoesNotExist:
            return []
        queryset = Worktree.objects.filter(ticket=resolved)
    elif active_only:
        queryset = Worktree.objects.active(overlay)
    else:
        queryset = Worktree.objects.for_overlay(overlay)
    rows = queryset.select_related("ticket").order_by("-pk")
    return [serialize_worktree(worktree) for worktree in rows]


def pr_for_ticket(*, ticket: str) -> list[dict[str, Any]]:
    """Open/merged pull requests recorded for a ticket (newest first).

    A ticket can carry several PRs (one per repo in a multi-repo ticket), so
    this returns a list. An unresolvable ``ticket`` reference returns an empty
    list.
    """
    try:
        resolved = Ticket.objects.resolve(ticket)
    except Ticket.DoesNotExist:
        return []
    rows = PullRequest.objects.filter(ticket=resolved).order_by("-pk")
    return [serialize_pull_request(pull_request) for pull_request in rows]


def loop_stats(*, overlay: str | None = None) -> dict[str, Any]:
    """Task-status counts for the autonomous loop, plus the dead-letter total.

    ``tasks`` is a count per ``Task.Status`` (pending / claimed / completed /
    failed) — the loop's work queue at a glance. ``overlay`` scopes the tasks
    through ``TaskQuerySet.for_overlay`` — the same overlay clause the loop's
    own claim path uses (spanning the ticket and session relations, legacy
    empty-overlay rows included). ``dead_letter`` is the global count of reply
    dispatches that exhausted their retries — a system-health signal that is
    not overlay-scoped (the inbound event it answers carries no overlay).
    """
    tasks = Task.objects.for_overlay(overlay)
    counts = {status.value: 0 for status in Task.Status}
    for row in tasks.values("status").annotate(total=Count("pk")):
        counts[row["status"]] = row["total"]
    dead_letter = ReplyDispatch.objects.filter(status=ReplyDispatch.Status.DEAD_LETTER).count()
    return {"overlay": overlay or "", "tasks": counts, "dead_letter": dead_letter}


def factory_signals(*, overlay: str | None = None, window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """The derived-on-read factory quality/velocity signals report (read-only).

    Delegates to :func:`teatree.core.factory.factory_signals.compute_factory_signals` —
    the same computation path ``t3 <overlay> signals`` uses. Returns the report's
    ``to_dict()``: the ``overlay`` scope, the trailing window's five signals
    (first-try-green, defect-escape, review-catch, merge-latency, repair-burn)
    with their fail-loud statuses, red-floor trips, and the top-level verdict the
    outer loop keys on.

    Scope: an omitted ``overlay`` means the whole-factory GLOBAL view (``""``) —
    where the CLI reads ``T3_OVERLAY_NAME`` and scopes to the active overlay,
    this MCP tool defaults global unless the caller passes one. Both surfaces now
    stamp the resolved scope in the payload's ``overlay`` field so a global
    reading is distinguishable from a scoped one from the output alone (#25). The
    two surfaces are held schema-identical by the named
    ``tests/conformance/test_signals_scope_parity.py`` lane — the parity guard
    that replaced the earlier unenforced "can never drift" docstring claim.
    """
    report = compute_factory_signals(window_days=window_days, overlay=overlay or "")
    return report.to_dict()


def factory_score(*, overlay: str | None = None, window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """The recipe-weighted factory score over the trailing window (read-only).

    Delegates to :func:`teatree.core.factory.factory_score.score` — the same recipe fold
    ``t3 <overlay> recipe score`` uses. Returns the score payload: the aggregate
    (``None`` when untrustworthy), the ``ok`` / ``regressing`` / ``red`` verdict,
    coverage vs floor, the recipe provenance (``recipe_sha`` + ``recipe_approved``),
    the snapshot deltas, and the per-signal contributions. Registered only when
    ``factory_score_enabled`` is on — absent otherwise (the shipped OFF state).
    """
    settings = get_effective_settings(overlay or None)
    result = factory_score_compute(
        window_days=window_days,
        overlay=overlay or "",
        approved_recipe_sha=settings.approved_recipe_sha,
    )
    return result.to_dict()


def incoming_event_recent(
    *,
    limit: int = _DEFAULT_EVENT_LIMIT,
    source: str | None = None,
    unprocessed_only: bool = False,
) -> list[dict[str, Any]]:
    """The most recent inbound platform events, newest first.

    ``source`` filters to one platform (``slack`` / ``gitlab`` / ``github`` /
    ``notion`` / ``ci``). ``unprocessed_only`` narrows to events the dispatcher
    has not yet handled via ``IncomingEventManager.unprocessed``.
    """
    queryset = IncomingEvent.objects.unprocessed() if unprocessed_only else IncomingEvent.objects.all()
    if source:
        queryset = queryset.filter(source=source)
    rows = queryset.order_by("-received_at", "-pk")[: _capped(limit, _DEFAULT_EVENT_LIMIT)]
    return [serialize_incoming_event(event) for event in rows]
