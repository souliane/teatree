"""The ticket-FSM kanban board and its htmx-poll column partial (#3162)."""

from typing import TYPE_CHECKING, TypedDict

from django.shortcuts import render
from django.views.decorators.http import require_GET

from teatree.dash.selectors import BoardFilters, KanbanBoard, build_kanban_columns
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class BoardContext(TypedDict):
    board: KanbanBoard
    filters: BoardFilters


def _filters(request: "HttpRequest") -> BoardFilters:
    params = request.GET
    return BoardFilters(
        overlay=params.get("overlay", "").strip(),
        role=params.get("role", "").strip(),
        kind=params.get("kind", "").strip(),
        text=params.get("text", "").strip(),
        include_ignored=params.get("ignored") in {"1", "true", "on"},
    )


def _board_context(request: "HttpRequest") -> BoardContext:
    filters = _filters(request)
    return {"board": build_kanban_columns(filters), "filters": filters}


@require_loopback_or_staff
@require_GET
def board(request: "HttpRequest") -> "HttpResponse":
    """Full board page — the kanban of tickets grouped by ``Ticket.state``."""
    return render(request, "dash/board.html", {**nav_context("dash:board"), **_board_context(request)})


@require_loopback_or_staff
@require_GET
def board_columns_partial(request: "HttpRequest") -> "HttpResponse":
    """Just the columns fragment — the target of the htmx poll (no page chrome)."""
    return render(request, "dash/partials/_columns.html", _board_context(request))
