import asyncio
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from teatree.agents.live_mailbox_client import (
    InteractiveMailboxLease,
    MailboxClient,
    MailboxError,
    NoLiveWorkerError,
    _joined_identity,
)


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


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (b"not json", "invalid data"),
        (b"[]", "invalid data"),
        (b'{"error":"denied","code":"permission_denied"}', "permission_denied: denied"),
        (b'{"result":{"address":"peer"}}', "invalid data"),
    ],
)
def test_join_rejects_invalid_or_refused_replies(response: bytes, message: str) -> None:
    with pytest.raises(MailboxError, match=message):
        _joined_identity(response)


def test_attended_join_reports_when_no_worker_is_live(tmp_path: Path) -> None:
    with pytest.raises(NoLiveWorkerError, match="No live worker accepted"):
        InteractiveMailboxLease.connect(tmp_path)


@pytest.mark.parametrize("failure_stage", ["connect", "after_reply"])
def test_attended_join_closes_socket_on_unexpected_error(tmp_path: Path, failure_stage: str) -> None:
    (tmp_path / "w-source.sock").touch()
    connected = Mock()
    if failure_stage == "connect":
        connected.connect.side_effect = RuntimeError("unexpected join error")
    else:
        connected.recv.return_value = b'{"result":{"address":"peer","token":"secret"}}\n'
        connected.settimeout.side_effect = [None, RuntimeError("unexpected join error")]
    with (
        patch("teatree.agents.live_mailbox_client.socket.socket", return_value=connected),
        patch("teatree.agents.live_mailbox_client._require_trusted_worker"),
        pytest.raises(RuntimeError, match="unexpected join error"),
    ):
        InteractiveMailboxLease.connect(tmp_path)
    connected.close.assert_called_once()
