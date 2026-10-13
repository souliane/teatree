"""The listener records an inbound DM BEFORE it wakes the answer cycle.

The wake was already wired, but the cycle it wakes reads
``PendingChatInjection.loop_unreplied()``, whose only writer was the ~60s inbox
sweep. So the wake fired ~1s after arrival, found no row, and stopped without
re-arming — the answer then waited out the sweep plus the 5m fallback. The
callback now records the row through the sweep's own ``record_events`` seam
first, so the woken cycle has something to read.
"""

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.slack import listen
from teatree.loops.slack_inbound import record_and_wake_slack_answer
from teatree.types import RawAPIDict


@dataclass
class FakeMessaging:
    """A messaging backend that records every outbound call it is asked to make."""

    outbound: list[str] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        self.outbound.append("post_message")
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        self.outbound.append("post_reply")
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return ""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        self.outbound.append("react")
        return {}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": "U_BOT_SELF", "bot_id": "B_BOT_SELF"}


_TAP = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "D1"}, "message": {"ts": "1.0"}, "actions": []}


class TestTheClickHandler:
    def test_listen_wires_the_click_handler(self, tmp_path: Path) -> None:
        with (
            patch.object(listen, "default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch.object(listen, "_resolve_overlays", return_value=[("ov", "xapp", "xoxb")]),
            patch.object(listen, "run_listener") as run,
        ):
            result = CliRunner().invoke(listen.slack_app, ["listen"])

        assert result.exit_code == 0
        assert run.call_args.kwargs["on_action"] is listen._answer_click
        assert run.call_args.kwargs["on_event"] is record_and_wake_slack_answer

    def test_a_tap_goes_to_the_binder_with_the_overlays_backend(self) -> None:
        backend = FakeMessaging()
        with (
            patch.object(listen, "messaging_from_overlay", return_value=backend),
            patch("teatree.loop.question_binding.answer_from_click") as click,
        ):
            listen._answer_click("demo", _TAP)

        click.assert_called_once_with(_TAP, backend=backend)

    def test_a_tap_with_no_backend_is_dropped_not_raised(self) -> None:
        with (
            patch.object(listen, "messaging_from_overlay", return_value=None),
            patch("teatree.loop.question_binding.answer_from_click") as click,
        ):
            listen._answer_click("demo", _TAP)

        click.assert_not_called()
