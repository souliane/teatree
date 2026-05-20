"""Render classified actions into per-overlay statusline zone lines.

Split out of :mod:`teatree.loop.rendering` so the line-builder concern
(anchor / action_needed / in_flight rows, OSC8 links, stale-anchor
filtering, per-overlay zone population) owns one module. Consumes the
duplicate-free :class:`_ClassifiedActions` produced by
:mod:`teatree.loop.rendering_classification`.
"""

import re

from teatree.loop.rendering_classification import _ClassifiedActions, _is_url
from teatree.loop.rendering_dms import render_dm_line as _render_dm_line
from teatree.loop.rendering_items import _LinkCtx, _OverlayActionRefs, _PRRef, _render_canonical_item
from teatree.loop.statusline import StatuslineZones, _hyperlink, plain_link

_DISPOSITION_LABELS: dict[str, str] = {
    "issue_closed": "closed",
    "unassigned": "reassigned",
    "label_removed": "label-removed",
}

# States that should not surface as anchors: only TERMINAL post-PR states.
# Rich work states (``not_started``, ``in_review``) intentionally surface so
# the statusline shows the real lifecycle, not just the started/tested/ready
# slice (#1163).  ``closed`` isn't a valid FSM value but turns up in old data.
_NOISE_STATES = frozenset(
    {
        "merged",
        "delivered",
        "shipped",
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


def _link(text: str, url: object, *, colorize: bool) -> str:
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return _hyperlink(text, url) if colorize else plain_link(text, url)
    return text


def _is_pr_url(url: str) -> bool:
    return bool(_is_url(url) and _PR_URL_RE.search(url))


def _render_pr_group(
    overlay: str,
    refs: list[_PRRef],
    *,
    ticket_index: dict[str, str] | None = None,
    colorize: bool,
) -> str:
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
        rendered = _link(text, ref.url, colorize=colorize)
        # #1113 enhancement: surface the review-channel post permalink so the
        # operator can jump from the statusline straight to the thread.
        if ref.review_permalink:
            rendered += " " + _link("(review)", ref.review_permalink, colorize=colorize)
        return rendered

    chunks: list[str] = []
    for tnum in sorted(by_ticket):
        bucket = " ".join(_label(r) for r in by_ticket[tnum])
        chunks.append(f"#{tnum}: {bucket}")
    if orphans:
        chunks.append(" · ".join(_label(r) for r in orphans))
    return f"{prefix}{' · '.join(chunks)}"


def _render_ticket_line(
    overlay: str,
    tickets: list[tuple[str, str, str, str]],
    pr_map: dict[str, list[_PRRef]],
    *,
    live_pr_urls: set[str] | None = None,
    colorize: bool,
) -> str:
    """Render the per-overlay anchor line — one row per state group.

    Every state line (``ready:``, ``started:``, ``tested:``, …) uses the
    same canonical item shape: ``#N (short desc) (!M1, !M2)`` where the
    description is the cached tracker title truncated to
    ``_ITEM_DESC_LEN`` and the MR refs are comma-separated and clickable
    (#1015). The PR group falls back to a space-separated form when no
    description is present, but in the canonical path every number is a
    hyperlink.

    ``pr_map`` is the overlay's MR-iid → child-refs map; entries appear
    here either because the MR's iid equals the ticket number (legacy
    shape), or because the caller pre-bucketed by parent ticket number
    via ``ticket_index`` (canonical shape).
    """
    prefix = f"[{overlay}] " if overlay else ""
    live = live_pr_urls or set()
    by_state: dict[str, list[str]] = {}
    for num, state, url, title in tickets:
        if state in _NOISE_STATES:
            continue
        if _is_pr_url(url) and url not in live:
            continue
        by_state.setdefault(state, []).append(
            _render_canonical_item(
                label=f"#{num}",
                url=url,
                title=title,
                child_refs=pr_map.get(num, []),
                ctx=_LinkCtx(colorize=colorize, link=_link),
            ),
        )
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


def _disposition_parts(action_refs: _OverlayActionRefs, *, colorize: bool) -> list[str]:
    """Render the issue-disposition rows for one overlay.

    Covers generic dispositions, the explicit ``reassigned (from → to)``
    transition, and the collapsed ``N stale`` row. Each is one concise part
    with linked refs — the stale row in particular folds every stale ticket
    for the overlay into a single line.
    """
    parts: list[str] = []
    for reason, refs in action_refs.disposition_refs.items():
        label = _DISPOSITION_LABELS.get(reason, reason)
        items = " ".join(_link(r.label, r.url, colorize=colorize) for r in refs)
        parts.append(f"{label}: {items}")
    for rr in action_refs.reassign_refs:
        to = ", ".join(rr.new_owners)
        ref_link = _link(rr.ref.label, rr.ref.url, colorize=colorize)
        parts.append(f"reassigned (from {rr.old_owner} → to {to}): {ref_link}")
    if action_refs.stale_refs:
        stale = action_refs.stale_refs
        items = " ".join(_link(r.label, r.url, colorize=colorize) for r in stale)
        parts.append(f"{len(stale)} stale: {items}")
    return parts


def _render_action_line(
    overlay: str,
    action_refs: _OverlayActionRefs,
    *,
    ticket_index: dict[str, str] | None = None,
    colorize: bool,
) -> str:
    prefix = f"[{overlay}] " if overlay else ""
    prs_by_ticket: dict[str, list[_PRRef]] = {}
    for ref in action_refs.pr_refs:
        parent = (ticket_index or {}).get(ref.url, "")
        if parent and parent != str(ref.iid):
            prs_by_ticket.setdefault(parent, []).append(ref)
    consumed_pr_urls: set[str] = set()

    parts: list[str] = _disposition_parts(action_refs, colorize=colorize)
    if action_refs.ready_refs:
        items: list[str] = []
        for ref in action_refs.ready_refs:
            number = ref.label.lstrip("#")
            prs = prs_by_ticket.get(number, [])
            items.append(
                _render_canonical_item(
                    label=ref.label,
                    url=ref.url,
                    title=ref.title,
                    child_refs=prs,
                    ctx=_LinkCtx(colorize=colorize, link=_link),
                ),
            )
            consumed_pr_urls.update(p.url for p in prs)
        parts.append(f"ready: {' '.join(items)}")
    if action_refs.pr_refs:
        remaining = [r for r in action_refs.pr_refs if r.url not in consumed_pr_urls]
        if remaining:
            parts.insert(
                0,
                _render_pr_group(overlay, remaining, ticket_index=ticket_index, colorize=colorize).removeprefix(prefix),
            )
    if not parts:
        return ""
    return f"{prefix}{' · '.join(parts)}"


def _running_tasks_lines() -> list[str]:
    """Render the ``[ov] agents: <phase> · <phase>`` row from CLAIMED tasks.

    Skips tasks with a blank ``phase`` (the column is ``blank=True`` and
    the default is ``""``) so the renderer never produces a phantom
    ``agents:  · coding`` with a double space the operator can't act on.
    Dedupes phase names per overlay: two parallel implementers both at
    ``coding`` collapse to one ``agents: coding`` ref — concurrency is
    implied by the loop, not by visually repeating the same word.
    Suppresses the whole row when no claimed task contributes a phase.
    """
    from django.apps import apps  # noqa: PLC0415

    from teatree.loop.rendering_classification import _dedup_in_order  # noqa: PLC0415

    try:
        task_model = apps.get_model("core", "Task")
        claimed = (
            task_model.objects.filter(status="claimed")
            .select_related("ticket")
            .only("phase", "ticket__overlay", "ticket__issue_url")
        )
        by_overlay: dict[str, list[str]] = {}
        for task in claimed:
            phase = (task.phase or "").strip()
            if not phase:
                continue
            overlay = task.ticket.overlay or ""
            by_overlay.setdefault(overlay, []).append(phase)
    except Exception:  # noqa: BLE001
        return []
    lines: list[str] = []
    for overlay, phases in sorted(by_overlay.items()):
        unique_phases = _dedup_in_order(phases)
        if not unique_phases:
            continue
        prefix = f"[{overlay}] " if overlay else ""
        lines.append(f"{prefix}agents: {' · '.join(unique_phases)}")
    return lines


def _populate_overlay_zones(
    zones: StatuslineZones,
    c: _ClassifiedActions,
    *,
    ticket_index: dict[str, str],
    colorize: bool,
) -> None:
    all_overlays = sorted(
        {
            *c.active_tickets,
            *c.action_prs,
            *c.disposition_refs,
            *c.reassign_refs,
            *c.stale_refs,
            *c.ready_refs,
            *c.inflight_prs,
            *c.dms,
        },
    )

    # ``pr_map`` keys are ticket numbers: each MR is bucketed under its
    # parent ticket (resolved via ``ticket_index`` — ``Closes #N`` footer
    # parsing in ``pr_ticket_index``). Legacy fall-through: when no parent
    # is known, the MR is still keyed by its own iid so a ticket-number ==
    # iid coincidence keeps rendering.
    all_pr_refs: dict[str, dict[str, list[_PRRef]]] = {}
    for overlay_key in all_overlays:
        for refs in (c.action_prs.get(overlay_key, []), c.inflight_prs.get(overlay_key, [])):
            for ref in refs:
                parent = ticket_index.get(ref.url, "")
                key = parent if parent and parent != str(ref.iid) else str(ref.iid)
                all_pr_refs.setdefault(overlay_key, {}).setdefault(key, []).append(ref)

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
            colorize=colorize,
        )
        if ticket_line:
            zones.anchors.append(ticket_line)

        dm_line = _render_dm_line(overlay_key, c.dms.get(overlay_key, []), link=_link, colorize=colorize)
        if dm_line:
            zones.anchors.append(dm_line)

        action_line = _render_action_line(
            overlay_key,
            _OverlayActionRefs(
                pr_refs=c.action_prs.get(overlay_key, []),
                disposition_refs=c.disposition_refs.get(overlay_key, {}),
                ready_refs=c.ready_refs.get(overlay_key, []),
                reassign_refs=c.reassign_refs.get(overlay_key, []),
                stale_refs=c.stale_refs.get(overlay_key, []),
            ),
            ticket_index=ticket_index,
            colorize=colorize,
        )
        if action_line:
            zones.action_needed.append(action_line)

        inflight_refs = c.inflight_prs.get(overlay_key, [])
        if inflight_refs:
            zones.in_flight.append(
                _render_pr_group(overlay_key, inflight_refs, ticket_index=ticket_index, colorize=colorize)
            )
