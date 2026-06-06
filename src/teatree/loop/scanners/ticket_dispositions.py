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

from teatree.backends.loader import get_code_host_for_url
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

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


@dataclass(frozen=True, slots=True)
class _DispositionReason:
    """One detected drift reason plus the fields the statusline needs.

    ``unassigned`` carries ``old_owner`` (the active user the ticket was
    taken from) and ``new_owners`` (whoever the issue now points at) so the
    statusline renders the transition explicitly. Other reasons leave both
    empty and contribute no extra payload keys.
    """

    reason: str
    old_owner: str = ""
    new_owners: tuple[str, ...] = ()

    def payload_extra(self) -> dict[str, str | list[str]]:
        if self.reason == "unassigned" and self.old_owner and self.new_owners:
            return {"old_owner": self.old_owner, "new_owners": list(self.new_owners)}
        return {}


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

    ``user_identity_aliases`` lists usernames/handles that all map to the
    operating human (e.g. a GitHub login, a GitLab username, an internal
    handle). When a reassignment moves the issue between two such aliases —
    both ``old_owner`` and every member of ``new_owners`` fall inside the
    set — the scanner drops the ``unassigned`` signal entirely: it's
    plumbing, not an actionable handoff. Reassigns crossing the alias
    boundary (alias → colleague, colleague → alias, or alias → mixed) still
    render normally.

    ``identity_alias_groups`` is the multi-human shape (#1015): each inner
    tuple is one human's aliases. Suppression fires when SOME group
    contains ``old_owner`` AND every ``new_owner`` — i.e. one human swapped
    between their own handles. Reassigns that cross group boundaries
    (human A → human B) still surface. Groups take precedence over
    ``user_identity_aliases``; when no groups are set, the flat list is
    treated as one implicit group, preserving the pre-#1015 contract.
    """

    host: CodeHostBackend
    overlay: OverlayBase | None = None
    ready_labels: tuple[str, ...] = field(default_factory=tuple)
    overlay_name: str = ""
    user_identity_aliases: tuple[str, ...] = field(default_factory=tuple)
    identity_alias_groups: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    name: str = "ticket_dispositions"

    def scan(self) -> list[ScanSignal]:
        author = self.host.current_user()
        signals: list[ScanSignal] = []
        for ticket in self._candidate_tickets():
            host = self._host_for_ticket(ticket)
            if host is None:
                continue
            try:
                issue = host.get_issue(ticket.issue_url)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to fetch issue for ticket %s (%s), skipping", ticket.pk, ticket.issue_url)
                continue
            snap = _snapshot(issue)
            if snap is None:
                continue
            signals.extend(
                ScanSignal(
                    kind="ticket.disposition_candidate",
                    summary=f"Ticket {ticket.ticket_number} — {detected.reason}",
                    payload={
                        "ticket_id": ticket.pk,
                        "ticket_number": ticket.ticket_number,
                        "ticket_state": ticket.state,
                        "issue_url": ticket.issue_url,
                        "reason": detected.reason,
                        **detected.payload_extra(),
                    },
                )
                for detected in self._detect_reasons(snap, author)
            )
        return signals

    def _host_for_ticket(self, ticket: "Ticket") -> CodeHostBackend | None:
        if self.overlay is not None:
            return get_code_host_for_url(self.overlay, ticket.issue_url)
        return self.host

    def _candidate_tickets(self) -> Iterable["Ticket"]:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.filter(state__in=_DISPOSITIONABLE_STATES).exclude(issue_url="")
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        return qs.only("id", "issue_url", "state", "overlay")

    def _detect_reasons(self, snap: _IssueSnapshot, author: str) -> list[_DispositionReason]:
        """Return every detected drift reason for *snap*.

        ``unassigned`` keeps the old/new owner identities so the statusline
        can render the transition explicitly instead of a bare
        ``reassigned``. Other reasons need no extra fields.

        The ``unassigned`` branch is suppressed when both sides of the
        reassignment fall within ``user_identity_aliases`` — a self-handoff
        between the operator's own identities is plumbing, not an
        actionable signal.
        """
        reasons: list[_DispositionReason] = []
        if snap.state in {"closed", "completed", "cancelled"}:
            reasons.append(_DispositionReason("issue_closed"))
        if (
            author
            and snap.assignees
            and author not in snap.assignees
            and not self._is_self_handoff(author, snap.assignees)
        ):
            reasons.append(_DispositionReason("unassigned", old_owner=author, new_owners=snap.assignees))
        if self.ready_labels and not any(label in snap.labels for label in self.ready_labels):
            reasons.append(_DispositionReason("label_removed"))
        return reasons

    def _is_self_handoff(self, old_owner: str, new_owners: tuple[str, ...]) -> bool:
        """True when *old_owner* and every *new_owners* entry are aliases of one human.

        Two configuration shapes feed this check. ``identity_alias_groups``
        (#1015) is the multi-human shape: each inner tuple is one human's
        aliases, and the reassignment is a self-handoff iff SOME group
        contains both ``old_owner`` and every ``new_owners`` entry —
        cross-group transitions (human A → human B) are kept.
        ``user_identity_aliases`` (legacy flat list) is treated as one
        implicit group when no explicit groups are configured.

        Empty defaults preserve the pre-#975 behaviour: every reassign renders.
        """
        groups: tuple[frozenset[str], ...]
        if self.identity_alias_groups:
            groups = tuple(frozenset(g) for g in self.identity_alias_groups if g)
        elif self.user_identity_aliases:
            groups = (frozenset(self.user_identity_aliases),)
        else:
            return False
        return any(old_owner in group and all(owner in group for owner in new_owners) for group in groups)
