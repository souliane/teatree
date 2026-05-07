import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ANSI palette — modern terminals (iTerm2, Kitty, WezTerm, Ghostty,
# GNOME Terminal, Konsole, Windows Terminal) all render these. Honour
# the ``NO_COLOR`` standard (https://no-color.org/) by passing
# ``colorize=False`` to :func:`render`.
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2;37m"
_ANSI_RED = "\033[1;31m"
_ANSI_YELLOW = "\033[1;33m"
_ANSI_CYAN = "\033[1;36m"
_ANSI_BOLD = "\033[1m"

_ZONE_COLORS: dict[str, str] = {
    "anchors": _ANSI_DIM,
    "action_needed": _ANSI_RED,
    "in_flight": _ANSI_CYAN,
}

_ZONE_HEADERS: dict[str, str] = {
    "anchors": "",
    "action_needed": "Action needed:",
    "in_flight": "In flight:",
}


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


def _hyperlink(text: str, url: str) -> str:
    """Wrap *text* in an OSC 8 terminal hyperlink pointing at *url*."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _format_item(item: ZoneItem, color: str, *, colorize: bool) -> str:
    text = item.text if isinstance(item, StatuslineEntry) else item
    url = item.url if isinstance(item, StatuslineEntry) else ""
    if colorize:
        if url:
            text = _hyperlink(text, url)
        return f"{color}{text}{_ANSI_RESET}"
    if url:
        return f"{text} <{url}>"
    return text


def _format_header(name: str, *, colorize: bool) -> str:
    header = _ZONE_HEADERS[name]
    if not header:
        return ""
    return f"{_ANSI_BOLD}{header}{_ANSI_RESET}" if colorize else header


def render(zones: StatuslineZones, *, target: Path | None = None, colorize: bool | None = None) -> Path:
    """Atomically write *zones* to *target* (or the default path).

    *colorize* defaults to ``True`` unless the ``NO_COLOR`` environment
    variable is set. Tests can pass ``colorize=False`` to assert plain
    text content.
    """
    target = target or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if colorize is None:
        colorize = "NO_COLOR" not in os.environ

    sections: list[str] = []
    for name in ("anchors", "action_needed", "in_flight"):
        items: list[ZoneItem] = getattr(zones, name)
        if not items:
            continue
        color = _ZONE_COLORS.get(name, "")
        rendered = "\n".join(_format_item(item, color, colorize=colorize) for item in items)
        header = _format_header(name, colorize=colorize)
        sections.append(f"{header}\n{rendered}" if header else rendered)

    body = "\n\n".join(sections) + "\n"

    fd, tmp_str = tempfile.mkstemp(prefix=".statusline-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        Path(tmp_path).replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return target


__all__ = ["StatuslineEntry", "StatuslineZones", "default_path", "render"]
