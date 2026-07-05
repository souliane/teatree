"""Render classified actions into per-overlay statusline zone lines.

Split out of :mod:`teatree.loop.rendering` so the line-builder concern
(anchor / action_needed / in_flight rows, OSC8 links, stale-anchor
filtering, per-overlay zone population) owns one module. Consumes the
duplicate-free :class:`_ClassifiedActions` produced by
:mod:`teatree.loop.rendering_classification`.
"""

from collections.abc import Callable

from teatree.loop.rendering_classification import ActiveTicketRow, _ClassifiedActions, _is_url
from teatree.loop.rendering_dms import render_dm_line as _render_dm_line
from teatree.loop.rendering_items import (
    _effective_url,
    _format_mr_ref,
    _LinkCtx,
    _OverlayActionRefs,
    _PRRef,
    _render_canonical_item,
)
from teatree.loop.statusline import StatuslineZones, plain_link
from teatree.loop.statusline_render import _hyperlink
from teatree.url_classify import Forge, forge_of

_DISPOSITION_LABELS: dict[str, str] = {
    "issue_closed": "closed",
    "unassigned": "reassigned",
    "label_removed": "label-removed",
}

# States that should not surface as anchors. Terminal post-PR states never
# surface; ``not_started`` and ``in_review`` are also filtered (#1377) — the
# anchor row is for "what am I working on right now". In-review work
# already surfaces via PR/MR chips in the in-flight zone, and the
# ``not_started`` backlog is not user-actionable from the statusline.
# ``closed`` isn't a valid FSM value but turns up in old data.
_NOISE_STATES = frozenset(
    {
        "merged",
        "delivered",
        "shipped",
        "retrospected",
        "closed",
        "not_started",
        "in_review",
    },
)
_MAX_PER_STATE = 5

# Tickets whose ``issue_url`` matches one of these are treated as PR-backed.
# A PR-backed ticket that doesn't appear in the live PR set (action_needed
# or in_flight) is considered stale — its remote MR has likely been merged
# or closed but the local FSM never advanced.

# Anchor-line state-group rendering order. Actively-shipping work comes
# first; states not listed here render in their original insertion order
# after the listed ones. With ``not_started`` and ``in_review`` filtered
# (#1377) the surviving anchor states are the actively-shipping ones.
_STATE_PRIORITY: tuple[str, ...] = (
    "started",
    "coded",
    "tested",
    "ready",
    "reviewed",
    "scoped",
)


def _state_sort_key(state: str) -> tuple[int, str]:
    """Sort key giving listed states their explicit priority order."""
    try:
        return (_STATE_PRIORITY.index(state), state)
    except ValueError:
        return (len(_STATE_PRIORITY), state)


def _link(text: str, url: object, *, colorize: bool) -> str:
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return _hyperlink(text, url) if colorize else plain_link(text, url)
    return text


# The tracker-search-base RESOLUTION lives in :mod:`teatree.loop.rendering`
# (which may import ``teatree.core``) and is threaded in as a plain string per
# overlay, so this line-builder module stays core-free (its tach node forbids a
# ``teatree.core`` edge).


def _link_with_search(text: str, url: str, *, search_base: str, colorize: bool) -> str:
    """Link *text* to its canonical URL, else the tracker search (PR-17)."""
    return _link(text, _effective_url(url, text, search_base), colorize=colorize)


def _is_pr_url(url: str) -> bool:
    return _is_url(url) and forge_of(url) is not Forge.UNKNOWN


def _render_pr_group(
    overlay: str,
    refs: list[_PRRef],
    *,
    ticket_index: dict[str, str] | None = None,
    colorize: bool,
    search_base: str = "",
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

    ctx = _LinkCtx(colorize=colorize, link=_link, search_base=search_base)

    chunks: list[str] = []
    for tnum in sorted(by_ticket):
        bucket = " ".join(_format_mr_ref(r, ctx) for r in by_ticket[tnum])
        chunks.append(f"#{tnum}: {bucket}")
    if orphans:
        chunks.append(" ".join(_format_mr_ref(r, ctx) for r in orphans))
    return f"{prefix}{' · '.join(chunks)}"


def _render_ticket_line(
    overlay: str,
    tickets: list[ActiveTicketRow],
    pr_map: dict[str, list[_PRRef]],
    *,
    live_pr_urls: set[str] | None = None,
    ctx: _LinkCtx,
) -> str:
    """Render the per-overlay anchor line grouped by FSM state (#130).

    One physical dim line per overlay (HARD RULE: max 1 line per overlay
    per color). Tickets are bucketed by their FSM ``state:`` label
    (``coded:`` / ``tested:`` / ``scoped:`` …), the buckets render in
    :data:`_STATE_PRIORITY` order, and the buckets are joined by `` · ``
    into the single line::

        [overlay] coded: #N (topic !chip) · tested: #M (topic) · scoped: #K

    The ``state:`` label is the in-line group header the user explicitly
    asked for — it makes the grouping legible and answers "what state is
    #X" at a glance. #1377 dropped it; the latest authoritative
    requirement (group BY STATUS, reusing the FSM state) restores it. The
    terse per-item shape ``#N (topic !chips)`` from #1377 is unchanged.

    ``pr_map`` is the overlay's MR-iid → child-refs map; entries appear
    here either because the MR's iid equals the ticket number (legacy
    shape), or because the caller pre-bucketed by parent ticket number
    via ``ticket_index`` (canonical shape).
    """
    prefix = f"[{overlay}] " if overlay else ""
    live = live_pr_urls or set()
    by_state: dict[str, list[str]] = {}
    for row in tickets:
        if row.state in _NOISE_STATES:
            continue
        if _is_pr_url(row.issue_url) and row.issue_url not in live:
            continue
        # ⚡ chip flags an expedite/release-blocker ticket (PR-07): it may push
        # pre-CI, but the merge keystone is never relaxed.
        label = f"⚡#{row.number}" if row.expedite else f"#{row.number}"
        by_state.setdefault(row.state, []).append(
            _render_canonical_item(
                label=label,
                url=row.issue_url,
                title=row.title,
                child_refs=pr_map.get(row.number, []),
                ctx=ctx,
            ),
        )
    if not by_state:
        return ""
    state_chunks: list[str] = []
    for state in sorted(by_state, key=_state_sort_key):
        bucket = by_state[state]
        shown = bucket[:_MAX_PER_STATE]
        overflow = len(bucket) - len(shown)
        items = list(shown)
        if overflow > 0:
            items.append(f"(+{overflow} more)")
        state_chunks.append(f"{state}: {' '.join(items)}")
    return f"{prefix}{' · '.join(state_chunks)}"


def _disposition_parts(action_refs: _OverlayActionRefs, *, search_base: str, colorize: bool) -> list[str]:
    """Render the issue-disposition rows for one overlay.

    Covers generic dispositions, the explicit ``reassigned (from → to)``
    transition, and the collapsed ``N stale`` row. Each is one concise part
    with linked refs — the stale row in particular folds every stale ticket
    for the overlay into a single line. A ref with no canonical URL links to
    the overlay's tracker search rather than rendering bare (PR-17).
    """
    parts: list[str] = []
    for reason, refs in action_refs.disposition_refs.items():
        # Defense-in-depth against the dispatch leak (#130): a disposition
        # whose refs ALL resolve to the bare ``?`` token carries no usable
        # identity — it is scanner bookkeeping that slipped through, not a
        # real ticket. Rendering it produces the ``<reason>: ?`` garbage row
        # the dashboard rework exists to kill, so drop the whole part.
        usable = [r for r in refs if r.label != "?"]
        if not usable:
            continue
        label = _DISPOSITION_LABELS.get(reason, reason)
        items = " ".join(_link_with_search(r.label, r.url, search_base=search_base, colorize=colorize) for r in usable)
        parts.append(f"{label}: {items}")
    for rr in action_refs.reassign_refs:
        to = ", ".join(rr.new_owners)
        ref_link = _link_with_search(rr.ref.label, rr.ref.url, search_base=search_base, colorize=colorize)
        parts.append(f"reassigned (from {rr.old_owner} → to {to}): {ref_link}")
    if action_refs.stale_refs:
        stale = action_refs.stale_refs
        items = " ".join(_link_with_search(r.label, r.url, search_base=search_base, colorize=colorize) for r in stale)
        parts.append(f"{len(stale)} stale: {items}")
    return parts


def _render_action_line(
    overlay: str,
    action_refs: _OverlayActionRefs,
    *,
    ticket_index: dict[str, str] | None = None,
    colorize: bool,
    search_base: str = "",
) -> str:
    prefix = f"[{overlay}] " if overlay else ""
    prs_by_ticket: dict[str, list[_PRRef]] = {}
    for ref in action_refs.pr_refs:
        parent = (ticket_index or {}).get(ref.url, "")
        if parent and parent != str(ref.iid):
            prs_by_ticket.setdefault(parent, []).append(ref)
    consumed_pr_urls: set[str] = set()

    parts: list[str] = _disposition_parts(action_refs, search_base=search_base, colorize=colorize)
    if action_refs.ready_refs:
        # Cap the ready: row at _MAX_PER_STATE and append ``(+N more)``
        # overflow, matching the anchor state lines. Without the cap a
        # backlog of assigned issues spills the entire list onto a single
        # line.
        ctx = _LinkCtx(colorize=colorize, link=_link, search_base=search_base)
        items: list[str] = []
        shown_refs = action_refs.ready_refs[:_MAX_PER_STATE]
        overflow = len(action_refs.ready_refs) - len(shown_refs)
        for ref in shown_refs:
            number = ref.label.lstrip("#")
            prs = prs_by_ticket.get(number, [])
            items.append(
                _render_canonical_item(
                    label=ref.label,
                    url=ref.url,
                    title=ref.title,
                    child_refs=prs,
                    ctx=ctx,
                ),
            )
            consumed_pr_urls.update(p.url for p in prs)
        ready_chunk = " ".join(items)
        if overflow > 0:
            ready_chunk += f" (+{overflow} more)"
        parts.append(f"ready: {ready_chunk}")
    if action_refs.pr_refs:
        remaining = [r for r in action_refs.pr_refs if r.url not in consumed_pr_urls]
        if remaining:
            parts.insert(
                0,
                _render_pr_group(
                    overlay,
                    remaining,
                    ticket_index=ticket_index,
                    colorize=colorize,
                    search_base=search_base,
                ).removeprefix(prefix),
            )
    if not parts:
        return ""
    return f"{prefix}{' · '.join(parts)}"


def _populate_overlay_zones(
    zones: StatuslineZones,
    c: _ClassifiedActions,
    *,
    ticket_index: dict[str, str],
    colorize: bool,
    search_base_of: Callable[[str], str] = lambda _overlay: "",
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
        search_base = search_base_of(overlay_key)
        ticket_line = _render_ticket_line(
            overlay_key,
            c.active_tickets.get(overlay_key, []),
            pr_map,
            live_pr_urls=live_pr_urls_by_overlay.get(overlay_key, set()),
            ctx=_LinkCtx(colorize=colorize, link=_link, search_base=search_base),
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
            search_base=search_base,
        )
        if action_line:
            zones.action_needed.append(action_line)

        inflight_refs = c.inflight_prs.get(overlay_key, [])
        if inflight_refs:
            zones.in_flight.append(
                _render_pr_group(
                    overlay_key,
                    inflight_refs,
                    ticket_index=ticket_index,
                    colorize=colorize,
                    search_base=search_base,
                )
            )
