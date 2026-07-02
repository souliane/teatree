"""Config resolution for ``agent_harness`` — the headless transport selector (#2565).

``agent_harness`` is DB-home: its sole authoritative tier is the ``ConfigSetting``
store (+ the ``T3_AGENT_HARNESS`` env). The resolver defaults to ``claude_sdk``
(today's behaviour) when no row is set, reads a stored backend, lets the env win
over the store, and raises LOUD on a corrupt stored value so a silent transport
switch never lands. ``CONFIG_PATH`` is isolated so the real ``~/.teatree.toml``
never leaks in.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import AgentHarness, get_effective_settings
from teatree.core.models import ConfigSetting


class TestAgentHarnessResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)

    def test_default_is_claude_sdk_when_no_row(self) -> None:
        assert get_effective_settings().agent_harness is AgentHarness.CLAUDE_SDK

    def test_stored_claude_sdk(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        assert get_effective_settings().agent_harness is AgentHarness.CLAUDE_SDK

    def test_stored_pydantic_ai(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        assert get_effective_settings().agent_harness is AgentHarness.PYDANTIC_AI

    def test_env_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        with patch.dict(os.environ, {"T3_AGENT_HARNESS": "pydantic_ai"}):
            assert get_effective_settings().agent_harness is AgentHarness.PYDANTIC_AI

    def test_corrupt_stored_value_raises(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "grpc")
        with pytest.raises(ValueError, match="agent_harness"):
            get_effective_settings()


class TestAgentHarnessParse:
    def test_parses_canonical_and_normalises(self) -> None:
        assert AgentHarness.parse("pydantic_ai") is AgentHarness.PYDANTIC_AI
        assert AgentHarness.parse("  CLAUDE_SDK  ") is AgentHarness.CLAUDE_SDK

    def test_invalid_value_raises_naming_the_setting(self) -> None:
        with pytest.raises(ValueError, match="agent_harness"):
            AgentHarness.parse("nope")
