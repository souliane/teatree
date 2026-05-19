r"""Render inbound Slack DMs into the statusline (#1050).

A separate module keeps :mod:`teatree.loop.rendering` under the
module-health LOC ceiling: the DM line is one concern with a tight
contract (input: list of ``_DmRef``; output: one ``[ov] DMs (N):
<permalink1> · …`` row in the anchors zone), so isolating it here is
the right split-by-concern.

The line is dim by virtue of routing to ``zones.anchors`` — the
statusline's ``_ZONE_COLORS`` maps ``anchors`` to ``\033[38;5;244m``
(the same palette as ``started:``/``tested:`` rows). DMs are
informational context (the user reads the body in Slack natively); a
red ``action_needed`` row would imply they require terminal-side
action, which they do not.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from teatree.loop.dispatch import DispatchAction

type Payload = Mapping[str, Any]
type LinkRenderer = Callable[..., str]


@dataclass(frozen=True, slots=True)
class DmRef:
    """One inbound Slack DM, rendered as a clickable permalink.

    ``permalink`` may be empty when ``backend.get_permalink`` failed at
    scan time; the renderer falls back to the bare ``ts`` label in that
    case so the row still surfaces (a Slack outage must not collapse
    the line back to red multi-line body-paste).
    """

    ts: str
    permalink: str


def is_dm_action(action: DispatchAction, payload: Payload) -> bool:
    """Return True when *action* came from a ``slack.dm`` scan signal.

    The scanner emits ``summary=f"DM {ts}: …"`` and a payload carrying
    ``event`` (the raw Slack event dict) plus ``ts`` (string). Dispatch
    mirrors ``summary`` into ``detail`` unchanged, so we identify the
    signal by the payload shape (event-dict + ts-string), gating on
    the ``DM `` summary prefix to skip foreign signals that happen to
    carry an ``event`` field.
    """
    if not action.detail.startswith("DM "):
        return False
    if not isinstance(payload.get("event"), dict):
        return False
    return isinstance(payload.get("ts"), str)


def render_dm_line(
    overlay: str,
    dms: list[DmRef],
    *,
    link: LinkRenderer,
    colorize: bool,
) -> str:
    """Render one ``[ov] DMs (N): <permalink1> · …`` line per overlay.

    *link* is the rendering module's ``_link`` helper — passed in so
    this module stays free of the OSC8/plain-link decision wiring.
    Inbound Slack DMs are informational context — the user reads the
    body in Slack natively, the statusline only needs to point at the
    message. When ``permalink`` is empty (Slack outage at scan time),
    the bare ``ts`` is shown as label so the row still surfaces.
    """
    if not dms:
        return ""
    prefix = f"[{overlay}] " if overlay else ""
    parts: list[str] = []
    for dm in dms:
        label = dm.ts or "?"
        if dm.permalink:
            parts.append(link(label, dm.permalink, colorize=colorize))
        else:
            parts.append(label)
    return f"{prefix}DMs ({len(dms)}): {' · '.join(parts)}"
