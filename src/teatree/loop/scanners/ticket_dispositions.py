"""Detect tickets whose underlying issue has changed in ways that warrant disposition.

Three signals can suggest a ticket should be ignored or torn down:

1. The remote issue was closed externally (often because a different PR's
    ``Fixes #N`` keyword merged and the platform auto-closed the issue).
2. The issue was reassigned away from the active user.
3. The configured ready-label was removed by a colleague.

This scanner only **reports** these conditions — it never transitions the
:class:`Ticket` or tears down the worktree on its own. The dispatcher routes
each finding to the statusline ``action_needed`` zone so the operator can
review and decide (transition to ``IGNORED``, run ``worktree teardown``,
reassign back, etc.).

Tickets past ``REVIEWED`` are skipped: once the PR exists, the disposition
concept no longer applies — ``MyPrsScanner`` already covers post-PR state.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.backends.protocols import CodeHostBackend
from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

# States in which disposition checks make sense — pre-PR work the operator can
# still cancel without tearing down published artifacts.
_DISPOSITIONABLE_STATES: frozenset[str] = frozenset({"not_started", "scoped", "started", "coded", "tested", "reviewed"})


@dataclass(frozen=True, slots=True)
class _IssueSnapshot:
    state: str
    assignees: tuple[str, ...]
    labels: tuple[str, ...]


def _normalize_username_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for key in ("username", "login"):  # GitLab uses username, GitHub uses login
                value = cast("RawAPIDict", item).get(key)
                if isinstance(value, str):
                    out.append(value)
                    break
    return tuple(out)


def _normalize_label_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                out.append(name)
    return tuple(out)


def _snapshot(issue: RawAPIDict) -> _IssueSnapshot | None:
    if "error" in issue:
        return None
    state = issue.get("state")
    if not isinstance(state, str):
        return None
    return _IssueSnapshot(
        state=state,
        assignees=_normalize_username_list(issue.get("assignees")),
        labels=_normalize_label_list(issue.get("labels")),
    )


@dataclass(slots=True)
class TicketDispositionScanner:
    """Flag active tickets whose remote issue has drifted from local state.

    Iterates the local :class:`Ticket` table (filtered by *overlay_name* when
    set) and calls :meth:`CodeHostBackend.get_issue` once per active ticket
    with a non-empty ``issue_url``. Emits a ``ticket.disposition_candidate``
    signal per detected drift; the operator decides whether to dispose.
    """

    host: CodeHostBackend
    ready_labels: tuple[str, ...] = field(default_factory=tuple)
    overlay_name: str = ""
    name: str = "ticket_dispositions"

    def scan(self) -> list[ScanSignal]:
        author = self.host.current_user()
        signals: list[ScanSignal] = []
        for ticket in self._candidate_tickets():
            try:
                issue = self.host.get_issue(ticket.issue_url)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to fetch issue for ticket %s (%s), skipping", ticket.pk, ticket.issue_url)
                continue
            snap = _snapshot(issue)
            if snap is None:
                continue
            signals.extend(
                ScanSignal(
                    kind="ticket.disposition_candidate",
                    summary=f"Ticket {ticket.ticket_number} — {reason}",
                    payload={
                        "ticket_id": ticket.pk,
                        "ticket_state": ticket.state,
                        "issue_url": ticket.issue_url,
                        "reason": reason,
                    },
                )
                for reason in self._detect_reasons(snap, author)
            )
        return signals

    def _candidate_tickets(self) -> Iterable["Ticket"]:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.filter(state__in=_DISPOSITIONABLE_STATES).exclude(issue_url="")
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        return qs.only("id", "issue_url", "state", "overlay")

    def _detect_reasons(self, snap: _IssueSnapshot, author: str) -> list[str]:
        reasons: list[str] = []
        if snap.state in {"closed", "completed", "cancelled"}:
            reasons.append("issue_closed")
        if author and snap.assignees and author not in snap.assignees:
            reasons.append("unassigned")
        if self.ready_labels and not any(label in snap.labels for label in self.ready_labels):
            reasons.append("label_removed")
        return reasons
