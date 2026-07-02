"""Config resolution for ``agent_harness_provider`` — the two-layer harness Layer 2 (#2887).

``agent_harness_provider`` is DB-home: its sole authoritative tier is the
``ConfigSetting`` store (+ the ``T3_AGENT_HARNESS_PROVIDER`` env). The resolver
defaults to ``None`` (no explicit Layer-2 pin — a ``ClaudeSdkHarness`` dispatch
inherits the ambient environment unchanged) when no row is set, reads a stored
provider, lets the env win over the store, and raises LOUD on a corrupt stored
value so a silent credential switch never lands. :meth:`AgentHarnessProvider.valid_for`
is the Layer-1-constrained-Layer-2 contract: ``claude_sdk`` accepts
``subscription_oauth`` / ``api_key``; ``pydantic_ai`` accepts only
``orca_router_byok``. ``CONFIG_PATH`` is isolated so the real ``~/.teatree.toml``
never leaks in.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import AgentHarness, AgentHarnessProvider, get_effective_settings
from teatree.core.models import ConfigSetting


class TestAgentHarnessProviderResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
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

    def test_pydantic_ai_accepts_only_orca_router_byok(self) -> None:
        assert AgentHarnessProvider.valid_for(AgentHarness.PYDANTIC_AI) == frozenset(
            {AgentHarnessProvider.ORCA_ROUTER_BYOK},
        )

    def test_orca_router_byok_is_invalid_under_claude_sdk(self) -> None:
        assert AgentHarnessProvider.ORCA_ROUTER_BYOK not in AgentHarnessProvider.valid_for(AgentHarness.CLAUDE_SDK)

    def test_api_key_is_invalid_under_pydantic_ai(self) -> None:
        assert AgentHarnessProvider.API_KEY not in AgentHarnessProvider.valid_for(AgentHarness.PYDANTIC_AI)
