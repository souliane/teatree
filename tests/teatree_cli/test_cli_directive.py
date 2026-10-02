"""``t3 directive`` CLI delegation tests (north-star PR-6 + PR-7).

The Typer subapp is thin: each verb bootstraps Django and delegates to the
``directive`` management command via ``call_command``. The cron mechanics (lease,
cadence gate) and the guarded FSM live in the management command and are tested there.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.directive import directive_app

runner = CliRunner()


class TestDirectiveCliDelegation:
    def test_capture_delegates_with_scope(self) -> None:
        with (
            patch("teatree.cli.directive.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(directive_app, ["capture", "draft MRs for X", "--scope", "t3-teatree"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("directive", "capture", "draft MRs for X", scope="t3-teatree")

    def test_tick_delegates(self) -> None:
        with (
            patch("teatree.cli.directive.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(directive_app, ["tick"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("directive", "tick")

    def test_status_delegates(self) -> None:
        with (
            patch("teatree.cli.directive.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(directive_app, ["status", "3"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("directive", "status", 3)

    def test_list_delegates_with_limit(self) -> None:
        with (
            patch("teatree.cli.directive.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(directive_app, ["list", "--limit", "5"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("directive", "list", limit=5)

    def test_resolve_revert_delegates(self) -> None:
        with (
            patch("teatree.cli.directive.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(directive_app, ["resolve-revert", "7", "--revert-sha", "cafe"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("directive", "resolve-revert", 7, revert_sha="cafe")

    def test_history_delegates_with_limit(self) -> None:
        with (
            patch("teatree.cli.directive.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(directive_app, ["history", "--limit", "5"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("directive", "history", limit=5)
