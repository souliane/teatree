"""``t3 dream`` CLI delegation tests (#1933).

The Typer subapp is thin: ``run`` / ``tick`` bootstrap Django and delegate to
the ``dream`` management command via ``call_command``. The cron mechanics
(lease, cadence, marker) live in the management command and are tested there.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.dream import dream_app

runner = CliRunner()


class TestDreamCliDelegation:
    def test_run_delegates_to_management_command(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(dream_app, ["run"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("dream", "run")

    def test_run_passes_dry_run_and_since(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(dream_app, ["run", "--dry-run", "--since", "2026-06-01T00:00:00+00:00"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("dream", "run", "--dry-run", "--since", "2026-06-01T00:00:00+00:00")

    def test_run_passes_full(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(dream_app, ["run", "--full"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("dream", "run", "--full")

    def test_tick_delegates_to_management_command(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(dream_app, ["tick"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("dream", "tick")
