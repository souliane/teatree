# test-path: cross-cutting
"""The shipped-``defaults.toml`` DEFAULTS tier in the effective-settings chain.

``config/defaults.toml`` is the shipped code-default layer, so the resolver READS it.
It sits UNDER every override, so per key the chain is

    env -> DB(overlay) -> DB(global) -> overlay code default -> TOML default.

The tier is observable only against a shipped file whose value diverges from the
dataclass default, and the committed file deliberately carries no such divergence
(wiring the tier in is behaviour-neutral by construction). These tests therefore point
the tier at a real fixture ``defaults.toml`` — real stdlib parse, real registry
coercion, real ``ConfigSetting`` rows — and pin both that the TOML value wins over the
dataclass default AND that every override tier still beats it.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.config import (
    cold_defaults,
    effective_default,
    get_effective_settings,
    mr_reminder_from_table,
    speak_from_subtable,
)
from teatree.config.cold_defaults import DEFAULTS_TOML, default_for, shipped_defaults_table
from teatree.config.settings import UserSettings
from teatree.core.models import ConfigSetting

# Two scalars whose dataclass defaults (85 / 1) the fixture diverges from, plus a
# structured sub-table — the shapes the resolver treats differently. Every other key is
# absent, standing in for the Secret/Personal keys the shipped file omits.
_FIXTURE = """\
[teatree]
provision_ram_ceiling_percent = 42
merge_wip = 7

[teatree.speak]
local = "all"
"""


class TestTomlDefaultTier(TestCase):
    @pytest.fixture(autouse=True)
    def _fixture_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        monkeypatch.delenv("T3_MERGE_WIP", raising=False)
        toml = tmp_path / "defaults.toml"
        toml.write_text(_FIXTURE, encoding="utf-8")
        monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", toml)
        self.monkeypatch = monkeypatch

    def test_toml_value_wins_over_the_dataclass_default(self) -> None:
        assert ConfigSetting.objects.count() == 0
        assert UserSettings().provision_ram_ceiling_percent == 85
        assert get_effective_settings().provision_ram_ceiling_percent == 42

    def test_db_global_row_beats_the_toml_default(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 60)
        assert get_effective_settings().provision_ram_ceiling_percent == 60

    def test_db_overlay_row_beats_the_toml_default(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 55, scope="t3-teatree")
        assert get_effective_settings().provision_ram_ceiling_percent == 55

    def test_db_overlay_row_beats_a_db_global_row_over_the_toml_default(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 60)
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 55, scope="t3-teatree")
        assert get_effective_settings().provision_ram_ceiling_percent == 55

    def test_env_beats_a_db_row_over_the_toml_default(self) -> None:
        assert get_effective_settings().merge_wip == 7
        ConfigSetting.objects.set_value("merge_wip", 5)
        self.monkeypatch.setenv("T3_MERGE_WIP", "3")
        assert get_effective_settings().merge_wip == 3

    def test_key_absent_from_the_toml_falls_back_to_the_dataclass_default(self) -> None:
        # A Default-category key the file happens to omit, and a Personal key absent from
        # the shipped file by construction — neither raises, both keep the code default.
        settings = get_effective_settings()
        assert settings.session_stale_after_hours == UserSettings().session_stale_after_hours
        assert settings.openai_compatible_model == UserSettings().openai_compatible_model

    def test_structured_sub_table_resolves_from_the_toml_and_a_db_row_still_wins(self) -> None:
        assert get_effective_settings().speak.local == "all"
        ConfigSetting.objects.set_value("speak", {"local": "off"})
        assert get_effective_settings().speak.local == "off"

    def test_effective_default_reports_the_toml_value(self) -> None:
        # The seed-skip / import-skip authority agrees with the resolver, so a row equal
        # to the shipped default stays provably redundant.
        assert effective_default("provision_ram_ceiling_percent") == 42
        assert effective_default("session_stale_after_hours") == UserSettings().session_stale_after_hours


def test_the_tier_and_the_cold_reader_serve_the_same_shipped_file() -> None:
    # One path constant, one parse — the resolver's DEFAULTS tier and the cold hook
    # reader can never disagree about what the shipped default IS.
    assert DEFAULTS_TOML.name == "defaults.toml"
    table = shipped_defaults_table(DEFAULTS_TOML)
    assert default_for("provision_ram_ceiling_percent") == table["provision_ram_ceiling_percent"]
    assert effective_default("provision_ram_ceiling_percent") == table["provision_ram_ceiling_percent"]


def _shipped_effective(key: str, code: UserSettings) -> object:
    """The value the resolver derives for *key* from the committed file alone.

    The two structured fields arrive as sub-tables, so they resolve through the same
    parsers the resolver rebuilds them with rather than through ``effective_default``
    (which reports them in stored dict form on purpose).
    """
    table = shipped_defaults_table()[key]
    if key == "mr_reminder":
        return mr_reminder_from_table(table)
    if key == "speak":
        return speak_from_subtable(table, base=code.speak)
    return effective_default(key)


def test_committed_defaults_toml_moves_no_effective_default() -> None:
    """Every shipped value equals its in-code default, so reading the file changes nothing.

    Wiring the tier in must not silently move an effective default. A future generator
    run that adopts a live box value turns this red — the divergence then has to be a
    reviewed decision rather than a side effect of regenerating the file.
    """
    code = UserSettings()
    diverged = {
        key: (_shipped_effective(key, code), getattr(code, key))
        for key in cold_defaults.shipped_defaults_table()
        if hasattr(code, key) and _shipped_effective(key, code) != getattr(code, key)
    }
    assert not diverged, f"defaults.toml moves an effective default: {diverged}"
