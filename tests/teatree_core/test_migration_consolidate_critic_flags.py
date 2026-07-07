# test-path: cross-cutting
"""Data-transform proof for migration 0039 (#104 flag consolidation + #105 ambient delete).

Exercises ``consolidate_critic_and_ambient_flags`` directly against real
``ConfigSetting`` rows (the migration's ``apps.get_model`` resolves to the live
model), so the row-deletion and the legacy-enforcement-bool -> tri-state-mode
re-type are pinned, not just the migration graph's linearity. The legacy key names
are read from the migration's own constants so the test can never drift from — and
never re-introduces the raw string of — the keys the migration purges.
"""

import importlib

from django.apps import apps as django_apps
from django.test import TestCase

from teatree.core.models import ConfigSetting

_MIGRATION = importlib.import_module("teatree.core.migrations.0039_consolidate_critic_flags_delete_ambient")
_DESIGN = _MIGRATION._DESIGN_CRITIC_KEY
_AMBIENT = _MIGRATION._AMBIENT_KEY
_LEGACY = _MIGRATION._CRITIC_LIVE_KEY
_MODE = _MIGRATION._CRITIC_MODE_KEY
_BLOCKING = _MIGRATION._CRITIC_MODE_BLOCKING


def _run() -> None:
    _MIGRATION.consolidate_critic_and_ambient_flags(django_apps, None)


class TestConsolidateCriticAndAmbientFlags(TestCase):
    def test_stale_design_and_ambient_rows_are_deleted(self) -> None:
        ConfigSetting.objects.set_value(_DESIGN, value=True)
        ConfigSetting.objects.set_value(_AMBIENT, value=True, scope="t3-teatree")
        _run()
        assert not ConfigSetting.objects.filter(key=_DESIGN).exists()
        assert not ConfigSetting.objects.filter(key=_AMBIENT).exists()

    def test_truthy_legacy_enforcement_row_becomes_blocking_mode(self) -> None:
        ConfigSetting.objects.set_value(_LEGACY, value=True)
        ConfigSetting.objects.set_value(_LEGACY, value=True, scope="t3-teatree")
        _run()
        assert not ConfigSetting.objects.filter(key=_LEGACY).exists()
        assert ConfigSetting.objects.get_effective(_MODE) == _BLOCKING
        assert ConfigSetting.objects.get_effective(_MODE, scope="t3-teatree") == _BLOCKING

    def test_falsy_legacy_row_falls_through_to_the_off_default(self) -> None:
        # A falsy row was the dark default — it is removed and NO mode row is written,
        # so the setting resolves to its dataclass default (off).
        ConfigSetting.objects.set_value(_LEGACY, value=False)
        _run()
        assert not ConfigSetting.objects.filter(key=_LEGACY).exists()
        assert ConfigSetting.objects.get_effective(_MODE) is None

    def test_unrelated_setting_is_untouched(self) -> None:
        ConfigSetting.objects.set_value("directive_loop_enabled", value=True)
        _run()
        assert ConfigSetting.objects.get_effective("directive_loop_enabled") is True

    def test_is_idempotent(self) -> None:
        ConfigSetting.objects.set_value(_LEGACY, value=True)
        _run()
        _run()  # a second pass finds no legacy rows and is a no-op
        assert ConfigSetting.objects.get_effective(_MODE) == _BLOCKING
        assert not ConfigSetting.objects.filter(key=_LEGACY).exists()
