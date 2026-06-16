"""``t3 <overlay> speed`` — show / set the throughput dial.

``speed`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a GLOBAL-scope DB ROW — a value in
``[teatree]`` TOML is ignored on read. Integration-first: the ``set`` write is
asserted on the persisted ``ConfigSetting`` row / the resolver, exercised
through the typer ``CliRunner`` against the same ``speed`` subgroup the overlay
app builder attaches via :func:`teatree.cli.speed.register_speed_commands`.
"""

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.speed import register_speed_commands
from teatree.config import Speed, get_effective_settings
from teatree.core.models import ConfigSetting

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register_speed_commands(app)
    return app


@pytest.mark.django_db
class TestSpeedSet:
    def test_set_writes_global_speed_row(self) -> None:
        result = runner.invoke(_app(), ["speed", "set", "full"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("speed") == Speed.FULL.value
        assert "full" in result.stdout

    def test_set_upserts_over_existing_row(self) -> None:
        ConfigSetting.objects.set_value("speed", Speed.MEDIUM.value)
        result = runner.invoke(_app(), ["speed", "set", "boost"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("speed") == Speed.BOOST.value

    def test_set_alias_is_normalised_to_canonical(self) -> None:
        result = runner.invoke(_app(), ["speed", "set", "high"])
        assert result.exit_code == 0
        # The canonical value is persisted, not the alias.
        assert ConfigSetting.objects.get_effective("speed") == Speed.FULL.value

    def test_set_round_trips_through_resolver(self) -> None:
        runner.invoke(_app(), ["speed", "set", "slow"])
        assert get_effective_settings().speed is Speed.SLOW

    def test_set_typo_is_rejected_and_writes_nothing(self) -> None:
        result = runner.invoke(_app(), ["speed", "set", "ludicrous"])
        assert result.exit_code == 1
        assert ConfigSetting.objects.count() == 0


@pytest.mark.django_db
class TestSpeedShow:
    def test_show_reports_effective_value(self) -> None:
        ConfigSetting.objects.set_value("speed", Speed.FULL.value)
        result = runner.invoke(_app(), ["speed", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Speed.FULL.value

    def test_show_defaults_to_medium_when_unset(self) -> None:
        result = runner.invoke(_app(), ["speed", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Speed.MEDIUM.value

    def test_show_is_read_only(self) -> None:
        ConfigSetting.objects.set_value("speed", Speed.SLOW.value)
        runner.invoke(_app(), ["speed", "show"])
        # ``show`` is a pure resolver read — no row is written or cleared.
        assert ConfigSetting.objects.count() == 1
        assert ConfigSetting.objects.get_effective("speed") == Speed.SLOW.value
