"""Headless watchdog and ticket budget settings resolve through DB-home config."""

import dataclasses

import pytest
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings
from teatree.core.models import ConfigSetting

_WATCHDOG_BUDGET_FIELDS = (
    "watchdog_max_runtime_seconds",
    "watchdog_max_turns",
    "watchdog_max_cost_usd",
    "ticket_budget_max_cost_usd",
)


class TestFieldsAreRegistered:
    def test_each_is_a_user_settings_field(self) -> None:
        names = {f.name for f in dataclasses.fields(UserSettings)}
        for field in _WATCHDOG_BUDGET_FIELDS:
            assert field in names, f"{field} must be a UserSettings field (F9.5)"

    def test_each_has_a_db_home_parser(self) -> None:
        for field in _WATCHDOG_BUDGET_FIELDS:
            assert field in OVERLAY_OVERRIDABLE_SETTINGS, f"{field} needs a parser in OVERLAY_OVERRIDABLE_SETTINGS"

    def test_defaults_preserve_the_shipped_watchdog_posture(self) -> None:
        # The runtime ceiling is armed; turn and cost caps ship disabled.
        defaults = UserSettings()
        assert defaults.watchdog_max_runtime_seconds == 3 * 60 * 60
        assert defaults.watchdog_max_turns == 0
        assert defaults.watchdog_max_cost_usd == pytest.approx(0.0)
        assert defaults.ticket_budget_max_cost_usd == pytest.approx(0.0)


class TestResolvesThroughConfigTier(TestCase):
    def test_db_row_is_visible_to_get_effective(self) -> None:
        ConfigSetting.objects.set_value("watchdog_max_turns", 250, scope="")
        assert ConfigSetting.objects.get_effective("watchdog_max_turns", scope="") == 250

    def test_ticket_budget_cap_resolves_from_the_store(self) -> None:
        ConfigSetting.objects.set_value("ticket_budget_max_cost_usd", 7.5, scope="")
        assert ConfigSetting.objects.get_effective("ticket_budget_max_cost_usd", scope="") == pytest.approx(7.5)
