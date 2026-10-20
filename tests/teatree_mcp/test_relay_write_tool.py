"""``pending_chat_relay`` over MCP reaches the same relay seam as ``t3 <overlay> pending-chat relay``."""

from typing import Any
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.core.models import PendingChatInjection
from teatree.mcp.server import build_server
from tests._factory_slack import FakeOwnerDm, live_factory_slack
from tests.teatree_mcp._call_tool_result import payloads

PROMPT = "  merge it?\nsecond line  \n"


def _relay(**args: str) -> Any:
    return payloads(async_to_sync(build_server().call_tool)("pending_chat_relay", args))[0]


class TestPendingChatRelayTool(TestCase):
    def test_a_live_relay_returns_the_receipt_and_writes_the_same_row_as_the_cli(self) -> None:
        dm = FakeOwnerDm()
        with live_factory_slack(dm), patch("teatree.core.management.commands.pending_chat.wake_slack_answer"):
            result = _relay(text=PROMPT, relay_id="m1", session_id="sess-mcp")

        row = PendingChatInjection.objects.get()
        [post] = dm.posts
        assert result["status"] == "relayed"
        assert result["marker"] == (
            f"t3-relay-receipt: pending_chat={row.pk} ts={post['ts']} permalink={result['permalink']}"
        )
        assert (row.text, row.relayed_by_session, row.slack_ts) == (PROMPT, "sess-mcp", post["ts"])

    def test_a_relay_the_terminal_must_answer_is_a_structured_refusal_not_a_tool_error(self) -> None:
        result = _relay(text=PROMPT, relay_id="m1")

        assert result["status"] == "refused"
        assert result["marker"].startswith("t3-relay-refused: slack_not_live")
        assert PendingChatInjection.objects.count() == 0

    def test_a_failed_post_is_reported_as_a_delivery_failure(self) -> None:
        with live_factory_slack(FakeOwnerDm(fail_post=True)):
            result = _relay(text=PROMPT, relay_id="m1")

        assert result["status"] == "delivery_failed"
        assert PendingChatInjection.objects.count() == 0


@pytest.mark.parametrize("name", ["pending_chat_relay"])
def test_the_tool_is_registered_with_a_declared_seam(name: str) -> None:
    from teatree.mcp.write_tools import TOOL_SEAMS  # noqa: PLC0415 — keeps the import beside its only use

    assert TOOL_SEAMS[name]
