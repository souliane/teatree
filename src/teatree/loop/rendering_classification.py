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
from typing import Any

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
                return _PRRef(iid=int(tail), url=url, annotation="review")
        return None
    draft_count = payload.get("draft_count")
    status = payload.get("status", "")
    if isinstance(draft_count, int) and draft_count > 0:
        return _PRRef(iid=iid, url=url, annotation=f"{draft_count} notes")
    if status in {"failed", "failure", "error"}:
        return _PRRef(iid=iid, url=url, annotation=f"pipeline {status}")
    return _PRRef(iid=iid, url=url, annotation="")


@dataclass(slots=True)
class _ClassifiedActions:
    disposition_refs: dict[str, dict[str, list[_IssueRef]]] = field(default_factory=dict)
    reassign_refs: dict[str, list[_ReassignRef]] = field(default_factory=dict)
    stale_refs: dict[str, list[_IssueRef]] = field(default_factory=dict)
    ready_refs: dict[str, list[_IssueRef]] = field(default_factory=dict)
    action_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    inflight_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    # ``(ticket_number, state, issue_url, title)`` — ``title`` is the cached
    # tracker title (``ticket.extra["issue_title"]``); empty when the
    # scanner has no title yet. Renderer uses it for the canonical
    # ``#N (short desc) (!M)`` item shape (#1015).
    active_tickets: dict[str, list[tuple[str, str, str, str]]] = field(default_factory=dict)
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


def _classify_disposition(c: _ClassifiedActions, overlay: str, payload: Payload, reason: str) -> None:
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
        c.reassign_refs.setdefault(overlay, []).append(
            _ReassignRef(ref=ref, old_owner=old_owner, new_owners=new_owners),
        )
        return
    c.disposition_refs.setdefault(overlay, {}).setdefault(reason, []).append(ref)


def _classify_actions(actions: list[DispatchAction]) -> _ClassifiedActions:
    c = _ClassifiedActions()
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        url_str = _str_field(payload, "url")
        overlay = _str_field(payload, "overlay")
        prefix = f"[{overlay}] " if overlay else ""

        if action.kind != "statusline":
            continue
        state = payload.get("state")
        ticket_number = payload.get("ticket_number")
        if action.zone == "anchors" and isinstance(state, str) and isinstance(ticket_number, str):
            issue_url = _str_field(payload, "issue_url")
            title = _str_field(payload, "title")
            c.active_tickets.setdefault(overlay, []).append((ticket_number, state, issue_url, title))
            continue
        if payload.get("stale") is True:
            c.stale_refs.setdefault(overlay, []).append(
                _issue_ref_from(
                    issue_url=_str_field(payload, "issue_url"),
                    ticket_number=_str_field(payload, "ticket_number"),
                ),
            )
            continue
        reason = payload.get("reason")
        if isinstance(reason, str):
            _classify_disposition(c, overlay, payload, reason)
            continue
        if action.zone == "action_needed" and action.detail.startswith("Ready to start:"):
            c.ready_refs.setdefault(overlay, []).append(
                _issue_ref_from(
                    url=_str_field(payload, "url"),
                    issue_url=_str_field(payload, "issue_url"),
                    ticket_number=_str_field(payload, "ticket_number"),
                    title=_str_field(payload, "title"),
                ),
            )
            continue
        if _is_dm_action(action, payload):
            c.dms.setdefault(overlay, []).append(_dm_ref_from(payload))
            continue
        if _is_slack_user_reply(action, payload):
            # #1113 Defect 2 defense-in-depth: raw Slack reply text + ts must
            # never reach ``c.other`` and render verbatim, even if the
            # dispatcher regresses. The reply is owned by the reactive
            # Slack-answer loop (``teatree.loop.slack_answer``); the
            # statusline never surfaces it.
            continue
        ref = _pr_ref(action)
        if ref is not None:
            bucket = c.action_prs if action.zone == "action_needed" else c.inflight_prs
            bucket.setdefault(overlay, []).append(ref)
            continue
        c.other.append((action.zone, StatuslineEntry(text=f"{prefix}{action.detail}", url=url_str)))
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


def _dedup_classified(c: _ClassifiedActions) -> None:
    """Collapse duplicate refs in every per-overlay bucket.

    The dispatch layer is fan-out by signal (one signal per scanner that
    saw it); the renderer is the boundary that must present each observed
    thing exactly once. Doing the dedup here keeps every downstream
    renderer simple — they can assume the input list has no duplicates.
    """
    for overlay, tickets in list(c.active_tickets.items()):
        c.active_tickets[overlay] = _dedup_in_order(tickets)
    for overlay, refs in list(c.stale_refs.items()):
        c.stale_refs[overlay] = _dedup_in_order(refs)
    for overlay, refs in list(c.ready_refs.items()):
        c.ready_refs[overlay] = _dedup_in_order(refs)
    for overlay, refs in list(c.action_prs.items()):
        c.action_prs[overlay] = _dedup_in_order(refs)
    for overlay, refs in list(c.inflight_prs.items()):
        c.inflight_prs[overlay] = _dedup_in_order(refs)
    for overlay, reass in list(c.reassign_refs.items()):
        c.reassign_refs[overlay] = _dedup_in_order(reass)
    for overlay, dms in list(c.dms.items()):
        c.dms[overlay] = _dedup_in_order(dms)
    for reason_map in c.disposition_refs.values():
        for reason, refs in list(reason_map.items()):
            reason_map[reason] = _dedup_in_order(refs)
