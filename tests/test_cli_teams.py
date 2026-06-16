"""``t3 teams`` — on / off / status for the agent-teams master switch.

``teams_enabled`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``on`` / ``off`` write a GLOBAL-scope DB ROW — a value
in the ``[teams]`` TOML table is ignored on read. Integration-first: the write is
asserted on the persisted ``ConfigSetting`` row / the resolver, exercised through
the typer ``CliRunner`` against the same ``teams`` app the root CLI registers via
:data:`teatree.cli.teams.teams_app`.
"""

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.teams import teams_app
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting

runner = CliRunner()


class TestTeamsOff(TestCase):
    def test_off_writes_teams_enabled_false(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=True)
        result = runner.invoke(teams_app, ["off"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("teams_enabled") is False

    def test_off_creates_row_when_absent(self) -> None:
        result = runner.invoke(teams_app, ["off"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("teams_enabled") is False


class TestTeamsOn(TestCase):
    def test_on_writes_teams_enabled_true(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=False)
        result = runner.invoke(teams_app, ["on"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("teams_enabled") is True

    def test_on_leaves_other_rows_untouched(self) -> None:
        ConfigSetting.objects.set_value("speed", "full")
        runner.invoke(teams_app, ["on"])
        assert ConfigSetting.objects.get_effective("teams_enabled") is True
        # An unrelated row is a distinct ``(scope, key)`` and is preserved.
        assert ConfigSetting.objects.get_effective("speed") == "full"


class TestTeamsStatus(TestCase):
    def test_status_reflects_enabled(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=True)
        result = runner.invoke(teams_app, ["status"])
        assert result.exit_code == 0
        assert "on" in result.stdout.lower()

    def test_status_reflects_disabled_with_classic_note(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=False)
        result = runner.invoke(teams_app, ["status"])
        assert result.exit_code == 0
        lowered = result.stdout.lower()
        assert "off" in lowered
        assert "classic" in lowered
        assert "sub-agent" in lowered

    def test_status_defaults_to_off_when_unset(self) -> None:
        result = runner.invoke(teams_app, ["status"])
        assert result.exit_code == 0
        assert "off" in result.stdout.lower()

    def test_status_is_read_only(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=True)
        runner.invoke(teams_app, ["status"])
        # ``status`` is a pure resolver read — no row is written or cleared.
        assert ConfigSetting.objects.count() == 1
        assert ConfigSetting.objects.get_effective("teams_enabled") is True


class TestStatusReflectsRoundTrip(TestCase):
    def test_off_then_status_reports_off(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=True)
        assert runner.invoke(teams_app, ["off"]).exit_code == 0
        status = runner.invoke(teams_app, ["status"])
        assert status.exit_code == 0
        assert "off" in status.stdout.lower()
        assert get_effective_settings().teams_enabled is False

    def test_on_then_status_reports_on(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=False)
        assert runner.invoke(teams_app, ["on"]).exit_code == 0
        status = runner.invoke(teams_app, ["status"])
        assert status.exit_code == 0
        assert "on" in status.stdout.lower()
        assert get_effective_settings().teams_enabled is True
