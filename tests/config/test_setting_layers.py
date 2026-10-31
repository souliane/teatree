# test-path: cross-cutting
"""The tier LAYERS of one settings resolution — folding, and the home filter.

``resolution`` owns the resolution ORDER; this module owns what ONE layer is and how
a layer folds onto the one below it. Pure fold logic over plain dicts, so it is tested
directly: the resolver-level integration (which layer comes from where) is
``test_declared_default_base`` / ``test_db_config_tier``.
"""

import logging

import pytest

from teatree.config.setting_layers import SettingLayers, apply_structured_settings, drop_db_home_overlay_keys, toml_home
from teatree.config.settings import UserSettings


class TestApplyStructuredSettings:
    """``mr_reminder`` takes the highest layer."""

    def test_no_layer_carries_a_table_leaves_the_defaults(self) -> None:
        base = UserSettings()
        assert apply_structured_settings(base, ({}, {}, {})) == base

    def test_mr_reminder_takes_the_highest_layer_with_no_merge(self) -> None:
        base = UserSettings()
        layers = (
            {"mr_reminder": {"default_channel": "from-toml", "channels": {"a": "x"}}},
            {"mr_reminder": {"default_channel": "from-global"}},
            {},
        )
        reminder = apply_structured_settings(base, layers).mr_reminder
        assert reminder.default_channel == "from-global"
        assert reminder.channels == ()  # highest layer wins WHOLE — no merge with the layer below

    def test_a_non_dict_value_is_ignored(self) -> None:
        base = UserSettings()
        settings = apply_structured_settings(base, ({"mr_reminder": []}, {}, {}))
        assert settings == base


class TestSettingLayers:
    def test_separates_the_raw_db_rows_from_the_coerced_scopes(self) -> None:
        layers = SettingLayers(({"b": 2}, {}), {"b": 2}, {})
        assert layers.db_rows == ({"b": 2}, {})
        assert layers.global_db == {"b": 2}
        assert layers.overlay_db == {}


class TestHomeFilter:
    """The #1775 carve-out is EMPTY, so every DB-home override key is dropped — loudly."""

    def test_no_live_key_is_toml_home(self) -> None:
        assert not any(toml_home(key) for key in vars(UserSettings()))

    def test_db_home_key_is_dropped_and_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="teatree.config"):
            assert drop_db_home_overlay_keys({"mode": "auto"}, "proj") == {}
        assert "mode" in caplog.text
        assert "proj" in caplog.text

    def test_unknown_key_is_dropped_without_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        # A stray key is a different concern — only a genuine DB-home field is flagged.
        with caplog.at_level(logging.WARNING, logger="teatree.config"):
            assert drop_db_home_overlay_keys({"not_a_setting": 1}, "proj") == {}
        assert caplog.text == ""
