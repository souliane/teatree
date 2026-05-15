import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ANSI palette — modern terminals (iTerm2, Kitty, WezTerm, Ghostty,
# GNOME Terminal, Konsole, Windows Terminal) all render these. Honour
# the ``NO_COLOR`` standard (https://no-color.org/) by passing
# ``colorize=False`` to :func:`render`.
_ANSI_RESET = "\033[0m"
# 256-color light gray reads better than legacy DIM (``\033[2m``) on most
# themes — DIM is essentially invisible on dark backgrounds with low contrast.
_ANSI_DIM = "\033[38;5;244m"
_ANSI_RED = "\033[1;31m"
_ANSI_YELLOW = "\033[1;33m"
_ANSI_CYAN = "\033[1;36m"
_ANSI_BOLD = "\033[1m"

_ZONE_COLORS: dict[str, str] = {
    "anchors": _ANSI_DIM,
    "action_needed": _ANSI_RED,
    "in_flight": _ANSI_CYAN,
}

# Zone-level "Action needed:" / "In flight:" headers used to live above each
# block but color (red/cyan) already carries that meaning — the legend at the
# bottom of :func:`render` makes the contract explicit without using a line
# of vertical space per zone.
_OVERLAY_PREFIX_RE = re.compile(r"^\[([^\]]+)\] ")


@dataclass(frozen=True, slots=True)
class StatuslineEntry:
    """A single statusline line with an optional URL.

    When *url* is non-empty, :func:`render` wraps *text* in an OSC 8
    hyperlink so terminals that support it render the line as clickable.
    """

    text: str
    url: str = ""


type ZoneItem = str | StatuslineEntry


@dataclass(slots=True)
class StatuslineZones:
    anchors: list[ZoneItem] = field(default_factory=list)
    action_needed: list[ZoneItem] = field(default_factory=list)
    in_flight: list[ZoneItem] = field(default_factory=list)


def default_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree" / "statusline.txt"


def colorize_enabled(*, colorize: bool | None = None) -> bool:
    """Resolve the effective colour decision (single source of truth).

    ``None`` resolves from the ``NO_COLOR`` standard (https://no-color.org/):
    colour is on unless ``NO_COLOR`` is present in the environment. Both
    :func:`render` and the line builder in :mod:`teatree.loop.rendering`
    consult this so the OSC 8 / plain-``text <url>`` decision is made in
    exactly one place (#721).
    """
    if colorize is not None:
        return colorize
    return "NO_COLOR" not in os.environ


def _hyperlink(text: str, url: str) -> str:
    """Wrap *text* in an OSC 8 terminal hyperlink pointing at *url*."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def plain_link(text: str, url: str) -> str:
    """The NO_COLOR fallback form — identical to ``_format_item``'s."""
    return f"{text} <{url}>"


def _format_item(item: ZoneItem, color: str, *, colorize: bool) -> str:
    text = item.text if isinstance(item, StatuslineEntry) else item
    url = item.url if isinstance(item, StatuslineEntry) else ""
    if colorize:
        if url:
            text = _hyperlink(text, url)
        return f"{color}{text}{_ANSI_RESET}"
    if url:
        return plain_link(text, url)
    return text


def _overlay_of(item: ZoneItem) -> str:
    """Pull the ``[ov]`` prefix from a line, or return '' when there is none.

    Each renderer in :mod:`teatree.loop.tick` prefixes its lines with
    ``[ov] …`` so we can group all of an overlay's anchors / action / in-flight
    rows together by reading the prefix back here.
    """
    text = item.text if isinstance(item, StatuslineEntry) else item
    match = _OVERLAY_PREFIX_RE.match(text)
    return match.group(1) if match else ""


def render(zones: StatuslineZones, *, target: Path | None = None, colorize: bool | None = None) -> Path:
    """Atomically write *zones* to *target* (or the default path).

    Output is grouped by overlay rather than by zone — each ``[ov]`` block
    shows its anchors (dim), action-needed rows (red), and in-flight rows
    (cyan) consecutively. The per-zone "Action needed:" / "In flight:"
    headers are gone — color carries the signal.

    *colorize* defaults to ``True`` unless the ``NO_COLOR`` environment
    variable is set. Tests can pass ``colorize=False`` to assert plain
    text content.
    """
    target = target or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    colorize = colorize_enabled(colorize=colorize)

    # Group every line by its [overlay] prefix, preserving insertion order.
    by_overlay: dict[str, dict[str, list[ZoneItem]]] = {}
    order: list[str] = []
    for name in ("anchors", "action_needed", "in_flight"):
        for item in getattr(zones, name):
            overlay = _overlay_of(item)
            if overlay not in by_overlay:
                by_overlay[overlay] = {"anchors": [], "action_needed": [], "in_flight": []}
                order.append(overlay)
            by_overlay[overlay][name].append(item)

    sections: list[str] = []
    for overlay in order:
        lines: list[str] = []
        for name in ("anchors", "action_needed", "in_flight"):
            color = _ZONE_COLORS.get(name, "")
            lines.extend(_format_item(item, color, colorize=colorize) for item in by_overlay[overlay][name])
        if lines:
            sections.append("\n".join(lines))

    body = ("\n\n".join(sections) + "\n") if sections else ""

    fd, tmp_str = tempfile.mkstemp(prefix=".statusline-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        Path(tmp_path).replace(target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return target


__all__ = ["StatuslineEntry", "StatuslineZones", "default_path", "render"]
