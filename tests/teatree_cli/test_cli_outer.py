"""``t3 outer`` CLI delegation tests (T4-PR-3).

The Typer subapp is thin: each verb bootstraps Django and delegates to the
``outer`` management command via ``call_command``. The cron mechanics (lease,
cadence gate) and the guarded FSM live in the management command and are tested
there.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.outer import outer_app

runner = CliRunner()


class TestOuterCliDelegation:
    def test_tick_delegates(self) -> None:
        with (
            patch("teatree.cli.outer.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(outer_app, ["tick"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("outer", "tick")

    def test_status_delegates(self) -> None:
        with (
            patch("teatree.cli.outer.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(outer_app, ["status"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("outer", "status")

    def test_propose_delegates_with_options(self) -> None:
        with (
            patch("teatree.cli.outer.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(outer_app, ["propose", "--hypothesis", "H", "--target", "review_catch"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("outer", "propose", hypothesis="H", target="review_catch")

    def test_history_delegates_with_limit(self) -> None:
        with (
            patch("teatree.cli.outer.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(outer_app, ["history", "--limit", "5"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("outer", "history", limit=5)

    def test_resolve_revert_delegates(self) -> None:
        with (
            patch("teatree.cli.outer.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(outer_app, ["resolve-revert", "7", "--revert-sha", "cafe"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("outer", "resolve-revert", 7, revert_sha="cafe")

    def test_resolve_keep_delegates(self) -> None:
        with (
            patch("teatree.cli.outer.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(outer_app, ["resolve-keep", "7"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("outer", "resolve-keep", 7)
