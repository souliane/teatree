import re

# ANSI palette — modern terminals (iTerm2, Kitty, WezTerm, Ghostty,
# GNOME Terminal, Konsole, Windows Terminal) all render these. Honour
# the ``NO_COLOR`` standard (https://no-color.org/) by passing
# ``colorize=False`` to :func:`teatree.loop.statusline_render.render`.
_ANSI_RESET = "\033[0m"
# 256-color light gray reads better than legacy DIM (``\033[2m``) on most
# themes — DIM is essentially invisible on dark backgrounds with low contrast.
_ANSI_DIM = "\033[38;5;244m"
_ANSI_RED = "\033[1;31m"
_ANSI_YELLOW = "\033[1;33m"
_ANSI_GREEN = "\033[1;32m"
_ANSI_CYAN = "\033[1;36m"
_ANSI_BOLD = "\033[1m"

# Per-loop recency thresholds, expressed as the FRACTION of the loop's own
# cadence still remaining until its next tick. Judging on the fraction (not
# absolute seconds) makes the color relative to each loop's cadence, so a fast
# 60s cron and a slow 1h cron are scored on their own scale.
_RECENCY_GREEN_FRACTION = 0.5
_RECENCY_YELLOW_FRACTION = 0.15

_ZONE_COLORS: dict[str, str] = {
    "anchors": _ANSI_DIM,
    "action_needed": _ANSI_RED,
    "in_flight": _ANSI_CYAN,
}

# Zone-level "Action needed:" / "In flight:" headers used to live above each
# block but color (red/cyan) already carries that meaning — the legend at the
# bottom of :func:`teatree.loop.statusline_render.render` makes the contract
# explicit without using a line of vertical space per zone.
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

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
