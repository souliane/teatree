"""A managed-lane run costs nothing on the meter, so it is recorded as a real $0, never a price-table guess."""

from django.test import TestCase

from teatree.agents.runner_usage import UsageObservation, _attempt_usage
from teatree.core.models import TaskAttempt
from tests.teatree_agents._sdk_fake import result_message


def _million_output_tokens(model: str) -> dict[str, object]:
    return {
        "usage": {"input_tokens": 0, "output_tokens": 1_000_000},
        "model_usage": {model: {"inputTokens": 0, "outputTokens": 1_000_000}},
    }


class TestManagedLaneCost(TestCase):
    def test_a_managed_lane_run_is_recorded_at_zero_and_not_estimated(self) -> None:
        usage = _attempt_usage(
            result_message(**_million_output_tokens("gpt-6-sol")),
            UsageObservation(lane=TaskAttempt.Lane.MANAGED),
        )

        assert (usage.cost_usd, usage.cost_is_estimated) == (0.0, False)

    def test_a_subscription_run_keeps_its_price_table_estimate(self) -> None:
        usage = _attempt_usage(
            result_message(**_million_output_tokens("claude-opus-5-5")),
            UsageObservation(lane=TaskAttempt.Lane.SUBSCRIPTION),
        )

        assert (usage.cost_usd, usage.cost_is_estimated) == (25.0, True)
