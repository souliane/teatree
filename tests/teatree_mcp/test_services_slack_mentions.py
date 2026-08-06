"""The Slack mentions MCP tool refuses rather than answering "you were not mentioned".

The Socket-Mode receiver writes inbound mentions to a JSONL the loop's scanner
drains in its own process; nothing fills the in-memory queue an MCP server holds.
So an empty read here has exactly one meaning — "this surface cannot see
mentions" — and returning ``[]`` reported the opposite fact. The sibling
``slack_channel_history`` was hardened the same way for the same reason.
"""

import asyncio
from unittest.mock import patch

import pytest

from teatree.mcp.services_slack import MentionQueueUnreadableError, _slack_mentions
from teatree.types import RawAPIDict


class _StubBackend:
    def __init__(self, mentions: list[RawAPIDict]) -> None:
        self._mentions = mentions

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return list(self._mentions)


def _call(backend: _StubBackend) -> list[RawAPIDict]:
    with patch("teatree.mcp.services_slack._client", return_value=backend):
        return asyncio.run(_slack_mentions())


def test_an_empty_queue_is_refused_never_reported_as_no_mentions() -> None:
    with pytest.raises(MentionQueueUnreadableError, match="Cannot read mentions"):
        _call(_StubBackend([]))


def test_the_refusal_names_where_mentions_actually_live() -> None:
    # An agent that cannot act on the answer must at least be told where to look.
    with pytest.raises(MentionQueueUnreadableError) as excinfo:
        _call(_StubBackend([]))
    assert "slack-events.jsonl" in str(excinfo.value)


def test_a_populated_queue_still_answers() -> None:
    # The anti-vacuity control: the tool refuses ONLY the ambiguous empty, so a
    # deployment whose queue is genuinely filled keeps working.
    mentions: list[RawAPIDict] = [{"text": "please review", "ts": "1.0"}]
    assert _call(_StubBackend(mentions)) == mentions
