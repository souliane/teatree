"""Native Anthropic Messages-API binding on the pydantic_ai lane (#3157 E1b).

Acceptance: ``agent_harness_provider=anthropic_api`` under ``agent_harness=pydantic_ai``
selects the NATIVE binding (real ``cache_control`` reachable), one branch in ``_resolve_model``,
and the metered router's own reported cost is passed through when present.
"""

import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase
from pydantic_ai.models.test import TestModel

from teatree.agents.harness import PydanticAiHarness, resolve_harness
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.pydantic_ai_config import (
    PYDANTIC_AI_NATIVE_CAPABILITIES,
    NativeAnthropicUnavailableError,
    PydanticAiBinding,
    PydanticAiModelConfig,
    build_model_settings,
    native_anthropic_model_name,
)
from teatree.agents.pydantic_ai_session import _router_reported_cost
from teatree.config import AgentHarness, AgentHarnessProvider
from teatree.core.models import ConfigSetting

_ANTHROPIC_INSTALLED = importlib.util.find_spec("anthropic") is not None


class TestBindingSelection(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_orca_router_provider_selects_the_router_binding(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "orca_router_byok")
        harness = resolve_harness()
        assert isinstance(harness, PydanticAiHarness)
        assert harness.binding is PydanticAiBinding.ROUTER
        assert harness.capabilities.cache_control is False

    def test_anthropic_api_provider_selects_the_native_binding(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "anthropic_api")
        harness = resolve_harness()
        assert isinstance(harness, PydanticAiHarness)
        assert harness.binding is PydanticAiBinding.NATIVE_ANTHROPIC
        assert harness.capabilities == PYDANTIC_AI_NATIVE_CAPABILITIES
        assert harness.capabilities.cache_control is True


class TestProviderConstraintTable:
    def test_anthropic_api_is_valid_under_pydantic_ai(self) -> None:
        valid = AgentHarnessProvider.valid_for(AgentHarness.PYDANTIC_AI)
        assert AgentHarnessProvider.ANTHROPIC_API in valid

    def test_anthropic_api_is_not_valid_under_claude_sdk(self) -> None:
        valid = AgentHarnessProvider.valid_for(AgentHarness.CLAUDE_SDK)
        assert AgentHarnessProvider.ANTHROPIC_API not in valid


class TestNativeModelResolution:
    @pytest.mark.skipif(_ANTHROPIC_INSTALLED, reason="anthropic extra present — see the constructs test")
    def test_native_branch_fails_loud_when_the_optional_extra_is_absent(self) -> None:
        # `pydantic-ai-slim[anthropic]` is an OPTIONAL extra; when absent the ONE native branch
        # (not the OpenAI router client) fails LOUD with the install hint rather than silently.
        harness = PydanticAiHarness(config=PydanticAiModelConfig(binding=PydanticAiBinding.NATIVE_ANTHROPIC))
        options = HarnessOptions(model="claude-opus-4-8")
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False),
            pytest.raises(NativeAnthropicUnavailableError, match="anthropic"),
        ):
            harness._resolve_model(options)

    @pytest.mark.skipif(not _ANTHROPIC_INSTALLED, reason="anthropic extra absent — see the fails-loud test")
    def test_native_branch_constructs_the_anthropic_model_when_the_extra_is_present(self) -> None:
        harness = PydanticAiHarness(config=PydanticAiModelConfig(binding=PydanticAiBinding.NATIVE_ANTHROPIC))
        options = HarnessOptions(model="claude-opus-4-8")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            model = harness._resolve_model(options)
        # The native Anthropic model, NOT the OpenAI-compatible router model.
        assert "anthropic" in type(model).__module__.lower()

    def test_injected_model_short_circuits_before_the_native_branch(self) -> None:
        injected = TestModel()
        harness = PydanticAiHarness(
            model=injected, config=PydanticAiModelConfig(binding=PydanticAiBinding.NATIVE_ANTHROPIC)
        )
        assert harness._resolve_model(HarnessOptions()) is injected


class TestNativeModelNameFallback:
    """AH-4: the unpinned native-Anthropic fallback must be a valid Anthropic model id."""

    def test_unpinned_fallback_is_a_concrete_anthropic_id_not_a_router_handle(self) -> None:
        # The bug: an unpinned dispatch resolved through resolve_pydantic_ai_model, which
        # returns an ``orcarouter/…`` router handle — invalid on the direct Anthropic API.
        name = native_anthropic_model_name(HarnessOptions())
        assert "/" not in name  # NOT a provider-prefixed router handle (orcarouter/teatree-factory)
        assert name.startswith("claude-")  # a concrete Claude dash-form id, valid on the Messages API

    def test_explicit_pin_passes_through_unchanged(self) -> None:
        assert native_anthropic_model_name(HarnessOptions(model="claude-opus-4-8")) == "claude-opus-4-8"


class TestBindingSpecificEffortSettings:
    """The reasoning effort must ride the key the SELECTED binding's model actually reads.

    pydantic_ai namespaces model settings per provider and silently ignores a foreign
    key, so the OpenAI-shaped ``openai_reasoning_effort`` handed to the native Anthropic
    binding dropped the whole effort axis with no error. The vocabularies differ too:
    the router scale carries ``minimal``, which the Anthropic Messages API rejects, and
    an explicitly-set ``anthropic_effort`` is passed straight to the wire unmapped.
    """

    # ``build_model_settings`` reads only ``model.profile``, so the binding shape is
    # provable without constructing a real AnthropicModel — keeping this hermetic and
    # free of the optional ``anthropic`` extra. The real-model construction is covered
    # by ``TestNativeBindingConstruction``.
    _XHIGH = SimpleNamespace(profile={"anthropic_supports_xhigh_effort": True})
    _NO_XHIGH = SimpleNamespace(profile={"anthropic_supports_xhigh_effort": False})

    def test_router_binding_uses_the_openai_effort_key(self) -> None:
        settings = build_model_settings(TestModel(), "high", binding=PydanticAiBinding.ROUTER)
        assert settings == {"openai_reasoning_effort": "high"}

    def test_native_binding_uses_the_anthropic_effort_key(self) -> None:
        settings = build_model_settings(self._XHIGH, "high", binding=PydanticAiBinding.NATIVE_ANTHROPIC)
        # The regression: NOT ``openai_reasoning_effort``, which AnthropicModel ignores.
        assert settings == {"anthropic_effort": "high"}

    def test_native_binding_maps_minimal_onto_the_anthropic_vocabulary(self) -> None:
        # ``minimal`` is router-scale only; sent verbatim the Messages API 400s.
        settings = build_model_settings(self._XHIGH, "minimal", binding=PydanticAiBinding.NATIVE_ANTHROPIC)
        assert settings == {"anthropic_effort": "low"}

    def test_native_binding_downgrades_xhigh_when_the_model_lacks_it(self) -> None:
        assert build_model_settings(self._XHIGH, "xhigh", binding=PydanticAiBinding.NATIVE_ANTHROPIC) == {
            "anthropic_effort": "xhigh"
        }
        assert build_model_settings(self._NO_XHIGH, "xhigh", binding=PydanticAiBinding.NATIVE_ANTHROPIC) == {
            "anthropic_effort": "max"
        }

    def test_absent_effort_yields_no_settings_on_either_binding(self) -> None:
        assert build_model_settings(TestModel(), None, binding=PydanticAiBinding.ROUTER) is None
        assert build_model_settings(self._XHIGH, None, binding=PydanticAiBinding.NATIVE_ANTHROPIC) is None


class TestRouterReportedCost:
    def test_reads_a_cost_key_from_run_usage_details(self) -> None:
        assert _router_reported_cost(SimpleNamespace(details={"cost": 0.37})) == pytest.approx(0.37)

    def test_none_when_no_details(self) -> None:
        assert _router_reported_cost(SimpleNamespace(details=None)) is None
        assert _router_reported_cost(object()) is None

    def test_ignores_a_bool_or_negative_value(self) -> None:
        assert _router_reported_cost(SimpleNamespace(details={"cost": True, "total_cost": -1})) is None
