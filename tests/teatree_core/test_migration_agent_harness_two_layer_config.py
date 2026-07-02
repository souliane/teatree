"""Regression test for ``core.0015_agent_harness_two_layer_config`` (#2887).

A ``ConfigSetting(key="agent_runtime")`` row stored before #2887 may carry
``sdk_oauth`` / ``sdk_apikey`` / ``api`` — values the post-#2887 ``AgentRuntime``
enum can no longer parse. ``0015`` collapses those to ``headless`` and, for the
two credential-carrying values, seeds the sibling ``agent_harness_provider`` row
in the SAME scope so an existing install resolves the identical credential
after the upgrade as before it.
"""

import importlib

from django.apps import apps
from django.db import connection
from django.test import TestCase

from teatree.core.models import ConfigSetting

_migration = importlib.import_module("teatree.core.migrations.0015_agent_harness_two_layer_config")


class CollapseAgentRuntimeToTwoLayerTest(TestCase):
    def test_sdk_oauth_collapses_to_headless_and_seeds_subscription_oauth(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "sdk_oauth")

        _migration.collapse_agent_runtime_to_two_layer(apps, connection.schema_editor())

        assert ConfigSetting.objects.get_effective("agent_runtime") == "headless"
        assert ConfigSetting.objects.get_effective("agent_harness_provider") == "subscription_oauth"

    def test_sdk_apikey_collapses_to_headless_and_seeds_api_key(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "sdk_apikey")

        _migration.collapse_agent_runtime_to_two_layer(apps, connection.schema_editor())

        assert ConfigSetting.objects.get_effective("agent_runtime") == "headless"
        assert ConfigSetting.objects.get_effective("agent_harness_provider") == "api_key"

    def test_api_collapses_to_headless_with_no_provider_seed(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "api")

        _migration.collapse_agent_runtime_to_two_layer(apps, connection.schema_editor())

        assert ConfigSetting.objects.get_effective("agent_runtime") == "headless"
        assert ConfigSetting.objects.get_effective("agent_harness_provider") is None

    def test_already_migrated_value_is_a_noop(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        ConfigSetting.objects.set_value("agent_harness_provider", "api_key")

        _migration.collapse_agent_runtime_to_two_layer(apps, connection.schema_editor())

        assert ConfigSetting.objects.get_effective("agent_runtime") == "headless"
        assert ConfigSetting.objects.get_effective("agent_harness_provider") == "api_key"

    def test_interactive_value_is_a_noop(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "interactive")

        _migration.collapse_agent_runtime_to_two_layer(apps, connection.schema_editor())

        assert ConfigSetting.objects.get_effective("agent_runtime") == "interactive"
        assert ConfigSetting.objects.get_effective("agent_harness_provider") is None

    def test_no_row_is_a_noop(self) -> None:
        _migration.collapse_agent_runtime_to_two_layer(apps, connection.schema_editor())

        assert ConfigSetting.objects.get_effective("agent_runtime") is None
        assert ConfigSetting.objects.get_effective("agent_harness_provider") is None

    def test_migrates_each_scope_independently(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "sdk_oauth", scope="acme")
        ConfigSetting.objects.set_value("agent_runtime", "sdk_apikey", scope="")

        _migration.collapse_agent_runtime_to_two_layer(apps, connection.schema_editor())

        assert ConfigSetting.objects.get_effective("agent_runtime", scope="acme") == "headless"
        assert ConfigSetting.objects.get_effective("agent_harness_provider", scope="acme") == "subscription_oauth"
        assert ConfigSetting.objects.get_effective("agent_runtime", scope="") == "headless"
        assert ConfigSetting.objects.get_effective("agent_harness_provider", scope="") == "api_key"
