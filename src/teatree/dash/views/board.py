"""The ticket-FSM kanban board and its htmx-poll column partial (#3162)."""

from dataclasses import replace
from typing import TYPE_CHECKING, TypedDict

from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from teatree.core.models.ticket import Ticket
from teatree.dash.cycle_time import build_merge_summary
from teatree.dash.pr_board import build_pr_board
from teatree.dash.selectors import BoardFilters, KanbanBoard, KanbanColumn, build_kanban_columns
from teatree.dash.views.access import require_loopback_or_staff
from teatree.dash.views.base import nav_context

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


_LIFECYCLE_PAGE_SIZE = 50
_PAGE_KEYS = {Ticket.State.NOT_STARTED: "page_not_started", Ticket.State.DELIVERED: "page_delivered"}


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


def _page_number(raw: str) -> int:
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _page_url(request: "HttpRequest", key: str, page: int) -> str:
    params = request.GET.copy()
    params[key] = str(page)
    return f"{reverse('dash:board_lifecycle')}?{params.urlencode()}"


def _page_column(request: "HttpRequest", column: KanbanColumn) -> KanbanColumn:
    key = _PAGE_KEYS.get(column.state)
    if key is None:
        return column
    page = _page_number(request.GET.get(key, "0"))
    start = page * _LIFECYCLE_PAGE_SIZE
    total = len(column.cards)
    return replace(
        column,
        cards=column.cards[start : start + _LIFECYCLE_PAGE_SIZE],
        total_count=total,
        page=page,
        next_url=_page_url(request, key, page + 1) if start + _LIFECYCLE_PAGE_SIZE < total else "",
        prev_url=_page_url(request, key, page - 1) if page > 0 else "",
        collapsed=True,
    )


def _board_context(request: "HttpRequest") -> BoardContext:
    filters = _filters(request)
    board = build_kanban_columns(filters)
    groups = tuple(
        replace(group, columns=tuple(_page_column(request, column) for column in group.columns))
        for group in board.groups
    )
    return {"board": replace(board, groups=groups), "filters": filters}


@require_loopback_or_staff
@require_GET
def board(request: "HttpRequest") -> "HttpResponse":
    return render(
        request,
        "dash/board.html",
        {**nav_context("dash:board"), "pr_board": build_pr_board(), "merge_summary": build_merge_summary()},
    )


@require_loopback_or_staff
@require_GET
def board_prs_partial(request: "HttpRequest") -> "HttpResponse":
    return render(request, "dash/partials/_pr_columns.html", {"pr_board": build_pr_board()})


@require_loopback_or_staff
@require_GET
def board_lifecycle(request: "HttpRequest") -> "HttpResponse":
    return render(
        request,
        "dash/board_lifecycle.html",
        {**nav_context("dash:board"), **_board_context(request), "merge_summary": build_merge_summary()},
    )


@require_loopback_or_staff
@require_GET
def board_columns_partial(request: "HttpRequest") -> "HttpResponse":
    """Just the columns fragment — the target of the htmx poll (no page chrome)."""
    return render(request, "dash/partials/_columns.html", _board_context(request))
