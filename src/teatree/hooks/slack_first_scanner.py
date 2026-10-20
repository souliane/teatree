"""Slack-first detector: an owner prompt in an attended session that never went through the relay.

Pure and Django-free — the Stop handler (``hooks/scripts/slack_first_gate.py``) feeds it the owner's words
and the final turn's tool results; the relay command and the MCP tool print the markers it looks for.
"""

import re
from typing import Final

RELAY_RECEIPT_MARKER: Final = "t3-relay-receipt:"
RELAY_REFUSED_MARKER: Final = "t3-relay-refused:"

_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:{re.escape(RELAY_RECEIPT_MARKER)}|{re.escape(RELAY_REFUSED_MARKER)})", re.MULTILINE
)


def has_relay_marker(text: str) -> bool:
    """Whether *text* carries a relay receipt or a relay refusal."""
    return _MARKER_RE.search(text) is not None
