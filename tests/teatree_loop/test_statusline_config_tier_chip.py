"""The config-tier degradation reaches a session that is looking at the statusline.

The marker had ONE consumer — `cli/doctor/checks_cold_hooks` — inside `t3 doctor check`,
which `hook_router` tells agents not to run. So a box resolving `autonomy` to `babysit`
and `require_human_approval_to_merge` to `true` because it could not read the store said
nothing anywhere a session looks, and every gate quietly ran on a value nobody chose.
"""

import time
from unittest.mock import patch

from teatree.config.override_read_health import DegradedReadReport
from teatree.loop.statusline import config_tier_chip


def _report(*, age_seconds: float = 0.0) -> DegradedReadReport:
    now = time.time() - age_seconds
    return DegradedReadReport(scopes=("(global)",), occurrences=3, first_seen=now, last_seen=now)


class TestTheChipAppearsOnlyWhenTheTierIsDegraded:
    def test_a_healthy_tier_costs_no_statusline_room(self) -> None:
        with patch("teatree.config.override_read_health.degraded_read_report", return_value=None):
            assert config_tier_chip() == []

    def test_a_live_fault_says_the_stored_settings_are_not_the_ones_in_force(self) -> None:
        with patch("teatree.config.override_read_health.degraded_read_report", return_value=_report()):
            (chip,) = config_tier_chip()
        assert "config: UNREADABLE" in chip
        assert "fail-closed" in chip

    def test_a_stale_marker_is_not_evidence_about_now(self) -> None:
        # A marker past its TTL records a fault that may long since have healed; the chip
        # is a live alarm, so it goes quiet rather than accusing a healthy box forever.
        with patch("teatree.config.override_read_health.degraded_read_report", return_value=_report(age_seconds=10**6)):
            assert config_tier_chip() == []

    def test_a_broken_read_never_blanks_the_statusline(self) -> None:
        with patch("teatree.config.override_read_health.degraded_read_report", side_effect=OSError("boom")):
            assert config_tier_chip() == []
