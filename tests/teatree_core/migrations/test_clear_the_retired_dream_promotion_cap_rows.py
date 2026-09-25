"""The ``0115`` data migration clears the stored rows under the retired dream promotion cap."""

from importlib import import_module

from django.apps import apps
from django.test import TestCase

from teatree.core.models import ConfigSetting

clear_rows = import_module("teatree.core.migrations.0115_clear_the_retired_dream_promotion_cap_rows").clear_rows


class TestClearTheRetiredDreamPromotionCapRows(TestCase):
    def test_every_scope_of_the_retired_key_is_cleared_and_nothing_else(self) -> None:
        ConfigSetting.objects.create(scope="", key="dream_promotion_cap", value="11")
        ConfigSetting.objects.create(scope="acme", key="dream_promotion_cap", value="0")
        ConfigSetting.objects.create(scope="", key="dream_memory_promote", value="false")

        clear_rows(apps, None)

        assert list(ConfigSetting.objects.values_list("key", flat=True)) == ["dream_memory_promote"]
