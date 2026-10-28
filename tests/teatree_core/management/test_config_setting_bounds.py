"""The CLI refuses run caps below the shared write floor."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting


class TestConfigSettingBounds(TestCase):
    def test_cli_refuses_low_run_cap_globally_and_per_overlay(self) -> None:
        for flags in ((), ("--overlay", "sample")):
            stderr = StringIO()
            with self.subTest(flags=flags), pytest.raises(SystemExit) as error:
                call_command("config_setting", "set", "agent_max_turns", "9", *flags, stderr=stderr)
            assert error.value.code == 2
            assert "agent_max_turns" in stderr.getvalue()
            assert "100" in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key="agent_max_turns").exists()

    def test_cli_accepts_zero_and_floor(self) -> None:
        call_command("config_setting", "set", "agent_max_turns", "0", stdout=StringIO())
        call_command("config_setting", "set", "agent_max_turns", "100", stdout=StringIO())
        assert ConfigSetting.objects.get_effective("agent_max_turns") == 100
