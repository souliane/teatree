"""The shared dashboard ``danger_gate_fail_open`` read — one guarded helper (#3313).

Both the health-bands mode band and the loop-control header read the master
fail-open switch through :func:`dash_gate_fail_open`, which fails closed on a
broken read so neither page 500s.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models.config_setting import ConfigSetting
from teatree.dash.gate_state import dash_gate_fail_open


class DashGateFailOpenTestCase(TestCase):
    def test_defaults_to_false_with_no_row(self) -> None:
        assert dash_gate_fail_open() is False

    def test_reflects_stored_true(self) -> None:
        ConfigSetting.objects.set_value("danger_gate_fail_open", value=True)
        assert dash_gate_fail_open() is True

    def test_broken_read_fails_closed_to_false(self) -> None:
        with patch.object(ConfigSetting.objects, "get_effective", side_effect=RuntimeError("db down")):
            assert dash_gate_fail_open() is False
