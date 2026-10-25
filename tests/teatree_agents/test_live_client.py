"""Operator client request shape for live pause."""

from unittest.mock import patch

from teatree.agents.live_client import LiveClient


def test_pause_uses_the_fenced_operator_command() -> None:
    client = LiveClient()
    with patch.object(client, "_operator_command", return_value={"outcome": "paused"}) as send:
        receipt = client.pause(7, "pause", command_id="slack:one", wait=10)

    assert receipt["outcome"] == "paused"
    request = send.call_args.args[0]
    assert (request.method, request.task, request.text, request.command_id, request.wait) == (
        "live.pause",
        7,
        "pause",
        "slack:one",
        10,
    )
