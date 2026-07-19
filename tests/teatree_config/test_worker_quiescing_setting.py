"""The ``worker_quiescing`` DB-home admission gate (drain-then-deploy).

Ships OFF, resolves from the ``ConfigSetting`` store like ``loop_runner_enabled``,
is registered in the overridable/env registries so ``config_setting set`` /
``t3 worker drain`` can write it and ``T3_WORKER_QUIESCING`` can override it.
"""

import os
from unittest import mock

from django.test import TestCase

from teatree.config import ENV_SETTING_OVERRIDES, OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, get_effective_settings
from teatree.core.models import ConfigSetting


class TestWorkerQuiescingSetting(TestCase):
    def test_defaults_off(self) -> None:
        assert UserSettings().worker_quiescing is False

    def test_registered_overridable_and_env(self) -> None:
        assert "worker_quiescing" in OVERLAY_OVERRIDABLE_SETTINGS
        assert ENV_SETTING_OVERRIDES["T3_WORKER_QUIESCING"][0] == "worker_quiescing"

    def test_resolves_from_the_db_store(self) -> None:
        ConfigSetting.objects.set_value("worker_quiescing", value=True)
        assert get_effective_settings().worker_quiescing is True

    def test_env_overrides_the_store(self) -> None:
        ConfigSetting.objects.set_value("worker_quiescing", value=True)
        with mock.patch.dict(os.environ, {"T3_WORKER_QUIESCING": "0"}):
            assert get_effective_settings().worker_quiescing is False
