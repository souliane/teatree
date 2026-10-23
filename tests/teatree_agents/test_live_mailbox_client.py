import asyncio
from pathlib import Path

import pytest

from teatree.agents.live_mailbox_client import MailboxClient, MailboxError


def test_from_env_uses_the_supplied_identity_and_hides_its_token() -> None:
    client = MailboxClient.from_env(
        {"T3_AGENT_MAILBOX_SOCKET": "/tmp/mailbox.sock", "T3_AGENT_MAILBOX_TOKEN": "private-token"}
    )
    assert client is not None
    assert client.socket_path == Path("/tmp/mailbox.sock")
    assert client.token == "private-token"
    assert "private-token" not in repr(client)


def test_from_env_rejects_a_partial_identity() -> None:
    assert MailboxClient.from_env({"T3_AGENT_MAILBOX_SOCKET": "/tmp/mailbox.sock"}) is None
    assert MailboxClient.from_env({"T3_AGENT_MAILBOX_TOKEN": "private-token"}) is None


def test_neutral_client_reports_a_broker_failure_without_mcp_errors() -> None:
    client = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")

    async def read() -> object:
        return await client.peers()

    with pytest.raises(MailboxError, match="Live mailbox is unavailable"):
        asyncio.run(read())
