"""Each health band fails open on its own — one raising reader never 500s the page (#3164).

SHOULD-FIX #3: the module docstring promises per-band fail-open, but only
``_spend_summary`` was guarded. A raising verdict/loops/mode reader used to
propagate and blank the whole ``/dash/health/`` page.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from teatree.core.models.config_setting import ConfigSetting
from teatree.dash import health_bands


class PerBandFailOpenTestCase(TestCase):
    def test_raising_verdict_reader_degrades_only_its_band(self) -> None:
        with patch.object(health_bands, "read_health", side_effect=RuntimeError("verdict boom")):
            view = health_bands.build_health_view()
        # the failed band carries a visible error…
        assert view.verdict.error is not None
        assert view.verdict.status == "error"
        # …and the other three bands are unaffected.
        assert view.loops.error is None
        assert view.capacity.error is None
        assert view.mode.error is None

    def test_raising_mode_reader_degrades_only_its_band(self) -> None:
        with patch.object(health_bands, "resolve_mode", side_effect=RuntimeError("mode boom")):
            view = health_bands.build_health_view()
        assert view.mode.error is not None
        assert view.verdict.error is None
        assert view.loops.error is None
        assert view.capacity.error is None

    def test_gate_read_failure_fails_closed_to_false(self) -> None:
        with patch.object(ConfigSetting.objects, "get_effective", side_effect=RuntimeError("db down")):
            assert health_bands._gate_fail_open() is False

    def test_health_page_survives_a_raising_band(self) -> None:
        # The page still renders 200 with the degraded band's error visible.
        with patch.object(health_bands, "read_health", side_effect=RuntimeError("verdict boom")):
            response = self.client.get(reverse("dash:health"))
        assert response.status_code == 200
        assert "verdict band unavailable" in response.content.decode()
