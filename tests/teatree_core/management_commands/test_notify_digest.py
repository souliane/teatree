"""The pulled surface, and the exit code a withheld signal must NOT carry (#4524)."""

import io
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.notify_types import NotifyReason


def _pull(key: str, text: str) -> None:
    notify_user(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=key,
        audience=NotifyAudience.OWNER_ESCALATION,
        user_id="U_ME",
    )


class TestNotifyDigest(TestCase):
    def test_it_reports_every_pulled_signal_grouped_by_kind(self) -> None:
        _pull("pr_sweep_aged_skip:souliane/teatree#1:a", "PR 1 skipped, reason ci_red")
        _pull("pr_sweep_aged_skip:souliane/teatree#2:b", "PR 2 skipped, reason ci_pending")
        _pull("watchdog:red:deadbeef:0:20260819", "watchdog found red findings on the box")

        out = io.StringIO()
        call_command("notify", "digest", stdout=out)
        rendered = out.getvalue()

        assert "pr_sweep_aged_skip" in rendered
        assert "watchdog" in rendered
        assert "2" in rendered

    def test_nothing_pulled_is_reported_as_nothing_not_as_an_error(self) -> None:
        out = io.StringIO()
        call_command("notify", "digest", stdout=out)
        assert "no status signals" in out.getvalue()

    def test_a_signal_outside_the_window_is_not_reported(self) -> None:
        _pull("pr_sweep_aged_skip:souliane/teatree#3:c", "PR 3 skipped")
        BotPing.objects.filter(status=BotPing.Status.PULLED).update(posted_at="2020-01-01T00:00:00Z")

        out = io.StringIO()
        call_command("notify", "digest", stdout=out)
        assert "no status signals" in out.getvalue()

    def test_a_non_positive_window_is_refused(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("notify", "digest", "--since-hours", "0", stdout=io.StringIO())
        assert exc.value.code == 2


class TestWithheldSignalExitsZero(TestCase):
    def test_a_pulled_status_signal_does_not_look_like_a_transport_failure(self) -> None:
        """`deploy/watchdog.sh` parks and re-sends any page whose send exits non-zero."""
        out, err = io.StringIO(), io.StringIO()

        call_command(
            "notify",
            "send",
            "watchdog found red findings on the box",
            "--idempotency-key",
            "watchdog:red:deadbeef:0:20260819",
            stdout=out,
            stderr=err,
        )

        assert "recorded, not DM'd" in out.getvalue()
        assert "withheld from the DM channel" in err.getvalue()

    def test_a_genuine_failure_still_exits_non_zero(self) -> None:
        with (
            patch(
                "teatree.core.notify.resolve_owner_dm_backend",
                return_value=(None, NotifyReason.NO_MESSAGING_BACKEND),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            call_command(
                "notify",
                "send",
                "the compose stack is down",
                "--idempotency-key",
                "watchdog:compose-up-failed:20260819",
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        assert exc.value.code == 1
