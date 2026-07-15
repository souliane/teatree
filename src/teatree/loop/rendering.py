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

from django.utils import timezone

from teatree.loop.dispatch import DispatchAction
from teatree.loop.pr_ticket_index import build_ticket_index
from teatree.loop.rendering_classification import _ClassifiedActions, _classify_actions, _issue_ref_from
from teatree.loop.rendering_items import _IssueRef, _OverlayActionRefs, _PRRef
from teatree.loop.rendering_permalinks import build_review_post_permalinks, enrich_pr_refs_with_permalinks
from teatree.loop.rendering_zones import _MAX_PER_STATE, _populate_overlay_zones, _render_action_line, _render_pr_group
from teatree.loop.statusline import StatuslineEntry, StatuslineZones, ZoneItem, colorize_enabled, dashboard_head_anchor

__all__ = [
    "_ClassifiedActions",
    "_IssueRef",
    "_OverlayActionRefs",
    "_PRRef",
    "_classify_actions",
    "_issue_ref_from",
    "_overlay_search_base",
    "_populate_dashboard_head",
    "_render_action_line",
    "_render_pr_group",
    "cost_chip_lines",
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
    # The consolidated dashboard head must stay line 1 (#130/#1400) —
    # statusline.sh prepends the per-session t3-master badge to it. It folds the
    # loop line (which carries the availability segment, #1678) with the
    # overlays summary and the health chip onto that one line.
    _populate_dashboard_head(zones, colorize=colorize)
    c = _classify_actions(actions, identity_aliases)
    ticket_index = build_ticket_index(actions)
    enrich_pr_refs_with_permalinks(c, build_review_post_permalinks(actions))
    _populate_overlay_zones(
        zones,
        c,
        ticket_index=ticket_index,
        colorize=colorize,
        search_base_of=_overlay_search_base,
    )
    _append_capped_other(zones, c.other)
    return zones


def _overlay_search_base(overlay: str) -> str:
    """Return the overlay's tracker-search URL prefix, or ``""`` (PR-17).

    Resolves the overlay's forge from its ``code_host`` (default GitHub) and
    returns the issue-search prefix the renderer appends a term to, so a ref
    with no canonical URL links to the tracker search instead of rendering bare.
    Lives here (not in the core-free ``rendering_zones``) because resolving the
    overlay needs ``teatree.core``; the resolved string is threaded down. Fails
    open to ``""`` — an unresolvable overlay degrades to bare text, never a crash.
    """
    if not overlay:
        return ""
    try:
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred read

        resolved = get_all_overlays().get(overlay)
        if resolved is None:
            return ""
        host = (resolved.config.code_host or "github").strip().lower()
    except Exception:  # noqa: BLE001 — fail-open: an unresolvable overlay degrades to bare text, never a crash
        return ""
    if host == "gitlab":
        return "https://gitlab.com/search?scope=issues&search="
    return "https://github.com/search?type=issues&q="


def _populate_dashboard_head(zones: StatuslineZones, *, colorize: bool | None = None) -> None:
    """Append the consolidated dashboard head line — loops + overlays + health.

    :func:`~teatree.loop.statusline.dashboard_head_anchor` folds the loop line
    (carrying availability, #1678), the configured-overlays summary, and the
    health chip onto ONE line and is itself fail-open, so this wrapper only
    resolves the ``NO_COLOR`` decision and does the append.
    """
    zones.anchors.extend(dashboard_head_anchor(colorize=colorize_enabled(colorize=colorize)))


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


def cost_chip_lines() -> list[str]:
    """Return the SDK-equivalent cost chip as a one-line list, or ``[]``.

    Cycle-to-date SDK-equivalent spend of the loop's headless ``claude -p``
    usage against the monthly Agent-SDK credit, tiny at any spend. Empty when
    no headless cost is captured this cycle. The statusline header reads it
    from the ``tick-meta.json`` sidecar and renders it next to the weekly
    (``7d=``) rate-limit segment. Fails open to ``[]`` on any DB / import
    error so a broken cost read never blanks the statusline.
    """
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.core.cost import CostReport, cycle_start, cycle_start_datetime  # noqa: PLC0415 — tick-time import
        from teatree.core.models.task_attempt import TaskAttempt  # noqa: PLC0415 — deferred: ORM/app-registry

        settings = get_effective_settings()
        anchor = settings.billing_cycle_anchor_day or None
        today = timezone.localdate()
        start_dt = cycle_start_datetime(today, anchor_day=anchor)
        breakdown = TaskAttempt.objects.headless().filter(started_at__gte=start_dt).cost_breakdown()
        if breakdown.attempts == 0:
            return []
        report = CostReport.build(
            breakdown,
            credit_usd=settings.sdk_monthly_credit_usd,
            cycle_start_date=cycle_start(today, anchor_day=anchor),
            today=today,
        )
        return [report.chip()]
    except Exception:  # noqa: BLE001 — rendering is best-effort; a failure degrades to no anchors
        return []
