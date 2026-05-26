import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.managers import OwnershipStatus

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

# Strip CSI (color/cursor) escapes. Matches the canonical SGR/CSI shape:
# the parameters (digits/semicolons/spaces) followed by any single final
# byte in 0x40-0x7E.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Match an OSC 8 terminal hyperlink and capture its URL and TEXT for the
# Slack-mrkdwn rewrite. The hyperlink wraps TEXT in a start/end pair whose
# terminators may be either the ST (ESC backslash) or BEL (0x07).
_ANSI_OSC8_RE = re.compile(
    r"\x1b\]8;[^;]*;(?P<url>[^\x07\x1b]*)(?:\x1b\\|\x07)"
    r"(?P<text>.*?)"
    r"\x1b\]8;[^;]*;(?:\x1b\\|\x07)",
    flags=re.DOTALL,
)


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


def availability_anchor(mode: str, queued: int) -> str:
    """Return the anchors-zone segment for the availability mode (#58).

    Renders ``mode=away · N queued`` when ``mode`` is ``away`` AND there
    is at least one deferred question waiting; ``mode=away`` alone when
    no queue; an empty string in ``present`` mode (the default mode is
    not interesting enough to warrant a dedicated anchor line).
    """
    if mode != "away":
        return ""
    if queued > 0:
        return f"mode=away · {queued} queued"
    return "mode=away"


def _live_loop_names() -> list[tuple[str, str]]:
    """Return ``(loop_name, session_id)`` for every currently-live LoopLease.

    Isolated as a thin DB-read seam so :func:`live_loops_anchor` stays a
    pure formatter — tests stub this function rather than constructing
    LoopLease fixtures, and the renderer keeps a single try/except gate
    around it for fail-open semantics.
    """
    from django.apps import apps  # noqa: PLC0415
    from django.utils import timezone  # noqa: PLC0415

    lease_model = apps.get_model("core", "LoopLease")
    rows = lease_model.objects.filter(lease_expires_at__gt=timezone.now()).only("name", "session_id").order_by("name")
    return [(row.name, row.session_id) for row in rows]


def _loop_tick_acquired_at() -> datetime | None:
    """Return the most recent ``acquired_at`` for the ``loop-tick`` lease.

    Isolated as a thin DB-read seam (mirrors :func:`_live_loop_names`) so
    :func:`live_loops_anchor` stays pure and tests stub this directly
    rather than constructing LoopLease fixtures with timestamps.

    Returns ``None`` when no row exists yet (no tick has ever fired) so
    the renderer can render a ``last tick: never`` fallback instead of
    pretending to know.
    """
    from django.apps import apps  # noqa: PLC0415

    lease_model = apps.get_model("core", "LoopLease")
    row = lease_model.objects.filter(name="loop-tick").values("acquired_at").first()
    if row is None:
        return None
    return row.get("acquired_at")


def _cadence_seconds() -> int:
    """Return the resolved loop cadence in seconds.

    Isolated as a seam so tests can stub it without spinning up the
    full config layer; production reads :func:`teatree.config.cadence_seconds`.
    """
    from teatree.config import cadence_seconds  # noqa: PLC0415

    return cadence_seconds()


def loop_owner_anchor(status: "OwnershipStatus", this_session: str) -> tuple[str, str]:
    """Return ``(zone, line)`` for the foreign-hijack RED line (#1073, #1156).

    #1156 narrowed this to only the foreign-hijack RED case. The dim
    ``loop-owner=THIS session ✓`` and ``loop-owner=unclaimed`` lines were
    replaced by :func:`live_loops_anchor`, which renders one line per
    live :class:`teatree.core.models.LoopLease` row.

    A *different* live session owns it → ``("action_needed",
    "loop-owner=session <short8> (NOT this session)")`` — RED, because a
    foreign owner is exactly the #1073 hijack the user must see.

    Anything else (this session owns it, or no live owner) → ``("anchors",
    "")``. Callers suppress empty lines.

    ``short8`` is the first 8 chars of the owner session id.
    """
    if not status.is_live:
        return "anchors", ""
    if this_session and status.owner_session == this_session:
        return "anchors", ""
    short8 = status.owner_session[:8]
    return "action_needed", f"loop-owner=session {short8} (NOT this session)"


def live_loops_anchor() -> list[str]:
    """Return one consolidated dim anchor line summarising live loops.

    Single line, prepended at the top of the statusline so the user's
    "where is the loop right now?" question is answered with one glance:

        ``loop · next tick in 3m12s · 5 loops live``

    Components, in order:

    *   ``next tick in <duration>`` when the ``loop-tick`` lease has fired
        at least once and a cadence is configured; the next-tick instant
        is ``acquired_at + cadence_seconds``. ``next tick due`` when the
        deadline has already passed (the next ``t3 loop tick`` is overdue
        but no tick has acquired the lease yet, e.g. cron paused).
        ``last tick: never`` when no row exists yet (no tick has fired).
    *   ``<N loops live>`` — the headline number, deduped per-loop.
        Replaces the prior one-line-per-loop dump (``loop:tick``,
        ``loop:owner``, …) which the user explicitly did not want at the
        top of the statusline.

    Returns ``[]`` when no loop is live — silences the line entirely on
    a quiet machine. Fails open: any DB / import error degrades to ``[]``
    so a broken LoopLease read cannot blank the statusline.

    Backward-compat: the return signature is unchanged (``list[str]``)
    so :func:`_populate_live_loops_anchor` keeps appending whatever lines
    we return; today it's at most one line, tomorrow it can grow without
    a caller-side change.
    """
    try:
        live_names = _live_loop_names()
    except Exception:  # noqa: BLE001
        return []
    if not live_names:
        return []

    parts: list[str] = ["loop"]
    parts.extend(_next_tick_parts())
    parts.append(f"{len(live_names)} loops live")
    return [" · ".join(parts)]


def _next_tick_parts() -> list[str]:
    """Return the ``next tick in <duration>`` / fallback fragments.

    Empty list when nothing useful is queryable (no cadence, no tick
    history). Fails open on every DB / config read — the line drops the
    fragment rather than refusing to render.
    """
    try:
        acquired_at = _loop_tick_acquired_at()
    except Exception:  # noqa: BLE001
        return []
    if acquired_at is None:
        return ["last tick: never"]
    try:
        cadence = _cadence_seconds()
    except Exception:  # noqa: BLE001
        return []
    from django.utils import timezone  # noqa: PLC0415

    now = timezone.now()
    next_tick_at = acquired_at + timedelta(seconds=cadence)
    delta_seconds = int((next_tick_at - now).total_seconds())
    if delta_seconds <= 0:
        return ["next tick due"]
    return [f"next tick in {_format_duration(delta_seconds)}"]


_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _format_duration(seconds: int) -> str:
    """Format ``seconds`` as a compact human duration (``3m12s``, ``45s``, ``1h05m``)."""
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < _SECONDS_PER_HOUR:
        minutes, remainder = divmod(seconds, _SECONDS_PER_MINUTE)
        if remainder:
            return f"{minutes}m{remainder:02d}s"
        return f"{minutes}m"
    hours, remainder = divmod(seconds, _SECONDS_PER_HOUR)
    minutes = remainder // _SECONDS_PER_MINUTE
    return f"{hours}h{minutes:02d}m"


def statusline_for_slack(*, path: Path | None = None) -> str:
    r"""Return the on-disk statusline transformed for Slack mrkdwn (#1121).

    Reads the statusline file at *path* (or :func:`default_path`), strips
    ANSI CSI escapes (colors/resets), and rewrites OSC 8 terminal
    hyperlinks ``ESC]8;;URL ESC\ TEXT ESC]8;; ESC\`` to Slack's
    ``<URL|TEXT>`` mrkdwn form.

    Returns ``""`` when the file is missing or empty — callers treat an
    empty result the same as "no statusline content", which is the cue to
    fall through to a different answer path.

    Never *regenerates* the statusline — Slack-answer is a reader, not a
    producer.
    """
    target = path or default_path()
    try:
        body = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    if not body:
        return ""
    rewritten = _ANSI_OSC8_RE.sub(lambda m: f"<{m.group('url')}|{m.group('text')}>", body)
    return _ANSI_CSI_RE.sub("", rewritten)


__all__ = [
    "StatuslineEntry",
    "StatuslineZones",
    "availability_anchor",
    "default_path",
    "live_loops_anchor",
    "loop_owner_anchor",
    "render",
    "statusline_for_slack",
]
