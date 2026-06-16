"""Tests for ``t3 <overlay> notify send`` management command (#1030).

Wraps :func:`teatree.notify.notify_user` so sub-agent identities can DM
the user directly from the shell instead of relaying through the parent
turn. Only the unstoppable Slack HTTP boundary
(:func:`messaging_from_overlay`) is mocked — the rest of the notify path
runs for real.
"""

import os
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import CommandError, call_command

from teatree.core.models import BotPing

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


def _call(*args: str) -> tuple[str, int]:
    buf = StringIO()
    code = 0
    try:
        call_command(*args, stdout=buf)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return buf.getvalue(), code


class TestNotifySendSubcommand:
    def test_send_invokes_notify_path_and_records_audit(self) -> None:
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            out, code = _call(
                "notify",
                "send",
                "PR #1016 merged.",
                "--user-id",
                "U_ME",
                "--kind",
                "info",
                "--idempotency-key",
                "session=s;turn=1",
            )

        assert code == 0
        backend.open_dm.assert_called_once_with("U_ME")
        backend.post_message.assert_called_once()
        text = backend.post_message.call_args.kwargs["text"]
        assert "PR #1016 merged." in text
        row = BotPing.objects.get(idempotency_key="session=s;turn=1")
        assert row.status == BotPing.Status.SENT
        assert row.kind == BotPing.Kind.INFO
        assert "sent" in out.lower()

    def test_overlay_flag_sets_env_for_bot_routing(self) -> None:
        backend = _backend()
        seen: dict[str, str] = {}

        def _capture() -> MagicMock:
            seen["overlay"] = os.environ.get("T3_OVERLAY_NAME", "")
            return backend

        with patch("teatree.core.notify.messaging_from_overlay", side_effect=_capture):
            _call(
                "notify",
                "send",
                "routed",
                "--user-id",
                "U_ME",
                "--kind",
                "info",
                "--idempotency-key",
                "k-overlay",
                "--overlay",
                "teatree",
            )

        assert seen["overlay"] == "teatree"

    def test_body_dash_reads_stdin(self) -> None:
        backend = _backend()
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=backend),
            patch("sys.stdin", StringIO("piped *mrkdwn* body")),
        ):
            _out, code = _call(
                "notify",
                "send",
                "-",
                "--user-id",
                "U_ME",
                "--kind",
                "info",
                "--idempotency-key",
                "k-stdin",
            )

        assert code == 0
        assert "piped *mrkdwn* body" in backend.post_message.call_args.kwargs["text"]

    def test_failed_delivery_exits_one(self) -> None:
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            _out, code = _call(
                "notify",
                "send",
                "no backend",
                "--user-id",
                "U_ME",
                "--kind",
                "info",
                "--idempotency-key",
                "k-fail",
            )

        assert code == 1
        assert BotPing.objects.get(idempotency_key="k-fail").status == BotPing.Status.NOOP

    def test_failed_delivery_surfaces_recorded_reason_on_stderr(self) -> None:
        """rc=1 carries *why* delivery failed, not a bare key (#1181)."""
        err = StringIO()
        code = 0
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            try:
                call_command(
                    "notify",
                    "send",
                    "no backend",
                    "--user-id",
                    "U_ME",
                    "--kind",
                    "info",
                    "--idempotency-key",
                    "k-reason",
                    stderr=err,
                )
            except SystemExit as exc:
                code = int(exc.code or 0)

        assert code == 1
        message = err.getvalue()
        assert "k-reason" in message
        # The NOOP reason recorded on the BotPing row is echoed verbatim.
        assert "no messaging backend or user_id configured" in message

    def test_missing_idempotency_key_is_required(self) -> None:
        with pytest.raises((SystemExit, CommandError)):
            _call("notify", "send", "body", "--user-id", "U_ME", "--kind", "info")

    def test_blank_idempotency_key_exits_two(self) -> None:
        _out, code = _call(
            "notify",
            "send",
            "body",
            "--user-id",
            "U_ME",
            "--kind",
            "info",
            "--idempotency-key",
            "   ",
        )

        assert code == 2

    def test_unknown_kind_exits_two(self) -> None:
        _out, code = _call(
            "notify",
            "send",
            "body",
            "--user-id",
            "U_ME",
            "--kind",
            "bogus",
            "--idempotency-key",
            "k-kind",
        )

        assert code == 2

    def test_overlay_flag_restores_previous_env(self) -> None:
        backend = _backend()
        os.environ["T3_OVERLAY_NAME"] = "pre-existing"
        try:
            with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
                _call(
                    "notify",
                    "send",
                    "routed",
                    "--user-id",
                    "U_ME",
                    "--kind",
                    "info",
                    "--idempotency-key",
                    "k-restore-prev",
                    "--overlay",
                    "teatree",
                )
            assert os.environ["T3_OVERLAY_NAME"] == "pre-existing"
        finally:
            os.environ.pop("T3_OVERLAY_NAME", None)

    def test_overlay_flag_restores_unset_env(self) -> None:
        backend = _backend()
        os.environ.pop("T3_OVERLAY_NAME", None)
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            _call(
                "notify",
                "send",
                "routed",
                "--user-id",
                "U_ME",
                "--kind",
                "info",
                "--idempotency-key",
                "k-restore",
                "--overlay",
                "teatree",
            )

        assert "T3_OVERLAY_NAME" not in os.environ

    def test_empty_body_exits_non_zero(self) -> None:
        _out, code = _call(
            "notify",
            "send",
            "   ",
            "--user-id",
            "U_ME",
            "--kind",
            "info",
            "--idempotency-key",
            "k-empty",
        )

        assert code == 2
