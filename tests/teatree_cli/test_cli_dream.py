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

    def test_compliance_show_delegates_to_management_command(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(dream_app, ["compliance", "show"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("dream", "compliance")


class TestDreamGapCommandsDelegate:
    def test_gap_coverage_passes_ticket_and_json(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(dream_app, ["gap-coverage", "--ticket", "905", "--json"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("dream", "gap-coverage", "--ticket", "905", "--json")

    def test_gap_coverage_propagates_a_non_zero_exit(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command", side_effect=SystemExit(1)),
        ):
            result = runner.invoke(dream_app, ["gap-coverage"])
        assert result.exit_code == 1

    def test_gap_disposition_passes_address_and_reject(self) -> None:
        with (
            patch("teatree.cli.dream.ensure_django"),
            patch("django.core.management.call_command") as call_mock,
        ):
            address = runner.invoke(dream_app, ["gap-disposition", "905", "abc", "--citation", "task 5561"])
            reject = runner.invoke(dream_app, ["gap-disposition", "905", "abc", "--reject", "not a core gap"])
        assert (address.exit_code, reject.exit_code) == (0, 0)
        prefix = ("dream", "gap-disposition", "905", "abc")
        assert call_mock.call_args_list[0].args == (*prefix, "--citation", "task 5561")
        assert call_mock.call_args_list[1].args == (*prefix, "--reject", "not a core gap")
