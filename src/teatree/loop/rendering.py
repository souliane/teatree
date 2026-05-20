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
from teatree.loop.rendering_zones import _populate_overlay_zones, _render_action_line, _render_pr_group
from teatree.loop.statusline import StatuslineZones, colorize_enabled

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


def zones_for(actions: list[DispatchAction], *, colorize: bool | None = None) -> StatuslineZones:
    """Build statusline zones from dispatched actions.

    *colorize* threads the OSC 8 vs. plain ``text <url>`` decision into the
    line builder so ``NO_COLOR`` is honoured at the point links are formed
    (#721). ``None`` resolves from the environment via
    :func:`~teatree.loop.statusline.colorize_enabled`, matching
    :func:`~teatree.loop.statusline.render`'s own default.
    """
    colorize = colorize_enabled(colorize=colorize)
    zones = StatuslineZones()
    _populate_availability_anchor(zones)
    c = _classify_actions(actions)
    ticket_index = build_ticket_index(actions)
    enrich_pr_refs_with_permalinks(c, build_review_post_permalinks(actions))
    _populate_overlay_zones(zones, c, ticket_index=ticket_index, colorize=colorize)

    for zone_name, entry in c.other:
        zone_list = getattr(zones, zone_name, None)
        if isinstance(zone_list, list):
            zone_list.append(entry)

    return zones


def _populate_availability_anchor(zones: StatuslineZones) -> None:
    """Append the ``mode=away · N queued`` anchor when availability=away (#58).

    Fails open: any import or query error degrades to a no-op so a broken
    availability config can never blank the statusline.
    """
    try:
        from teatree.core.availability import pending_questions_count, resolve_mode  # noqa: PLC0415
        from teatree.loop.statusline import availability_anchor  # noqa: PLC0415

        resolution = resolve_mode()
        queued = pending_questions_count() if resolution.mode == "away" else 0
        line = availability_anchor(resolution.mode, queued)
    except Exception:  # noqa: BLE001
        return
    if line:
        zones.anchors.append(line)
