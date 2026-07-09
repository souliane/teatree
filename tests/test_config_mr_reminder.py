"""``MrReminderConfig`` parsing + DB-home ``mr_reminder`` resolution (TODO-276).

The schema: an ordered ``channels`` slug→channel map and an optional
``default_channel`` fallback. Covers defaults when absent, a full parse with order
preserved, partial keys, and malformed-input degradation. ``mr_reminder`` is
DB-home (legacy file tier removed) — it resolves from a JSON-dict ``ConfigSetting``
row (rebuilt bespoke via ``mr_reminder_from_table``), so the resolver wiring is
exercised end-to-end, not just the parser.
"""

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.config_mr_reminder import MrReminderConfig, mr_reminder_from_table, resolve_mr_reminder
from teatree.core.models import ConfigSetting


class TestMrReminderFromTable:
    def test_empty_table_yields_defaults(self) -> None:
        cfg = mr_reminder_from_table({})
        assert cfg == MrReminderConfig()
        assert cfg.channels == ()
        assert cfg.default_channel == ""

    def test_parses_channels_and_default(self) -> None:
        cfg = mr_reminder_from_table(
            {
                "default_channel": "C_FALLBACK",
                "channels": {"souliane/teatree": "C_TEATREE", "acme-engineering": "C_ACME"},
            },
        )
        assert cfg.default_channel == "C_FALLBACK"
        assert cfg.channels == (("souliane/teatree", "C_TEATREE"), ("acme-engineering", "C_ACME"))

    def test_preserves_channel_insertion_order(self) -> None:
        cfg = mr_reminder_from_table({"channels": {"z/z": "C_Z", "a/a": "C_A", "m/m": "C_M"}})
        assert [slug for slug, _ in cfg.channels] == ["z/z", "a/a", "m/m"]

    def test_drops_blank_slug_and_blank_channel(self) -> None:
        cfg = mr_reminder_from_table({"channels": {"": "C_X", "a/b": "", "c/d": "C_OK"}})
        assert cfg.channels == (("c/d", "C_OK"),)

    def test_non_dict_channels_degrades_to_empty(self) -> None:
        assert mr_reminder_from_table({"channels": ["not", "a", "dict"]}).channels == ()

    def test_non_string_default_degrades_to_empty(self) -> None:
        assert mr_reminder_from_table({"default_channel": 123}).default_channel == ""


class TestResolveMrReminder:
    def test_absent_table_yields_defaults(self) -> None:
        assert resolve_mr_reminder({}) == MrReminderConfig()

    def test_non_dict_table_yields_defaults(self) -> None:
        assert resolve_mr_reminder({"mr_reminder": "oops"}) == MrReminderConfig()

    def test_reads_top_level_table(self) -> None:
        cfg = resolve_mr_reminder({"mr_reminder": {"channels": {"o/r": "C1"}}})
        assert cfg.channels == (("o/r", "C1"),)


class TestMrReminderDbResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_resolves_from_db_row(self) -> None:
        ConfigSetting.objects.set_value(
            "mr_reminder",
            value={
                "default_channel": "C_FALLBACK",
                "channels": {"souliane/teatree": "C_TEATREE", "acme": "C_ACME"},
            },
        )
        mr = get_effective_settings().mr_reminder
        assert mr.default_channel == "C_FALLBACK"
        assert mr.channels == (("souliane/teatree", "C_TEATREE"), ("acme", "C_ACME"))

    def test_no_row_keeps_default(self) -> None:
        assert get_effective_settings().mr_reminder == MrReminderConfig()
