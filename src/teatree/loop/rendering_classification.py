"""Classify dispatched actions into per-overlay statusline buckets.

Split out of :mod:`teatree.loop.rendering` so the classification concern
(signal → typed ref, per-overlay bucketing, cross-scanner dedup) owns one
module and the orchestrator stays a thin formatter. The renderer is the
boundary that must present each observed thing exactly once, so the dedup
lives here — every downstream line builder can assume duplicate-free input.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering_dms import DmRef as _DmRef
from teatree.loop.rendering_dms import dm_ref_from as _dm_ref_from
from teatree.loop.rendering_dms import is_dm_action as _is_dm_action
from teatree.loop.rendering_items import _IssueRef, _PRRef, _ReassignRef
from teatree.loop.statusline import StatuslineEntry

# DispatchAction payloads are `dict[str, Any]` by contract (see dispatch.py).
# Renderer-side reads only ever look up scalar fields, so we narrow to
# Mapping[str, Any] at the function signatures to keep the module health
# gate happy without inventing a TypedDict per signal shape.
type Payload = Mapping[str, Any]

# Summary prefixes the reviewer-prs scanner stamps on its signals (see
# ``scanners/reviewer_prs.py``): ``Review needed:`` for
# ``reviewer_pr.new_sha``/``unreviewed`` and ``Approval dismissed:`` for
# ``reviewer_pr.approval_dismissed``. ``_pr_ref`` keys the URL-tail iid
# derivation off these so every reviewer dual-dispatch renders as a
# clickable ``!N`` ref, while non-reviewer PR-URL signals keep their
# human-readable generic line.
_REVIEWER_SUMMARY_PREFIXES: tuple[str, ...] = ("Review needed:", "Approval dismissed:")

_TITLE_FALLBACK_LEN = 32


class ActiveTicketRow(NamedTuple):
    """One active-ticket anchor row: number, FSM state, issue URL, title, expedite flag.

    ``title`` is the cached tracker title (empty when the scanner has no title
    yet); ``expedite`` marks a release-blocker ticket the renderer flags with a
    ⚡ chip (PR-07). A :class:`NamedTuple` (not a bare 4-tuple) so adding the
    ``expedite`` field is a named, defaulted extension — existing positional
    construction stays valid and reads self-documenting at every consumer.
    """

    number: str
    state: str
    issue_url: str
    title: str
    expedite: bool = False


type IdentityAliases = tuple[tuple[str, ...], ...]


class _CanonicalIdentity:
    """Maps each of one human's forge handles to a single canonical name.

    Each group in *aliases* is one human's set of handles; the group's
    first handle is that human's canonical display name. A handle outside
    every group is its own canonical name.
    """

    def __init__(self, aliases: IdentityAliases) -> None:
        self._canonical: dict[str, str] = {}
        for group in aliases:
            if not group:
                continue
            for handle in group:
                self._canonical[handle] = group[0]

    def of(self, handle: str) -> str:
        return self._canonical.get(handle, handle)

    def is_self_handoff(self, old_owner: str, new_owners: tuple[str, ...]) -> bool:
        if not old_owner or not new_owners:
            return False
        canonical_old = self.of(old_owner)
        return all(self.of(owner) == canonical_old for owner in new_owners)


def _is_url(text: object) -> bool:
    return isinstance(text, str) and text.startswith(("http://", "https://"))


def _is_slack_user_reply(action: DispatchAction, payload: Payload) -> bool:
    """True when *action* originated from a ``slack.user_reply`` scan signal.

    The scanner emits ``summary=f"Slack user reply {ts}: {text[:80]}"`` plus
    a payload carrying ``ts``/``text``/``channel``/``user_id``. The
    statusline never surfaces these (the reactive Slack-answer loop owns
    replies), so the classifier drops them before they hit the ``c.other``
    catch-all and leak verbatim into the red zone (#1113 Defect 2).
    """
    if not action.detail.startswith("Slack user reply "):
        return False
    return isinstance(payload.get("ts"), str) and isinstance(payload.get("text"), str)


def _ticket_number_from_url(url: str) -> str:
    match = re.search(r"(\d+)(?:/?)$", url)
    return match.group(1) if match else ""


def _pr_ref(action: DispatchAction) -> _PRRef | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    url = payload.get("url", "")
    iid = payload.get("iid")
    title = _str_field(payload, "title")
    if not isinstance(iid, int) or iid == 0:
        # Reviewer-pr signals don't ship `iid` in the payload but the MR URL
        # ends with the numeric ref — derive it so the row renders as `!N`
        # under the right overlay. Match every reviewer summary form (the
        # scanner emits ``Review needed:`` for new_sha/unreviewed and
        # ``Approval dismissed:`` for approval_dismissed) without hijacking
        # other PR-URL-bearing signals (e.g. ``my_pr.open``), which keep
        # their human-readable generic line.
        if isinstance(url, str) and action.detail.startswith(_REVIEWER_SUMMARY_PREFIXES):
            tail = _ticket_number_from_url(url)
            if tail.isdigit():
                return _PRRef(iid=int(tail), url=url, title=title)
        return None
    return _PRRef(iid=iid, url=url, title=title)


@dataclass(slots=True)
class _ClassifiedActions:
    disposition_refs: dict[str, dict[str, list[_IssueRef]]] = field(default_factory=dict)
    reassign_refs: dict[str, list[_ReassignRef]] = field(default_factory=dict)
    stale_refs: dict[str, list[_IssueRef]] = field(default_factory=dict)
    ready_refs: dict[str, list[_IssueRef]] = field(default_factory=dict)
    action_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    inflight_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    # Renderer uses each :class:`ActiveTicketRow` for the canonical
    # ``#N (short desc) (!M)`` item shape (#1015), prefixed with a ⚡ chip when
    # ``expedite`` (PR-07).
    active_tickets: dict[str, list[ActiveTicketRow]] = field(default_factory=dict)
    dms: dict[str, list[_DmRef]] = field(default_factory=dict)
    other: list[tuple[str, StatuslineEntry]] = field(default_factory=list)


def _issue_ref_from(
    *,
    url: str = "",
    issue_url: str = "",
    ticket_number: str = "",
    title: str = "",
) -> _IssueRef:
    # Dispositions emit ``issue_url``; my-pr and ready emit ``url``. Honour
    # whichever the source provided so the renderer never collapses a real
    # ticket into a bare ``?`` token. Falls back to ``ticket_number`` or a
    # title slug when no usable URL is available.
    url_str = url or issue_url
    number = _ticket_number_from_url(url_str)
    if number:
        return _IssueRef(label=f"#{number}", url=url_str, title=title)
    if ticket_number:
        return _IssueRef(
            label=f"#{ticket_number}",
            url=url_str if _is_url(url_str) else "",
            title=title,
        )
    if title:
        snippet = title if len(title) <= _TITLE_FALLBACK_LEN else title[: _TITLE_FALLBACK_LEN - 3] + "…"
        return _IssueRef(label=snippet, url=url_str, title=title)
    return _IssueRef(label="?", url=url_str, title=title)


def _str_field(payload: Payload, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _str_list_field(payload: Payload, key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _active_ticket_tuple(
    *,
    ticket_number: str,
    state: str,
    payload: Payload,
) -> ActiveTicketRow:
    """Build the canonical :class:`ActiveTicketRow` from a ``ticket.active`` payload.

    #1163 refinement 4: when the scanner observed the tracker URL last
    returned a 404, drop the URL so the renderer prints a bare ``#N``
    token instead of a clickable dead permalink. Extracting this keeps
    :func:`_classify_actions` within the module's complexity budget.
    """
    issue_url = _str_field(payload, "issue_url")
    title = _str_field(payload, "title")
    if payload.get("tracker_404") is True:
        issue_url = ""
    return ActiveTicketRow(
        number=ticket_number,
        state=state,
        issue_url=issue_url,
        title=title,
        expedite=payload.get("expedite") is True,
    )


def _classify_disposition(
    c: _ClassifiedActions,
    overlay: str,
    payload: Payload,
    reason: str,
    identity: _CanonicalIdentity,
) -> None:
    """Route a ``reason``-bearing action into reassign or generic disposition buckets."""
    ref = _issue_ref_from(
        url=_str_field(payload, "url"),
        issue_url=_str_field(payload, "issue_url"),
        ticket_number=_str_field(payload, "ticket_number"),
        title=_str_field(payload, "title"),
    )
    old_owner = _str_field(payload, "old_owner")
    new_owners = _str_list_field(payload, "new_owners")
    if reason == "unassigned" and old_owner and new_owners:
        if identity.is_self_handoff(old_owner, new_owners):
            return
        c.reassign_refs.setdefault(overlay, []).append(
            _ReassignRef(
                ref=ref,
                old_owner=identity.of(old_owner),
                new_owners=tuple(identity.of(owner) for owner in new_owners),
            ),
        )
        return
    c.disposition_refs.setdefault(overlay, {}).setdefault(reason, []).append(ref)


def _is_orphaned_task(payload: Payload) -> bool:
    """True when *payload* belongs to a ``task.orphaned`` operator-review advisory.

    The scanner emits one signal per unverifiable task; without collapse,
    N orphaned tasks produce N identical-shaped rows in ``action_needed``
    and flood the statusline. The classifier intercepts them and collapses
    the whole batch to one summary line (``N tasks need operator review``).
    The ScanSignals themselves are preserved for non-statusline consumers;
    only the rendered view collapses.
    """
    return isinstance(payload.get("task_id"), int)


def _emit_orphaned_summaries(
    c: _ClassifiedActions,
    orphaned_counts: dict[str, int],
) -> None:
    """Append one collapsed ``N tasks need operator review`` entry per overlay."""
    for overlay, count in sorted(orphaned_counts.items()):
        prefix = f"[{overlay}] " if overlay else ""
        label = "task needs" if count == 1 else "tasks need"
        c.other.append(("action_needed", StatuslineEntry(text=f"{prefix}{count} {label} operator review")))


def _is_pending_task(payload: Payload) -> bool:
    """True when *payload* belongs to a ``pending_task`` statusline fallback.

    The ``PendingTasksScanner`` emits one ``pending_task`` per row; a phase
    with no registered sub-agent falls through ``dispatch._dispatch_one``
    to the ``in_flight`` statusline zone. Without collapse the renderer
    prints one ``Task <id> (<phase>) <status>`` line per row — ~50 lines
    that waste vertical space and leak the raw phase token (``short_describe``
    / ``dogfood_smoke`` / ``architectural_review``) where a description
    would go. The classifier intercepts these and collapses each overlay's
    rows to one ``teatree tasks: <status>: N`` line, grouped by status.
    """
    return isinstance(payload.get("task_id"), int) and isinstance(payload.get("phase"), str)


def _emit_pending_task_summaries(
    c: _ClassifiedActions,
    status_counts: dict[str, dict[str, int]],
) -> None:
    """Append one ``teatree tasks: <status>: N · …`` line per overlay (grouped by status).

    No individual task id or phase is listed — the raw phase token never
    surfaces as a pseudo-description, and the ~50-row dump collapses to a
    single compact line per overlay. The label is ``teatree tasks`` (the loop's
    claimable task queue), distinct from the session's harness TODO list.
    """
    for overlay, by_status in sorted(status_counts.items()):
        prefix = f"[{overlay}] " if overlay else ""
        parts = [f"{status}: {count}" for status, count in sorted(by_status.items())]
        c.other.append(("in_flight", StatuslineEntry(text=f"{prefix}teatree tasks: {' · '.join(parts)}")))


def _classify_one(c: _ClassifiedActions, action: DispatchAction, identity: _CanonicalIdentity) -> None:
    """Route one statusline action into the right classified bucket.

    Pre-condition: ``action.kind == "statusline"``, not a slack user reply,
    and not a ``task.orphaned`` advisory (both filtered by the caller).
    """
    payload = action.payload if isinstance(action.payload, dict) else {}
    url_str = _str_field(payload, "url")
    overlay = _str_field(payload, "overlay")
    prefix = f"[{overlay}] " if overlay else ""

    state = payload.get("state")
    ticket_number = payload.get("ticket_number")
    if action.zone == "anchors" and isinstance(state, str) and isinstance(ticket_number, str):
        c.active_tickets.setdefault(overlay, []).append(
            _active_ticket_tuple(ticket_number=ticket_number, state=state, payload=payload),
        )
        return
    if payload.get("stale") is True:
        c.stale_refs.setdefault(overlay, []).append(
            _issue_ref_from(
                issue_url=_str_field(payload, "issue_url"),
                ticket_number=_str_field(payload, "ticket_number"),
            ),
        )
        return
    reason = payload.get("reason")
    if isinstance(reason, str):
        _classify_disposition(c, overlay, payload, reason, identity)
        return
    if action.zone == "action_needed" and action.detail.startswith("Ready to start:"):
        c.ready_refs.setdefault(overlay, []).append(
            _issue_ref_from(
                url=_str_field(payload, "url"),
                issue_url=_str_field(payload, "issue_url"),
                ticket_number=_str_field(payload, "ticket_number"),
                title=_str_field(payload, "title"),
            ),
        )
        return
    if _is_dm_action(action, payload):
        c.dms.setdefault(overlay, []).append(_dm_ref_from(payload))
        return
    ref = _pr_ref(action)
    if ref is not None:
        bucket = c.action_prs if action.zone == "action_needed" else c.inflight_prs
        bucket.setdefault(overlay, []).append(ref)
        return
    c.other.append((action.zone, StatuslineEntry(text=f"{prefix}{action.detail}", url=url_str)))


def _classify_actions(actions: list[DispatchAction], identity_aliases: IdentityAliases = ()) -> _ClassifiedActions:
    c = _ClassifiedActions()
    identity = _CanonicalIdentity(identity_aliases)
    orphaned_counts: dict[str, int] = {}
    pending_task_counts: dict[str, dict[str, int]] = {}
    for action in actions:
        if action.kind != "statusline":
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        overlay = _str_field(payload, "overlay")
        if _is_slack_user_reply(action, payload):
            # #1113 Defect 2 defense-in-depth: raw Slack reply text + ts must
            # never reach ``c.other`` and render verbatim, even if the
            # dispatcher regresses. The reply is owned by the reactive
            # Slack-answer loop (``teatree.loop.slack_answer``); the
            # statusline never surfaces it.
            continue
        if action.zone == "action_needed" and _is_orphaned_task(payload):
            orphaned_counts[overlay] = orphaned_counts.get(overlay, 0) + 1
            continue
        if _is_pending_task(payload):
            status = _str_field(payload, "status") or "pending"
            by_status = pending_task_counts.setdefault(overlay, {})
            by_status[status] = by_status.get(status, 0) + 1
            continue
        _classify_one(c, action, identity)
    _emit_orphaned_summaries(c, orphaned_counts)
    _emit_pending_task_summaries(c, pending_task_counts)
    _dedup_classified(c)
    return c


def _dedup_in_order[T](items: list[T]) -> list[T]:
    """Return *items* with duplicates dropped, first-occurrence wins.

    Statusline rows must be stable across ticks: two scanners surfacing
    the same observation (same ``ticket.active`` row, same PR seen by both
    ``MyPrsScanner`` and ``ReviewerPrsScanner``, same ``ticket.stale`` row
    emitted again on the next sweep) must collapse to one ref. Order is
    preserved so the user sees a deterministic line.
    """
    seen: set[T] = set()
    out: list[T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _dedup_reassign_by_ticket(refs: list[_ReassignRef]) -> list[_ReassignRef]:
    """Collapse reassign rows for the same ticket, first-occurrence wins.

    ``_dedup_in_order`` keys on full ``_ReassignRef`` equality, so the same
    ticket reassigned from two distinct source handles survives twice. A
    ticket is one observable thing regardless of source handle — key on its
    identity (issue URL, else label).
    """
    seen: set[str] = set()
    out: list[_ReassignRef] = []
    for ref in refs:
        key = ref.ref.url or ref.ref.label
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _dedup_active_tickets_across_overlays(
    by_overlay: dict[str, list[ActiveTicketRow]],
) -> dict[str, list[ActiveTicketRow]]:
    """Drop duplicate ticket rows that surface under more than one overlay.

    A single underlying tracker row (same ``issue_url``) can be claimed by
    multiple overlays both watching the same upstream issue; without
    cross-overlay dedup the statusline shows the same ``#N`` row twice —
    once per overlay prefix — which is exactly the visual repetition the
    user pinned in #1163.

    First-occurrence-wins ordering: the earlier overlay (by sorted key)
    keeps the row, later overlays drop it. ``issue_url == ""`` is treated
    as unkeyable and never dedup'd — a missing URL (404 or never-set) is
    not a reliable identity, so we err on the side of showing the row.
    """
    seen_urls: set[str] = set()
    out: dict[str, list[ActiveTicketRow]] = {}
    for overlay in sorted(by_overlay):
        kept: list[ActiveTicketRow] = []
        for ticket in by_overlay[overlay]:
            issue_url = ticket.issue_url
            if issue_url and issue_url in seen_urls:
                continue
            if issue_url:
                seen_urls.add(issue_url)
            kept.append(ticket)
        out[overlay] = kept
    return out


def _drop_stale_already_on_active_line(
    stale_by_overlay: dict[str, list[_IssueRef]],
    active_by_overlay: dict[str, list[ActiveTicketRow]],
) -> dict[str, list[_IssueRef]]:
    """Drop a stale ref when its ticket number already renders on the active line.

    Every ticket in :data:`teatree.loop.scanners.stale_tickets._STALE_CANDIDATE_STATES`
    is *also* an active ticket (#1324). Without this filter the renderer
    shows the same ``#N`` on the dim anchor line AND on the red ``N stale:``
    row — pure visual duplication.
    """
    out: dict[str, list[_IssueRef]] = {}
    for overlay, refs in stale_by_overlay.items():
        on_active = {row.number for row in active_by_overlay.get(overlay, [])}
        out[overlay] = [r for r in refs if r.label.lstrip("#") not in on_active]
    return out


def _dedup_classified(c: _ClassifiedActions) -> None:
    """Collapse duplicate refs in every per-overlay bucket.

    The dispatch layer is fan-out by signal (one signal per scanner that
    saw it); the renderer is the boundary that must present each observed
    thing exactly once. Doing the dedup here keeps every downstream
    renderer simple — they can assume the input list has no duplicates.
    """
    for overlay, tickets in list(c.active_tickets.items()):
        c.active_tickets[overlay] = _dedup_in_order(tickets)
    # #1163 refinement 1: cross-overlay dedup on issue_url. The earlier
    # per-overlay pass collapses within-overlay duplicates; this pass
    # collapses across overlays so the same tracker row never surfaces N
    # times when N overlays watch it.
    c.active_tickets = _dedup_active_tickets_across_overlays(c.active_tickets)
    for overlay, refs in list(c.stale_refs.items()):
        c.stale_refs[overlay] = _dedup_in_order(refs)
    # #1324: drop stale refs whose ticket number already appears on the
    # active anchor line for the same overlay. Stale is informational
    # about an active ticket — surface it once.
    c.stale_refs = _drop_stale_already_on_active_line(c.stale_refs, c.active_tickets)
    for overlay, refs in list(c.ready_refs.items()):
        c.ready_refs[overlay] = _dedup_in_order(refs)
    for overlay, refs in list(c.action_prs.items()):
        c.action_prs[overlay] = _dedup_in_order(refs)
    for overlay, refs in list(c.inflight_prs.items()):
        c.inflight_prs[overlay] = _dedup_in_order(refs)
    for overlay, reass in list(c.reassign_refs.items()):
        c.reassign_refs[overlay] = _dedup_reassign_by_ticket(_dedup_in_order(reass))
    for overlay, dms in list(c.dms.items()):
        c.dms[overlay] = _dedup_in_order(dms)
    for reason_map in c.disposition_refs.values():
        for reason, refs in list(reason_map.items()):
            reason_map[reason] = _dedup_in_order(refs)
