"""The PR board, the ticket-FSM kanban board, and their htmx-poll partials (#3162, #5007)."""

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


_PAGE_PREFIX = "page_"
_COLLAPSED_STATES = (Ticket.State.NOT_STARTED, Ticket.State.DELIVERED)
_TRUTHY = {"1", "true", "on"}


class BoardContext(TypedDict):
    board: KanbanBoard
    filters: BoardFilters
    page_params: tuple[tuple[str, int], ...]


def _page_number(raw: str) -> int:
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _filters(request: "HttpRequest") -> BoardFilters:
    pages = {
        key.removeprefix(_PAGE_PREFIX): _page_number(value)
        for key, value in request.GET.items()
        if key.startswith(_PAGE_PREFIX)
    }
    return BoardFilters(
        overlay=request.GET.get("overlay", "").strip(),
        role=request.GET.get("role", "").strip(),
        kind=request.GET.get("kind", "").strip(),
        text=request.GET.get("text", "").strip(),
        include_ignored=request.GET.get("ignored") in _TRUTHY,
        pages=pages,
    )


def _page_url(request: "HttpRequest", state: str, page: int) -> str:
    params = request.GET.copy()
    params[f"{_PAGE_PREFIX}{state}"] = str(page)
    return f"{reverse('dash:board_lifecycle')}?{params.urlencode()}"


def _with_pager(request: "HttpRequest", column: KanbanColumn) -> KanbanColumn:
    return replace(
        column,
        next_url=_page_url(request, column.state, column.page + 1) if column.has_next_page else "",
        prev_url=_page_url(request, column.state, column.page - 1) if column.page else "",
        collapsed=column.state in _COLLAPSED_STATES,
    )


def _board_context(request: "HttpRequest") -> BoardContext:
    filters = _filters(request)
    board = build_kanban_columns(filters)
    groups = tuple(
        replace(group, columns=tuple(_with_pager(request, column) for column in group.columns))
        for group in board.groups
    )
    hidden = tuple(_with_pager(request, column) for column in board.hidden)
    paged = tuple(
        (f"{_PAGE_PREFIX}{column.state}", column.page)
        for column in (*(column for group in groups for column in group.columns), *hidden)
        if column.page
    )
    return {"board": replace(board, groups=groups, hidden=hidden), "filters": filters, "page_params": paged}


@require_loopback_or_staff
@require_GET
def board(request: "HttpRequest") -> "HttpResponse":
    """Full board page — the open pull requests grouped by who acts next."""
    return render(
        request,
        "dash/board.html",
        {**nav_context("dash:board"), "pr_board": build_pr_board(), "merge_summary": build_merge_summary()},
    )


@require_loopback_or_staff
@require_GET
def board_prs_partial(request: "HttpRequest") -> "HttpResponse":
    """Just the PR groups — the target of the PR board's htmx poll (no page chrome)."""
    return render(request, "dash/partials/_pr_columns.html", {"pr_board": build_pr_board()})


@require_loopback_or_staff
@require_GET
def board_lifecycle(request: "HttpRequest") -> "HttpResponse":
    """Full lifecycle page — the kanban of tickets grouped by ``Ticket.state``."""
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
