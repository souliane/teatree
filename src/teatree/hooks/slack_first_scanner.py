"""Slack-first detector: an owner prompt in an attended session that never went through the relay.

Pure and Django-free — the Stop handler (``hooks/scripts/slack_first_gate.py``) feeds it the owner's words
and the final turn's tool results; the relay command and the MCP tool print the markers it looks for.
"""

import re
from typing import Final, NamedTuple

RELAY_RECEIPT_MARKER: Final = "t3-relay-receipt:"
RELAY_REFUSED_MARKER: Final = "t3-relay-refused:"

_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:{re.escape(RELAY_RECEIPT_MARKER)}|{re.escape(RELAY_REFUSED_MARKER)})", re.MULTILINE
)


_PROMPT_PREVIEW_CHARS: Final = 120


class SlackFirstVerdict(NamedTuple):
    """The owner prompt that was answered in the terminal although factory Slack is live."""

    prompt: str


def has_relay_marker(text: str) -> bool:
    """Whether *text* carries a relay receipt or a relay refusal."""
    return _MARKER_RE.search(text) is not None


def find_unrelayed_prompt(owner_words: str, turn_tool_results: str, *, slack_live: bool) -> SlackFirstVerdict | None:
    """A verdict to BLOCK, or ``None`` to allow.

    Fires only on the full conjunction: factory Slack is live, the owner said something, and the final
    turn's tool results carry neither a relay receipt nor a relay refusal. Any leg missing allows.
    """
    words = " ".join(owner_words.split())
    if not slack_live or not words or has_relay_marker(turn_tool_results):
        return None
    return SlackFirstVerdict(prompt=words[:_PROMPT_PREVIEW_CHARS])


def format_block_message(verdict: SlackFirstVerdict) -> str:
    """The BLOCK reason: what was not relayed, how to relay it, and the ways out."""
    return (
        "SLACK-FIRST GATE — factory Slack is live, so the owner's prompt goes to their factory DM, not an "
        f'answer here.\n  prompt: "{verdict.prompt}"\n'
        "Relay it verbatim: `t3 <overlay> pending_chat relay --relay-id <new id> --text-file <file holding "
        "the prompt>` or the MCP tool `mcp__teatree__pending_chat_relay`. Then end the turn with only the "
        f"permalink from its {RELAY_RECEIPT_MARKER} line. Exit 3 or 4, or a {RELAY_REFUSED_MARKER} line, "
        "means answer in the terminal with a one-line reason. Not a prompt for the factory (a harness "
        "command, an attachment, or the owner said to answer here)? End the turn with "
        "[skip-slack-first: <reason>]."
    )
