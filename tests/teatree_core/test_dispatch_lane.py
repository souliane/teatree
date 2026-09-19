"""ONE authority answers which Layer-2 lane a dispatch rides (souliane/teatree#4816).

The runner resolved the lane for ATTRIBUTION and the governor never resolved it at all,
so admission judged every dispatch against the subscription fleet — including dispatches
that would never touch it. A second copy of the mapping would let the two disagree about
the very lane the refusal names, so the dispatch and the governor read one function.
"""

from django.test import TestCase

from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.runner import _resolve_dispatch_lane
from teatree.config import AgentHarnessProvider
from teatree.core.dispatch_lane import configured_dispatch_lane, dispatch_lane
from teatree.core.models import ConfigSetting, TaskAttempt


class _Harness:
    def __init__(self, *, metered_lane: bool) -> None:
        self.capabilities = HarnessCapabilities(metered_lane=metered_lane)


class TestTheLaneAuthorityAndTheDispatchAgree(TestCase):
    def test_every_provider_resolves_the_same_lane_on_both_seams(self) -> None:
        for provider in [*AgentHarnessProvider, None]:
            for metered_harness in (True, False):
                assert dispatch_lane(provider=provider, metered_harness=metered_harness) == _resolve_dispatch_lane(
                    _Harness(metered_lane=metered_harness), provider
                )

    def test_a_metered_transport_fixes_the_lane_whatever_the_provider(self) -> None:
        assert (
            dispatch_lane(provider=AgentHarnessProvider.SUBSCRIPTION_OAUTH, metered_harness=True)
            == TaskAttempt.Lane.METERED
        )

    def test_the_subscription_oauth_provider_is_the_subscription_lane(self) -> None:
        assert (
            dispatch_lane(provider=AgentHarnessProvider.SUBSCRIPTION_OAUTH, metered_harness=False)
            == TaskAttempt.Lane.SUBSCRIPTION
        )

    def test_every_byok_key_provider_is_the_metered_lane(self) -> None:
        for provider in (
            AgentHarnessProvider.API_KEY,
            AgentHarnessProvider.OPENAI_COMPATIBLE,
            AgentHarnessProvider.ANTHROPIC_API,
        ):
            assert dispatch_lane(provider=provider, metered_harness=False) == TaskAttempt.Lane.METERED

    def test_control_no_provider_pin_is_unattributed_never_guessed(self) -> None:
        assert dispatch_lane(provider=None, metered_harness=False) == ""


class TestTheConfiguredLaneReadsTheProviderNotTheHarnessRegistry(TestCase):
    """``teatree.core`` may not import ``teatree.agents`` (tach), which is the whole design.

    The governor therefore reads the PROVIDER pin. An unset pin is genuinely unattributed
    from core, which keeps today's subscription-path behaviour byte-for-byte.
    """

    def test_a_metered_provider_pin_resolves_the_metered_lane(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "openai_compatible")

        assert configured_dispatch_lane() == TaskAttempt.Lane.METERED

    def test_control_a_subscription_pin_resolves_the_subscription_lane(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")

        assert configured_dispatch_lane() == TaskAttempt.Lane.SUBSCRIPTION

    def test_control_an_unreadable_setting_is_unattributed_never_a_guess(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415 — local to the one degraded-read case

        with patch("teatree.core.dispatch_lane._configured_provider", side_effect=RuntimeError("config down")):
            assert configured_dispatch_lane() == ""
