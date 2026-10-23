"""``t3 slack listen`` hands the receiver the live record-then-wake callback and the catch-up hook."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.slack import listen
from teatree.loops.slack_inbound import catch_up_on_connect, record_and_wake_slack_answer


def test_listen_passes_the_catch_up_hook_to_the_receiver() -> None:
    with (
        patch.object(listen, "_resolve_overlays", return_value=[("demo", "xapp", "xoxb")]),
        patch.object(listen, "run_listener") as run_listener,
    ):
        result = CliRunner().invoke(listen.slack_app, ["listen"])

    assert result.exit_code == 0, result.output
    kwargs = run_listener.call_args.kwargs
    assert (kwargs["on_event"], kwargs["on_connect"]) == (record_and_wake_slack_answer, catch_up_on_connect)
