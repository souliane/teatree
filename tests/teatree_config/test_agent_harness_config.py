"""Config resolution for the OPEN ``agent_harness`` transport selector (#2565, #3157 E1).

``agent_harness`` is DB-home: its sole authoritative tier is the ``ConfigSetting``
store (+ the ``T3_AGENT_HARNESS`` env). The resolver defaults to ``claude_sdk``
(today's behaviour) when no row is set, reads a stored backend, and lets the env win
over the store. The backend set is OPEN (#3157 E1): this setting is a registry KEY, so a
well-formed but UNREGISTERED name (an overlay whose entry point failed to load, a typo) is
no longer rejected at config parse — the config layer cannot see the agents-layer registry
— but fails LOUD at dispatch (``resolve_harness`` raises ``UnknownHarnessError``). An empty
value is still rejected at parse so a blank row never resolves to a nameless backend.
"""

import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.harness import resolve_harness
from teatree.agents.harness_registry import UnknownHarnessError
from teatree.config import AgentHarness, get_effective_settings
from teatree.core.models import ConfigSetting


class TestAgentHarnessResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)

    def test_default_is_claude_sdk_when_no_row(self) -> None:
        # A registry KEY (string) now, not the enum member — but value-equal to it.
        assert get_effective_settings().agent_harness == AgentHarness.CLAUDE_SDK

    def test_stored_claude_sdk(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        assert get_effective_settings().agent_harness == AgentHarness.CLAUDE_SDK

    def test_stored_pydantic_ai(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        assert get_effective_settings().agent_harness == AgentHarness.PYDANTIC_AI

    def test_env_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        with patch.dict(os.environ, {"T3_AGENT_HARNESS": "pydantic_ai"}):
            assert get_effective_settings().agent_harness == AgentHarness.PYDANTIC_AI

    def test_empty_stored_value_raises_at_parse(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "   ")
        with pytest.raises(ValueError, match="agent_harness"):
            get_effective_settings()

    def test_unregistered_name_is_accepted_at_config_but_fails_loud_at_dispatch(self) -> None:
        # An open backend set: a well-formed unknown name parses (the config layer cannot
        # see the agents registry), then fails LOUD at dispatch rather than silently.
        ConfigSetting.objects.set_value("agent_harness", "grpc")
        assert get_effective_settings().agent_harness == "grpc"
        with pytest.raises(UnknownHarnessError, match="grpc"):
            resolve_harness()


class TestAgentHarnessParse:
    def test_parses_canonical_and_normalises(self) -> None:
        assert AgentHarness.parse("pydantic_ai") is AgentHarness.PYDANTIC_AI
        assert AgentHarness.parse("  CLAUDE_SDK  ") is AgentHarness.CLAUDE_SDK

    def test_invalid_value_raises_naming_the_setting(self) -> None:
        with pytest.raises(ValueError, match="agent_harness"):
            AgentHarness.parse("nope")
