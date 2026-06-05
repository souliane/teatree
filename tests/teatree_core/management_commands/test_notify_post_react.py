"""Tests for ``t3 <overlay> notify post`` / ``notify react`` (#1750).

The deterministic CLI for posting and for adding a reaction, both routed
by destination through ``SlackBotBackend.post_routed`` / ``react_routed``
(self-DM → bot, colleague/channel → ``xoxp``). Both check the Slack
``ok`` field and exit non-zero loudly on ``ok:false``; a ``missing_scope``
failure prints the remediation (which scope, add it to the user-OAuth app
and re-auth). The token-by-destination decision itself is covered at the
backend boundary in
``tests/teatree_backends/test_slack_post_react_routing.py``; here the CLI
is asserted to call the routed methods and to surface the Slack body's
``ok`` / ``error`` correctly.

Only the messaging backend (the Slack HTTP boundary) is mocked.
"""

import os
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def _call(*args: str) -> tuple[str, str, int]:
    out, err = StringIO(), StringIO()
    code = 0
    try:
        call_command(*args, stdout=out, stderr=err)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return out.getvalue(), err.getvalue(), code


class TestNotifyPost:
    def test_post_routes_via_user_token_and_exits_zero(self) -> None:
        backend = MagicMock()
        backend.post_routed.return_value = {"ok": True, "ts": "1700.0001"}
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            out, _err, code = _call("notify", "post", "--channel", "C_TEAM", "--text", "hi team")

        assert code == 0
        backend.post_routed.assert_called_once_with(channel="C_TEAM", text="hi team", thread_ts="")
        assert "1700.0001" in out

    def test_post_threads_when_thread_ts_given(self) -> None:
        backend = MagicMock()
        backend.post_routed.return_value = {"ok": True, "ts": "1700.9"}
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            _out, _err, code = _call(
                "notify", "post", "--channel", "C_TEAM", "--thread-ts", "1700.0001", "--text", "reply"
            )

        assert code == 0
        backend.post_routed.assert_called_once_with(channel="C_TEAM", text="reply", thread_ts="1700.0001")

    def test_post_self_dm_threaded_reply_lands_thread_ts_in_payload(self) -> None:
        # Self-DM reply (the user's own bot DM) is the ungated path; the
        # ``--thread-ts`` value must reach the chat.postMessage payload so a
        # threaded user-reply actually threads. Mock only the HTTP egress.
        from teatree.backends.slack_bot import SlackBotBackend  # noqa: PLC0415

        backend = SlackBotBackend(
            bot_token="xoxb-test",
            user_id="U_ME",
            dm_channel_id="D_ME",
        )
        with (
            patch.object(backend, "_post", return_value={"ok": True, "ts": "1.0"}) as post,
            patch(
                "teatree.core.management.commands.notify.messaging_from_overlay",
                return_value=backend,
            ),
        ):
            _out, _err, code = _call(
                "notify", "post", "--channel", "D_ME", "--thread-ts", "1780685008.488439", "--text", "reply"
            )

        assert code == 0
        payload = post.call_args.args[1]
        assert payload["thread_ts"] == "1780685008.488439"
        assert payload["channel"] == "D_ME"

    def test_post_text_dash_reads_stdin(self) -> None:
        backend = MagicMock()
        backend.post_routed.return_value = {"ok": True, "ts": "1.2"}
        with (
            patch(
                "teatree.core.management.commands.notify.messaging_from_overlay",
                return_value=backend,
            ),
            patch("sys.stdin", StringIO("piped body")),
        ):
            _out, _err, code = _call("notify", "post", "--channel", "C_TEAM", "--text", "-")

        assert code == 0
        assert backend.post_routed.call_args.kwargs["text"] == "piped body"

    def test_post_not_ok_exits_one_loudly(self) -> None:
        backend = MagicMock()
        backend.post_routed.return_value = {"ok": False, "error": "channel_not_found"}
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            _out, err, code = _call("notify", "post", "--channel", "C_GONE", "--text", "x")

        assert code == 1
        assert "channel_not_found" in err

    def test_post_no_backend_exits_one(self) -> None:
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=None,
        ):
            _out, err, code = _call("notify", "post", "--channel", "C_TEAM", "--text", "x")

        assert code == 1
        assert "no messaging backend" in err.lower()

    def test_post_empty_text_exits_two(self) -> None:
        _out, err, code = _call("notify", "post", "--channel", "C_TEAM", "--text", "   ")
        assert code == 2
        assert "text" in err.lower()

    def test_post_overlay_flag_sets_env(self) -> None:
        backend = MagicMock()
        backend.post_routed.return_value = {"ok": True, "ts": "1"}
        seen: dict[str, str] = {}

        def _capture(*_a: object, **_k: object) -> MagicMock:
            seen["overlay"] = os.environ.get("T3_OVERLAY_NAME", "")
            return backend

        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            side_effect=_capture,
        ):
            _call("notify", "post", "--channel", "C_TEAM", "--text", "x", "--overlay", "teatree")

        assert seen["overlay"] == "teatree"


class TestNotifyReact:
    def test_react_routes_via_user_token_and_exits_zero(self) -> None:
        backend = MagicMock()
        backend.react_routed.return_value = {"ok": True}
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            _out, _err, code = _call("notify", "react", "--channel", "C_TEAM", "--ts", "1700.0001", "--emoji", "eyes")

        assert code == 0
        backend.react_routed.assert_called_once_with(channel="C_TEAM", ts="1700.0001", emoji="eyes")

    def test_react_strips_colons_from_emoji(self) -> None:
        backend = MagicMock()
        backend.react_routed.return_value = {"ok": True}
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            _call("notify", "react", "--channel", "C_TEAM", "--ts", "1.2", "--emoji", ":eyes:")

        assert backend.react_routed.call_args.kwargs["emoji"] == "eyes"

    def test_react_missing_scope_prints_remediation_and_exits_one(self) -> None:
        backend = MagicMock()
        backend.react_routed.return_value = {"ok": False, "error": "missing_scope", "needed": "reactions:write"}
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            _out, err, code = _call("notify", "react", "--channel", "C_TEAM", "--ts", "1.2", "--emoji", "eyes")

        assert code == 1
        assert "reactions:write" in err
        assert "re-auth" in err.lower()
        assert "user-oauth" in err.lower()

    def test_react_not_ok_other_error_exits_one(self) -> None:
        backend = MagicMock()
        backend.react_routed.return_value = {"ok": False, "error": "already_reacted"}
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            _out, err, code = _call("notify", "react", "--channel", "C_TEAM", "--ts", "1.2", "--emoji", "eyes")

        assert code == 1
        assert "already_reacted" in err

    def test_react_no_backend_exits_one(self) -> None:
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=None,
        ):
            _out, err, code = _call("notify", "react", "--channel", "C_TEAM", "--ts", "1.2", "--emoji", "eyes")

        assert code == 1
        assert "no messaging backend" in err.lower()

    def test_react_empty_emoji_exits_two(self) -> None:
        _out, err, code = _call("notify", "react", "--channel", "C_TEAM", "--ts", "1.2", "--emoji", "  ")
        assert code == 2
        assert "emoji" in err.lower()

    def test_react_overlay_flag_restores_previous_env(self) -> None:
        backend = MagicMock()
        backend.react_routed.return_value = {"ok": True}
        os.environ["T3_OVERLAY_NAME"] = "pre-existing"
        try:
            with patch(
                "teatree.core.management.commands.notify.messaging_from_overlay",
                return_value=backend,
            ):
                _call(
                    "notify", "react", "--channel", "C_TEAM", "--ts", "1.2", "--emoji", "eyes", "--overlay", "teatree"
                )
            assert os.environ["T3_OVERLAY_NAME"] == "pre-existing"
        finally:
            os.environ.pop("T3_OVERLAY_NAME", None)

    def test_react_overlay_flag_restores_unset_env(self) -> None:
        backend = MagicMock()
        backend.react_routed.return_value = {"ok": True}
        os.environ.pop("T3_OVERLAY_NAME", None)
        with patch(
            "teatree.core.management.commands.notify.messaging_from_overlay",
            return_value=backend,
        ):
            _call("notify", "react", "--channel", "C_TEAM", "--ts", "1.2", "--emoji", "eyes", "--overlay", "teatree")

        assert "T3_OVERLAY_NAME" not in os.environ
