"""The pure self-repair criterion: exactly one valid resolution, else page (#3665)."""

import pytest
from django.test import TestCase

from teatree.core.config_self_repair import SELF_REPAIR_STAMP, ConfigRepair, plan_config_repair
from teatree.core.models import ConfigSetting

REGISTRY_ERROR = (
    "agent_harness_provider='openai_compatible' is not valid under agent_harness='claude_sdk'; "
    "valid: api_key, subscription_oauth"
)
HEADLESS_ERROR = (
    "agent_harness_provider='openai_compatible' is not valid under agent_harness=claude_sdk; "
    "valid values: api_key, subscription_oauth"
)

_TWO_HARNESS_TABLE = {
    "claude_sdk": frozenset({"subscription_oauth", "api_key"}),
    "pydantic_ai": frozenset({"openai_compatible", "anthropic_api"}),
}


class TestPlanConfigRepair:
    """Exactly ONE valid resolution self-repairs; zero or many is a decision that pages."""

    @pytest.mark.parametrize("error", [REGISTRY_ERROR, HEADLESS_ERROR])
    def test_unique_harness_for_the_pinned_provider_is_self_correctable(self, error: str) -> None:
        repair = plan_config_repair(error, valid_providers_by_harness=_TWO_HARNESS_TABLE)
        assert repair == ConfigRepair(
            setting="agent_harness",
            value="pydantic_ai",
            detail=(
                "agent_harness_provider='openai_compatible' is valid under exactly one harness "
                "('pydantic_ai'), so the pinned provider decides the transport"
            ),
        )

    def test_ambiguous_provider_pages_instead_of_guessing(self) -> None:
        table = {**_TWO_HARNESS_TABLE, "vendor_sdk": frozenset({"openai_compatible"})}
        assert plan_config_repair(REGISTRY_ERROR, valid_providers_by_harness=table) is None

    def test_provider_valid_nowhere_pages(self) -> None:
        assert plan_config_repair(REGISTRY_ERROR, valid_providers_by_harness={}) is None

    def test_unrecognised_failure_is_not_self_correctable(self) -> None:
        error = "AssertionError: expected 3 got 4"
        assert plan_config_repair(error, valid_providers_by_harness=_TWO_HARNESS_TABLE) is None

    def test_empty_error_is_not_self_correctable(self) -> None:
        assert plan_config_repair("", valid_providers_by_harness=_TWO_HARNESS_TABLE) is None

    def test_stamp_names_the_setting_and_the_corrected_value(self) -> None:
        repair = ConfigRepair(setting="agent_harness", value="pydantic_ai", detail="d")
        assert repair.stamp() == f"{SELF_REPAIR_STAMP} agent_harness=pydantic_ai"


class TestApplyConfigRepair(TestCase):
    def test_writes_the_single_valid_value_into_the_config_store(self) -> None:
        ConfigRepair(setting="agent_harness", value="pydantic_ai", detail="d").apply()
        assert ConfigSetting.objects.get_effective("agent_harness") == "pydantic_ai"
