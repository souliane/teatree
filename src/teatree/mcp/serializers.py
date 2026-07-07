"""Pure model -> JSON-serializable dict converters for the MCP search tools.

The serializers are the read-only projection of each core model onto the
typed-JSON shape an MCP client consumes. They hold no query logic (that is
:mod:`teatree.mcp.search`) and no protocol wiring (that is
:mod:`teatree.mcp.server`) — given a loaded model instance they return a flat
dict of primitives, with every ``datetime`` rendered as a UTC ISO-8601 string
so the boundary stays JSON-safe.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from teatree.core.models.incoming_event import IncomingEvent
    from teatree.core.models.pull_request import PullRequest
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.worktree import Worktree


def _iso(value: "datetime | None") -> str | None:
    """ISO-8601 string for a datetime, or ``None`` — keeps the boundary JSON-safe."""
    return value.isoformat() if isinstance(value, datetime) else None


def serialize_ticket(ticket: "Ticket") -> dict[str, Any]:
    """Project a :class:`Ticket` onto its structured-search shape.

    ``extra`` is intentionally omitted — it is an unbounded internal JSON blob,
    not a search field. The lifecycle ``state``, ``overlay``, and the resolved
    ``ticket_number`` are the columns a client filters and renders on.
    """
    return {
        "id": ticket.pk,
        "ticket_number": ticket.ticket_number,
        "issue_url": ticket.issue_url,
        "overlay": ticket.overlay,
        "state": ticket.state,
        "role": ticket.role,
        "kind": ticket.kind,
        "variant": ticket.variant,
        "repos": list(ticket.repos or []),
        "short_description": ticket.short_description,
        "is_terminal": ticket.is_terminal,
        "remote_missing": ticket.remote_missing,
    }


def serialize_ticket_detail(ticket: "Ticket") -> dict[str, Any]:
    """The single-ticket projection: the base fields plus the visited-phase ledger.

    A list read serializes many rows and stays on :func:`serialize_ticket` (no
    per-row session query); the ``ticket_get`` single read affords the one extra
    query for ``visited_phases`` — the union of phases recorded across every
    session, the lifecycle-progress answer a client inspecting one ticket wants.
    """
    visited_phases, _ = ticket.aggregate_phase_records()
    return {**serialize_ticket(ticket), "visited_phases": visited_phases}


def serialize_task(task: "Task") -> dict[str, Any]:
    """Project a :class:`Task` onto its structured-search shape.

    ``ticket_number``, ``overlay`` and ``subject`` read through the related
    ticket — callers select_related ``ticket`` so this adds no per-row query.
    ``subject`` is the human-readable one-line description the statusline shows.
    """
    return {
        "id": task.pk,
        "ticket_id": task.ticket_id,  # ty: ignore[unresolved-attribute]
        "ticket_number": task.ticket.ticket_number,
        "overlay": task.ticket.overlay,
        "phase": task.phase,
        "status": task.status,
        "execution_target": task.execution_target,
        "subject": task.display_subject(),
        "claimed_by": task.claimed_by,
        "created_at": _iso(task.created_at),
        "claimed_at": _iso(task.claimed_at),
    }


def serialize_worktree(worktree: "Worktree") -> dict[str, Any]:
    """Project a :class:`Worktree` onto its structured-search shape.

    ``ticket_number`` reads through the related ticket — callers select_related
    ``ticket`` so this adds no per-row query. ``is_stale`` answers "does the
    on-disk worktree still exist", the question a status client most needs.
    """
    return {
        "id": worktree.pk,
        "ticket_id": worktree.ticket_id,  # ty: ignore[unresolved-attribute]
        "ticket_number": worktree.ticket.ticket_number,
        "overlay": worktree.overlay,
        "repo_path": worktree.repo_path,
        "branch": worktree.branch,
        "state": worktree.state,
        "db_name": worktree.db_name,
        "worktree_path": worktree.worktree_path,
        "is_stale": worktree.is_stale,
    }


def serialize_pull_request(pull_request: "PullRequest") -> dict[str, Any]:
    """Project a :class:`PullRequest` onto its structured-search shape."""
    return {
        "id": pull_request.pk,
        "ticket_id": pull_request.ticket_id,  # ty: ignore[unresolved-attribute]
        "overlay": pull_request.overlay,
        "url": pull_request.url,
        "repo": pull_request.repo,
        "iid": pull_request.iid,
        "state": pull_request.state,
        "slack_url": pull_request.slack_url,
        "review_requested_at": _iso(pull_request.review_requested_at),
    }


def serialize_incoming_event(event: "IncomingEvent") -> dict[str, Any]:
    """Project an :class:`IncomingEvent` onto its structured-search shape."""
    return {
        "id": event.pk,
        "source": event.source,
        "actor": event.actor,
        "channel_ref": event.channel_ref,
        "thread_ref": event.thread_ref,
        "is_thread_reply": event.is_thread_reply,
        "body": event.body,
        "received_at": _iso(event.received_at),
        "processed_at": _iso(event.processed_at),
        "idempotency_key": event.idempotency_key,
    }
