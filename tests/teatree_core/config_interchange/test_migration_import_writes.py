"""The import's write phase is one transaction judged as one SET (B10_core_misc-8/-9).

Per-row writes let a document moving a COUPLED pair between two valid states be
rejected on an invalid intermediate, and left the store half-imported when any
row failed.
"""

from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.test import TestCase

from teatree.core.config_interchange.migration import import_toml_to_db
from teatree.core.models import ConfigSetting

_COUPLED_TARGET = """
[teatree]
agent_harness = "pydantic_ai"
agent_harness_provider = "openai_compatible"
"""

_TWO_ROWS = """
[teatree]
backlog_sweep_cadence_hours = 9
bulk_close_threshold = 7
"""


class TestCoupledPairImport(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")

    def test_moves_a_coupled_pair_between_two_valid_states(self) -> None:
        result = import_toml_to_db(_COUPLED_TARGET)

        assert not result.rejected
        assert ConfigSetting.objects.get_effective("agent_harness") == "pydantic_ai"
        assert ConfigSetting.objects.get_effective("agent_harness_provider") == "openai_compatible"

    def test_still_refuses_a_document_whose_resulting_pair_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            import_toml_to_db('[teatree]\nagent_harness_provider = "openai_compatible"\n')

        assert ConfigSetting.objects.get_effective("agent_harness_provider") == "subscription_oauth"


class TestImportWriteAtomicity(TestCase):
    def test_a_failed_row_leaves_no_partial_import(self) -> None:
        calls: list[str] = []
        real = ConfigSetting.objects.reject_inconsistent_cross_key

        def blow_up_on_second(key: str, value: object, scope: str) -> None:
            calls.append(key)
            if len(calls) > 1:
                msg = "simulated write-time refusal"
                raise ValidationError(msg)
            real(key, value, scope)

        with (
            patch.object(ConfigSetting.objects, "reject_inconsistent_cross_key", side_effect=blow_up_on_second),
            pytest.raises(ValidationError),
        ):
            import_toml_to_db(_TWO_ROWS, allow_safety_posture=True)

        assert ConfigSetting.objects.get_effective("backlog_sweep_cadence_hours") is None
        assert ConfigSetting.objects.get_effective("bulk_close_threshold") is None
