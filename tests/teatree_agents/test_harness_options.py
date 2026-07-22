"""The neutral HarnessOptions adapter boundary (#3157 AH-2).

Acceptance: a provider-agnostic backend consumes the neutral
:class:`~teatree.agents.harness_options.HarnessOptions`, adapted ONCE from the vendor
``ClaudeAgentOptions`` at the ``open`` boundary — the vendor type never leaks into
provider-agnostic harness logic, and the factory overlay can build the neutral options directly.
"""

import inspect
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.harness import PydanticAiHarness, resolve_effort
from teatree.agents.harness_options import HarnessOptions, extract_system_prompt
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.pydantic_ai_config import native_anthropic_model_name, resolve_native_anthropic_model


class TestExtractSystemPrompt:
    def test_plain_string_passes_through(self) -> None:
        assert extract_system_prompt(ClaudeAgentOptions(system_prompt="a plain prompt")) == "a plain prompt"

    def test_preset_extracts_the_appended_context(self) -> None:
        options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": "the appended context"}
        )
        assert extract_system_prompt(options) == "the appended context"

    def test_none_yields_empty_string(self) -> None:
        assert extract_system_prompt(ClaudeAgentOptions(system_prompt=None)) == ""


class TestHarnessOptionsFromSdk:
    """from_sdk_options is the ONE adapter that touches the vendor type."""

    def test_carries_the_provider_agnostic_fields(self, tmp_path: Path) -> None:
        options = ClaudeAgentOptions(
            model="claude-opus-4-8",
            effort="high",
            system_prompt={"type": "preset", "preset": "claude_code", "append": "ctx"},
            cwd=str(tmp_path),
            env={"ANTHROPIC_API_KEY": "sk-x"},
        )
        neutral = HarnessOptions.from_sdk_options(options)
        assert neutral.model == "claude-opus-4-8"
        assert neutral.effort == "high"
        assert neutral.system_prompt == "ctx"  # the SDK claude_code preset is stripped to the plain append
        assert neutral.cwd == str(tmp_path)
        assert neutral.env == {"ANTHROPIC_API_KEY": "sk-x"}

    def test_coerces_a_path_cwd_to_a_plain_string(self, tmp_path: Path) -> None:
        neutral = HarnessOptions.from_sdk_options(ClaudeAgentOptions(cwd=tmp_path))
        assert neutral.cwd == str(tmp_path)
        assert isinstance(neutral.cwd, str)

    def test_defaults_are_empty_and_neutral(self) -> None:
        neutral = HarnessOptions.from_sdk_options(ClaudeAgentOptions())
        assert neutral.model is None
        assert neutral.effort is None
        assert neutral.system_prompt == ""
        assert neutral.cwd is None
        assert neutral.env == {}

    def test_the_neutral_type_is_buildable_without_the_vendor_type(self) -> None:
        # The factory overlay builds HarnessOptions directly (no ClaudeAgentOptions dependency).
        neutral = HarnessOptions(model="claude-sonnet-5", system_prompt="hi")
        assert neutral.model == "claude-sonnet-5"
        assert neutral.system_prompt == "hi"

    def test_positive_max_turns_is_carried(self) -> None:
        assert HarnessOptions.from_sdk_options(ClaudeAgentOptions(max_turns=3)).max_turns == 3

    def test_sdk_none_and_zero_max_turns_coerce_to_zero(self) -> None:
        # The SDK default is None and headless dispatch sends 0 — both mean "uncapped" → 0, so
        # only a genuinely positive cap wins over the lane's request_limit downstream.
        assert HarnessOptions.from_sdk_options(ClaudeAgentOptions()).max_turns == 0
        assert HarnessOptions.from_sdk_options(ClaudeAgentOptions(max_turns=0)).max_turns == 0


class TestVendorTypeDoesNotLeakPastOpen:
    """AH-2 acceptance: the provider-agnostic harness logic consumes ONLY HarnessOptions.

    ``Harness.open`` still accepts ``ClaudeAgentOptions`` at the boundary (the claude_sdk
    backend needs it; the SDK-specific port surface is a deferred strangler-fig migration),
    but past ``open`` every provider-agnostic helper takes the neutral HarnessOptions — so
    the vendor type does not thread into pydantic_ai/Vertex logic.
    """

    def test_provider_agnostic_helpers_take_the_neutral_type(self) -> None:
        for func in (
            PydanticAiHarness._resolve_model,
            resolve_effort,
            LaneBToolConfig.from_options,
            native_anthropic_model_name,
            resolve_native_anthropic_model,
        ):
            annotation = inspect.signature(func).parameters["options"].annotation
            assert annotation is HarnessOptions, f"{func.__qualname__} leaks the vendor option type"

    def test_open_still_accepts_the_vendor_type_at_the_boundary(self) -> None:
        # The one documented seam boundary where the vendor type is still the carrier.
        annotation = inspect.signature(PydanticAiHarness.open).parameters["options"].annotation
        assert annotation is ClaudeAgentOptions
