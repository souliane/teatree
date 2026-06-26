"""Config resolution for ``agent_runtime`` — the loop-dispatched phase runtime selector.

``agent_runtime`` is DB-home: its sole authoritative tier is the ``ConfigSetting``
store (+ the ``T3_AGENT_RUNTIME`` env). The resolver defaults to ``interactive``
(today's behaviour) when no row is set, reads a stored headless runtime, lets the
env win over the store, and raises LOUD on a corrupt stored value so a silent
runtime switch never lands. ``CONFIG_PATH`` is isolated so the real
``~/.teatree.toml`` never leaks in.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import AgentRuntime, get_effective_settings
from teatree.core.models import ConfigSetting


class TestAgentRuntimeResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_RUNTIME", raising=False)

    def test_default_is_interactive_when_no_row(self) -> None:
        assert get_effective_settings().agent_runtime is AgentRuntime.INTERACTIVE

    def test_stored_sdk_oauth(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "sdk_oauth")
        assert get_effective_settings().agent_runtime is AgentRuntime.SDK_OAUTH

    def test_stored_sdk_apikey(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "sdk_apikey")
        assert get_effective_settings().agent_runtime is AgentRuntime.SDK_APIKEY

    def test_stored_api(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "api")
        assert get_effective_settings().agent_runtime is AgentRuntime.API

    def test_env_wins_over_store(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        with patch.dict(os.environ, {"T3_AGENT_RUNTIME": "sdk_apikey"}):
            assert get_effective_settings().agent_runtime is AgentRuntime.SDK_APIKEY

    def test_corrupt_stored_value_raises(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headfull")
        with pytest.raises(ValueError, match="agent_runtime"):
            get_effective_settings()


class TestAgentRuntimeParse:
    def test_parses_canonical_and_normalises(self) -> None:
        assert AgentRuntime.parse("sdk_oauth") is AgentRuntime.SDK_OAUTH
        assert AgentRuntime.parse("  INTERACTIVE  ") is AgentRuntime.INTERACTIVE

    def test_invalid_value_raises_naming_the_setting(self) -> None:
        with pytest.raises(ValueError, match="agent_runtime"):
            AgentRuntime.parse("nope")

    def test_is_headless_partitions_the_runtimes(self) -> None:
        assert not AgentRuntime.INTERACTIVE.is_headless
        assert AgentRuntime.SDK_OAUTH.is_headless
        assert AgentRuntime.SDK_APIKEY.is_headless
        assert AgentRuntime.API.is_headless
