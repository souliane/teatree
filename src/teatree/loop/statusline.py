"""The statusline public surface, re-exported from cohesive sibling modules.

The implementation is split by concern so each module stays well under the
module-health cap:

*   :mod:`teatree.loop.statusline_palette` — the shared ANSI palette, zone
    colors, the CSI / OSC 8 / overlay-prefix regexes, and the time / recency
    constants both presentation and the loop line consume.
*   :mod:`teatree.loop.statusline_render` — the presentation layer:
    :class:`StatuslineEntry` / :class:`StatuslineZones`, the colour decision
    (:func:`colorize_enabled`), item / hyperlink formatting, the grouped-by-
    overlay :func:`render` writer, and the Slack-mrkdwn reader
    (:func:`statusline_for_slack`).
*   :mod:`teatree.loop.statusline_loops` — the dedicated loop-line dashboard:
    :func:`live_loops_anchor` and its DB-read seams (live leases, cadence
    resolution, mini-loop schedules, availability, pending questions) plus the
    recency-colour and next-tick formatters.

This module owns only the re-export so external importers keep a single stable
home (``teatree.loop.statusline``); identity is preserved so
``statusline.render is statusline_render.render``.
"""

from teatree.loop.statusline_loops import (
    availability_segment,
    live_loops_anchor,
    loop_owner_anchor,
    mini_loops_anchor,
    set_mini_loop_schedules_reader,
)
from teatree.loop.statusline_render import (
    StatuslineEntry,
    StatuslineZones,
    ZoneItem,
    colorize_enabled,
    default_path,
    plain_link,
    render,
    statusline_for_slack,
)

__all__ = [
    "StatuslineEntry",
    "StatuslineZones",
    "ZoneItem",
    "availability_segment",
    "colorize_enabled",
    "default_path",
    "live_loops_anchor",
    "loop_owner_anchor",
    "mini_loops_anchor",
    "plain_link",
    "render",
    "set_mini_loop_schedules_reader",
    "statusline_for_slack",
]
