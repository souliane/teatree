"""Render dispatched actions into statusline zones.

Split out of :mod:`teatree.loop.tick` so each module owns one concern: tick is
the orchestrator (scan → dispatch → render), rendering is the formatter
(classify actions per overlay, render anchor / action_needed / in_flight rows
with OSC8 links, filter stale anchors).

This module is the thin top-level orchestrator. The two concerns it
coordinates live in focused sub-modules so each stays under the module
health ceiling: ``rendering_classification`` turns each dispatched signal
into a typed ref and buckets/dedups per overlay (``_classify_actions``),
and ``rendering_zones`` turns the classified buckets into per-zone
statusline rows (``_populate_overlay_zones`` and the line builders).

The names re-exported below keep ``from teatree.loop.rendering import X``
working for every existing consumer and test after the split.
"""

from teatree.loop.dispatch import DispatchAction
from teatree.loop.pr_ticket_index import build_ticket_index
from teatree.loop.rendering_classification import _ClassifiedActions, _classify_actions, _issue_ref_from
from teatree.loop.rendering_items import _IssueRef, _OverlayActionRefs, _PRRef
from teatree.loop.rendering_permalinks import build_review_post_permalinks, enrich_pr_refs_with_permalinks
from teatree.loop.rendering_zones import _MAX_PER_STATE, _populate_overlay_zones, _render_action_line, _render_pr_group
from teatree.loop.statusline import StatuslineEntry, StatuslineZones, ZoneItem, colorize_enabled

__all__ = [
    "_ClassifiedActions",
    "_IssueRef",
    "_OverlayActionRefs",
    "_PRRef",
    "_classify_actions",
    "_issue_ref_from",
    "_render_action_line",
    "_render_pr_group",
    "zones_for",
]


def zones_for(
    actions: list[DispatchAction],
    *,
    colorize: bool | None = None,
    identity_aliases: tuple[tuple[str, ...], ...] = (),
) -> StatuslineZones:
    """Build statusline zones from dispatched actions.

    *colorize* threads the OSC 8 vs. plain ``text <url>`` decision into the
    line builder so ``NO_COLOR`` is honoured at the point links are formed
    (#721). ``None`` resolves from the environment via
    :func:`~teatree.loop.statusline.colorize_enabled`, matching
    :func:`~teatree.loop.statusline.render`'s own default.

    *identity_aliases* groups one human's forge handles; a reassignment
    between handles of the same human is suppressed and every handle
    collapses to its group's canonical name.
    """
    colorize = colorize_enabled(colorize=colorize)
    zones = StatuslineZones()
    # The dedicated ``loop running · …`` line must stay line 1 (#130/#1400);
    # the live availability segment rides on that line (#1678).
    _populate_live_loops_anchor(zones)
    c = _classify_actions(actions, identity_aliases)
    ticket_index = build_ticket_index(actions)
    enrich_pr_refs_with_permalinks(c, build_review_post_permalinks(actions))
    _populate_overlay_zones(zones, c, ticket_index=ticket_index, colorize=colorize)
    _append_capped_other(zones, c.other)
    return zones


def _append_capped_other(zones: StatuslineZones, other: list[tuple[str, StatuslineEntry]]) -> None:
    """Append the ``_ClassifiedActions.other`` fallback rows, capped per zone.

    Each prefix-less fallback row (a ``pending_task`` whose phase has no agent,
    an unrouted statusline signal) lands here. A backlog of them — dozens of
    auto-enqueued ``short_describe`` tasks — otherwise floods one zone and
    pushes the anchor lines (the loop line and the configured-overlays summary)
    out of the height-limited statusline pane. The cap mirrors the per-state
    ``_MAX_PER_STATE`` + ``(+N more)`` overflow the per-overlay line builders
    already apply, keeping the whole statusline bounded.
    """
    by_zone: dict[str, list[ZoneItem]] = {}
    for zone_name, entry in other:
        by_zone.setdefault(zone_name, []).append(entry)
    for zone_name, entries in by_zone.items():
        zone_list = getattr(zones, zone_name, None)
        if not isinstance(zone_list, list):
            continue
        zone_list.extend(entries[:_MAX_PER_STATE])
        overflow = len(entries) - _MAX_PER_STATE
        if overflow > 0:
            zone_list.append(f"(+{overflow} more)")


def _populate_live_loops_anchor(zones: StatuslineZones) -> None:
    """Append one anchor line per live :class:`LoopLease` row (#1163).

    Multi-loop visibility: the user runs ``loop-tick``, ``loop-owner``,
    ``loop-self-improve``, ``loop-slack-answer``, ``loop-slot`` in parallel
    — surfacing each gives the at-a-glance count the prior single
    ``loop-owner=…`` anchor hid.

    :func:`~teatree.loop.statusline.live_loops_anchor` is itself fail-open,
    so this wrapper exists only to do the append.
    """
    from teatree.loop.statusline import live_loops_anchor  # noqa: PLC0415

    zones.anchors.extend(live_loops_anchor())
