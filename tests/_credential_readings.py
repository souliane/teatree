"""Test fixture for metered credential health readings."""

from teatree.core.models.anthropic_token_usage import REJECTED_STATUS, TokenHealthReading
from teatree.llm.rate_limits import MeteredKeySnapshot


def reading_from_metered(snapshot: MeteredKeySnapshot) -> TokenHealthReading:
    return TokenHealthReading(
        organization_id=snapshot.organization_id,
        utilization_5h=None,
        utilization_7d=None,
        status_5h="",
        status_7d=REJECTED_STATUS if snapshot.out_of_credits else "",
        reset_5h=None,
        reset_7d=None,
    )
