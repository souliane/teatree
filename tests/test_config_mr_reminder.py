"""``MrReminderConfig`` parsing + ``[mr_reminder]`` resolution (TODO-276).

The schema: a top-level ``[mr_reminder]`` table with an ordered
``[mr_reminder.channels]`` slug→channel sub-table and an optional
``default_channel`` fallback. Covers defaults when absent, a full parse
with order preserved, partial keys, and malformed-input degradation.
``load_config`` round-trips it from a real ``tmp_path`` toml file so the
loader wiring is exercised, not just the parser.
"""

from pathlib import Path

from teatree.config import load_config
from teatree.config_mr_reminder import MrReminderConfig, mr_reminder_from_table, resolve_mr_reminder


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


class TestLoadConfigRoundTrip:
    def test_load_config_populates_mr_reminder(self, tmp_path: Path) -> None:
        toml = tmp_path / ".teatree.toml"
        toml.write_text(
            """\
[mr_reminder]
default_channel = "C_FALLBACK"
[mr_reminder.channels]
"souliane/teatree" = "C_TEATREE"
"acme" = "C_ACME"
""",
        )
        user = load_config(toml).user
        assert user.mr_reminder.default_channel == "C_FALLBACK"
        assert user.mr_reminder.channels == (("souliane/teatree", "C_TEATREE"), ("acme", "C_ACME"))

    def test_load_config_without_table_keeps_default(self, tmp_path: Path) -> None:
        toml = tmp_path / ".teatree.toml"
        toml.write_text('[teatree]\nbranch_prefix = "ac"\n')
        assert load_config(toml).user.mr_reminder == MrReminderConfig()
