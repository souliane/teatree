"""Render dispatched actions into statusline zones.

Split out of :mod:`teatree.loop.tick` so each module owns one concern: tick is
the orchestrator (scan → dispatch → render), rendering is the formatter
(classify actions per overlay, render anchor / action_needed / in_flight rows
with OSC8 links, filter stale anchors).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from teatree.loop.dispatch import DispatchAction
from teatree.loop.pr_ticket_index import build_ticket_index
from teatree.loop.statusline import StatuslineEntry, StatuslineZones, _hyperlink

# DispatchAction payloads are `dict[str, Any]` by contract (see dispatch.py).
# Renderer-side reads only ever look up scalar fields, so we narrow to
# Mapping[str, Any] at the function signatures to keep the module health
# gate happy without inventing a TypedDict per signal shape.
type Payload = Mapping[str, Any]

_DISPOSITION_LABELS: dict[str, str] = {
    "issue_closed": "closed",
    "unassigned": "reassigned",
    "label_removed": "label-removed",
}


def _is_url(text: object) -> bool:
    return isinstance(text, str) and text.startswith(("http://", "https://"))


def _link(text: str, url: object) -> str:
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return _hyperlink(text, url)
    return text


@dataclass(frozen=True, slots=True)
class _PRRef:
    iid: int
    url: str
    annotation: str


@dataclass(frozen=True, slots=True)
class _IssueRef:
    label: str
    url: str


def _pr_ref(action: DispatchAction) -> _PRRef | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    url = payload.get("url", "")
    iid = payload.get("iid")
    if not isinstance(iid, int) or iid == 0:
        # Reviewer-pr signals don't ship `iid` in the payload but the MR URL
        # ends with the numeric ref — derive it so the row renders as `!N`
        # under the right overlay. Other signal kinds still fall through
        # without an iid (rendered as a generic line).
        if action.detail.startswith("Review needed:") and isinstance(url, str):
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


def _ticket_number_from_url(url: str) -> str:
    match = re.search(r"(\d+)(?:/?)$", url)
    return match.group(1) if match else ""


def _render_pr_group(overlay: str, refs: list[_PRRef], *, ticket_index: dict[str, str] | None = None) -> str:
    """Render a flat list of PR refs, grouped per parent ticket when known."""
    prefix = f"[{overlay}] " if overlay else ""
    if not refs:
        return ""
    by_ticket: dict[str, list[_PRRef]] = {}
    orphans: list[_PRRef] = []
    for ref in refs:
        parent = (ticket_index or {}).get(ref.url, "")
        if parent and parent != str(ref.iid):
            by_ticket.setdefault(parent, []).append(ref)
        else:
            orphans.append(ref)

    def _label(ref: _PRRef) -> str:
        text = f"!{ref.iid}"
        if ref.annotation:
            text += f" ({ref.annotation})"
        return _link(text, ref.url)

    chunks: list[str] = []
    for tnum in sorted(by_ticket):
        bucket = " ".join(_label(r) for r in by_ticket[tnum])
        chunks.append(f"#{tnum}: {bucket}")
    if orphans:
        chunks.append(" · ".join(_label(r) for r in orphans))
    return f"{prefix}{' · '.join(chunks)}"


@dataclass(slots=True)
class _ClassifiedActions:
    disposition_refs: dict[str, dict[str, list[_IssueRef]]] = field(default_factory=dict)
    ready_refs: dict[str, list[_IssueRef]] = field(default_factory=dict)
    action_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    inflight_prs: dict[str, list[_PRRef]] = field(default_factory=dict)
    active_tickets: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    other: list[tuple[str, StatuslineEntry]] = field(default_factory=list)


_TITLE_FALLBACK_LEN = 32


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
        return _IssueRef(label=f"#{number}", url=url_str)
    if ticket_number:
        return _IssueRef(label=f"#{ticket_number}", url=url_str if _is_url(url_str) else "")
    if title:
        snippet = title if len(title) <= _TITLE_FALLBACK_LEN else title[: _TITLE_FALLBACK_LEN - 3] + "…"
        return _IssueRef(label=snippet, url=url_str)
    return _IssueRef(label="?", url=url_str)


def _str_field(payload: Payload, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


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
            c.active_tickets.setdefault(overlay, []).append((ticket_number, state, issue_url))
            continue
        reason = payload.get("reason")
        if isinstance(reason, str):
            ref = _issue_ref_from(
                url=_str_field(payload, "url"),
                issue_url=_str_field(payload, "issue_url"),
                ticket_number=_str_field(payload, "ticket_number"),
                title=_str_field(payload, "title"),
            )
            c.disposition_refs.setdefault(overlay, {}).setdefault(reason, []).append(ref)
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
        ref = _pr_ref(action)
        if ref is not None:
            bucket = c.action_prs if action.zone == "action_needed" else c.inflight_prs
            bucket.setdefault(overlay, []).append(ref)
            continue
        c.other.append((action.zone, StatuslineEntry(text=f"{prefix}{action.detail}", url=url_str)))
    return c


# States that should not surface as anchors: terminal/post-PR states plus
# ``closed`` which isn't a valid FSM value but turns up in old data.
_NOISE_STATES = frozenset(
    {
        "not_started",
        "merged",
        "delivered",
        "shipped",
        "in_review",
        "retrospected",
        "closed",
    },
)
_MAX_PER_STATE = 5

# Tickets whose ``issue_url`` matches one of these are treated as PR-backed.
# A PR-backed ticket that doesn't appear in the live PR set (action_needed
# or in_flight) is considered stale — its remote MR has likely been merged
# or closed but the local FSM never advanced.
_PR_URL_RE = re.compile(r"/(?:merge_requests|pull|pulls)/\d+/?$")


def _is_pr_url(url: str) -> bool:
    return bool(_is_url(url) and _PR_URL_RE.search(url))


def _render_ticket_line(
    overlay: str,
    tickets: list[tuple[str, str, str]],
    pr_map: dict[str, list[_PRRef]],
    *,
    live_pr_urls: set[str] | None = None,
) -> str:
    prefix = f"[{overlay}] " if overlay else ""
    live = live_pr_urls or set()
    by_state: dict[str, list[str]] = {}
    for num, state, url in tickets:
        if state in _NOISE_STATES:
            continue
        if _is_pr_url(url) and url not in live:
            continue
        ticket_text = _link(f"#{num}", url)
        prs = pr_map.get(num, [])
        if prs:
            pr_parts = [_link(f"!{r.iid}", r.url) for r in prs]
            ticket_text += f" ({' '.join(pr_parts)})"
        by_state.setdefault(state, []).append(ticket_text)
    if not by_state:
        return ""
    groups: list[str] = []
    for state, items in by_state.items():
        shown = items[:_MAX_PER_STATE]
        overflow = len(items) - len(shown)
        label = " ".join(shown)
        if overflow > 0:
            label += f" (+{overflow})"
        groups.append(f"{state}: {label}")
    return f"{prefix}{' · '.join(groups)}"


def _render_action_line(
    overlay: str,
    *,
    pr_refs: list[_PRRef],
    disposition_refs: dict[str, list[_IssueRef]],
    ready_refs: list[_IssueRef],
    ticket_index: dict[str, str] | None = None,
) -> str:
    prefix = f"[{overlay}] " if overlay else ""
    parts: list[str] = []
    if pr_refs:
        parts.append(_render_pr_group(overlay, pr_refs, ticket_index=ticket_index).removeprefix(prefix))
    for reason, refs in disposition_refs.items():
        label = _DISPOSITION_LABELS.get(reason, reason)
        items = " ".join(_link(r.label, r.url) for r in refs)
        parts.append(f"{label}: {items}")
    if ready_refs:
        items = " ".join(_link(r.label, r.url) for r in ready_refs)
        parts.append(f"ready: {items}")
    if not parts:
        return ""
    return f"{prefix}{' · '.join(parts)}"


def _running_tasks_lines() -> list[str]:
    from django.apps import apps  # noqa: PLC0415

    try:
        task_model = apps.get_model("core", "Task")
        claimed = (
            task_model.objects.filter(status="claimed")
            .select_related("ticket")
            .only("phase", "ticket__overlay", "ticket__issue_url")
        )
        by_overlay: dict[str, list[str]] = {}
        for task in claimed:
            overlay = task.ticket.overlay or ""
            by_overlay.setdefault(overlay, []).append(task.phase)
    except Exception:  # noqa: BLE001
        return []
    lines: list[str] = []
    for overlay, phases in sorted(by_overlay.items()):
        prefix = f"[{overlay}] " if overlay else ""
        lines.append(f"{prefix}agents: {' · '.join(phases)}")
    return lines


def _populate_overlay_zones(
    zones: StatuslineZones,
    c: _ClassifiedActions,
    *,
    ticket_index: dict[str, str],
) -> None:
    all_overlays = sorted({*c.active_tickets, *c.action_prs, *c.disposition_refs, *c.ready_refs, *c.inflight_prs})

    all_pr_refs: dict[str, dict[str, list[_PRRef]]] = {}
    for overlay_key in all_overlays:
        for refs in (c.action_prs.get(overlay_key, []), c.inflight_prs.get(overlay_key, [])):
            for ref in refs:
                all_pr_refs.setdefault(overlay_key, {}).setdefault(str(ref.iid), []).append(ref)

    live_pr_urls_by_overlay: dict[str, set[str]] = {}
    for overlay_key in all_overlays:
        live = {r.url for r in c.action_prs.get(overlay_key, []) if r.url}
        live |= {r.url for r in c.inflight_prs.get(overlay_key, []) if r.url}
        live_pr_urls_by_overlay[overlay_key] = live

    for overlay_key in all_overlays:
        pr_map = all_pr_refs.get(overlay_key, {})
        ticket_line = _render_ticket_line(
            overlay_key,
            c.active_tickets.get(overlay_key, []),
            pr_map,
            live_pr_urls=live_pr_urls_by_overlay.get(overlay_key, set()),
        )
        if ticket_line:
            zones.anchors.append(ticket_line)

        action_line = _render_action_line(
            overlay_key,
            pr_refs=c.action_prs.get(overlay_key, []),
            disposition_refs=c.disposition_refs.get(overlay_key, {}),
            ready_refs=c.ready_refs.get(overlay_key, []),
            ticket_index=ticket_index,
        )
        if action_line:
            zones.action_needed.append(action_line)

        inflight_refs = c.inflight_prs.get(overlay_key, [])
        if inflight_refs:
            zones.in_flight.append(_render_pr_group(overlay_key, inflight_refs, ticket_index=ticket_index))


def zones_for(actions: list[DispatchAction]) -> StatuslineZones:
    zones = StatuslineZones()
    c = _classify_actions(actions)
    ticket_index = build_ticket_index(actions)
    _populate_overlay_zones(zones, c, ticket_index=ticket_index)

    for zone_name, entry in c.other:
        zone_list = getattr(zones, zone_name, None)
        if isinstance(zone_list, list):
            zone_list.append(entry)

    zones.in_flight.extend(_running_tasks_lines())

    return zones
