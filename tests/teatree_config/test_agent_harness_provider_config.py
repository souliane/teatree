"""Config resolution for ``agent_harness_provider`` — the two-layer harness Layer 2 (#2887).

``agent_harness_provider`` is DB-home: its sole authoritative tier is the
``ConfigSetting`` store (+ the ``T3_AGENT_HARNESS_PROVIDER`` env). The resolver
defaults to ``None`` (no explicit Layer-2 pin — a ``ClaudeSdkHarness`` dispatch
inherits the ambient environment unchanged) when no row is set, reads a stored
provider, lets the env win over the store, and raises LOUD on a corrupt stored
value so a silent credential switch never lands. :meth:`AgentHarnessProvider.valid_for`
is the Layer-1-constrained-Layer-2 contract: ``claude_sdk`` accepts
``subscription_oauth`` / ``api_key``; ``pydantic_ai`` accepts only
``orca_router_byok``.
"""

import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import AgentHarness, AgentHarnessProvider, get_effective_settings
from teatree.core.models import ConfigSetting


class TestAgentHarnessProviderResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)

    def test_default_is_none_when_no_row(self) -> None:
        assert get_effective_settings().agent_harness_provider is None

    def test_stored_api_key(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "api_key")
        assert get_effective_settings().agent_harness_provider is AgentHarnessProvider.API_KEY

    def test_stored_orca_router_byok(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "orca_router_byok")
        assert get_effective_settings().agent_harness_provider is AgentHarnessProvider.ORCA_ROUTER_BYOK

    def test_env_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        with patch.dict(os.environ, {"T3_AGENT_HARNESS_PROVIDER": "api_key"}):
            assert get_effective_settings().agent_harness_provider is AgentHarnessProvider.API_KEY

    def test_corrupt_stored_value_raises(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "vertex")
        with pytest.raises(ValueError, match="agent_harness_provider"):
            get_effective_settings()


class TestOrcaRouterLaneAndNameResolution(TestCase):
    """The two DB-home OrcaRouter call-site knobs — ``orca_router_lane`` / ``orca_router_name``.

    ``orca_router_lane`` is the ``x-lane`` header value (``factory`` default, ``eval``
    for the eval CI job, ``bulk`` for a secondary overlay's cheap legs); ``orca_router_name`` is the
    per-overlay OrcaRouter router handle (empty → the ``teatree-factory`` default). Both
    are DB-home: env → ConfigSetting (overlay → global) → default.
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ORCA_ROUTER_LANE", raising=False)
        monkeypatch.delenv("T3_ORCA_ROUTER_NAME", raising=False)

    def test_defaults_are_factory_lane_and_empty_name(self) -> None:
        settings = get_effective_settings()
        assert settings.orca_router_lane == "factory"
        assert settings.orca_router_name == ""

    def test_stored_lane_and_name(self) -> None:
        ConfigSetting.objects.set_value("orca_router_lane", "bulk")
        ConfigSetting.objects.set_value("orca_router_name", "orcarouter/secondary-factory")
        settings = get_effective_settings()
        assert settings.orca_router_lane == "bulk"
        assert settings.orca_router_name == "orcarouter/secondary-factory"

    def test_env_lane_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("orca_router_lane", "factory")
        with patch.dict(os.environ, {"T3_ORCA_ROUTER_LANE": "eval"}):
            assert get_effective_settings().orca_router_lane == "eval"

    def test_env_name_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("orca_router_name", "orcarouter/teatree-factory")
        with patch.dict(os.environ, {"T3_ORCA_ROUTER_NAME": "orcarouter/secondary-factory"}):
            assert get_effective_settings().orca_router_name == "orcarouter/secondary-factory"

    def test_overlay_scoped_row_beats_global(self) -> None:
        ConfigSetting.objects.set_value("orca_router_name", "orcarouter/teatree-factory")
        ConfigSetting.objects.set_value("orca_router_name", "orcarouter/secondary-factory", scope="secondary-overlay")
        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "secondary-overlay"}):
            assert get_effective_settings().orca_router_name == "orcarouter/secondary-factory"


class TestAgentHarnessProviderParse:
    def test_parses_canonical_and_normalises(self) -> None:
        assert AgentHarnessProvider.parse("api_key") is AgentHarnessProvider.API_KEY
        assert AgentHarnessProvider.parse("  SUBSCRIPTION_OAUTH  ") is AgentHarnessProvider.SUBSCRIPTION_OAUTH

    def test_invalid_value_raises_naming_the_setting(self) -> None:
        with pytest.raises(ValueError, match="agent_harness_provider"):
            AgentHarnessProvider.parse("nope")


class TestAgentHarnessProviderValidFor:
    def test_claude_sdk_accepts_the_two_anthropic_credentials(self) -> None:
        assert AgentHarnessProvider.valid_for(AgentHarness.CLAUDE_SDK) == frozenset(
            {AgentHarnessProvider.SUBSCRIPTION_OAUTH, AgentHarnessProvider.API_KEY},
        )

    def test_pydantic_ai_accepts_the_router_byok_and_native_anthropic_credentials(self) -> None:
        # #3157 E1b added the native Anthropic Messages-API binding under pydantic_ai.
        assert AgentHarnessProvider.valid_for(AgentHarness.PYDANTIC_AI) == frozenset(
            {AgentHarnessProvider.ORCA_ROUTER_BYOK, AgentHarnessProvider.ANTHROPIC_API},
        )

    def test_orca_router_byok_is_invalid_under_claude_sdk(self) -> None:
        assert AgentHarnessProvider.ORCA_ROUTER_BYOK not in AgentHarnessProvider.valid_for(AgentHarness.CLAUDE_SDK)

    def test_api_key_is_invalid_under_pydantic_ai(self) -> None:
        assert AgentHarnessProvider.API_KEY not in AgentHarnessProvider.valid_for(AgentHarness.PYDANTIC_AI)
