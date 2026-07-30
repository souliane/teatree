"""Live-registry resolution of a self-correctable config breach (#3665)."""

from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.loop.config_self_repair import repair_for_error
from tests.teatree_core.test_config_self_repair import REGISTRY_ERROR


class TestRepairForError(TestCase):
    def test_resolves_against_the_live_harness_registry(self) -> None:
        repair = repair_for_error(REGISTRY_ERROR)
        assert repair is not None
        assert (repair.setting, repair.value) == ("agent_harness", "pydantic_ai")

    def test_already_corrected_config_is_not_repaired_again(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        assert repair_for_error(REGISTRY_ERROR) is None

    def test_unrecognised_failure_resolves_to_no_repair(self) -> None:
        assert repair_for_error("AssertionError: expected 3 got 4") is None
