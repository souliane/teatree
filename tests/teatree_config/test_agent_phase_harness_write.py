"""``agent_phase_harness`` is refused at write time unless every value pins a known harness or unpins."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting


def _set(value: str, stderr: StringIO) -> None:
    call_command("config_setting", "set", "agent_phase_harness", value, stdout=StringIO(), stderr=stderr)


class TestAgentPhaseHarnessWrite(TestCase):
    def test_a_harness_outside_the_closed_set_is_refused_naming_the_valid_values(self) -> None:
        stderr = StringIO()
        with pytest.raises(SystemExit):
            _set('{"coding": "codex_app_server"}', stderr)

        assert "valid values: claude_sdk, pydantic_ai" in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key="agent_phase_harness").exists()

    def test_an_unpin_is_stored(self) -> None:
        _set('{"testing": "inherit"}', StringIO())

        assert ConfigSetting.objects.get_effective("agent_phase_harness") == {"testing": "inherit"}
