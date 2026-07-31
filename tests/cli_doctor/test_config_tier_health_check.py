"""``t3 doctor check`` surfaces a degraded ConfigSetting override tier (#3873).

The fault the check exists for is invisible by construction: a runtime read failure
resolves every DB-home setting to a shipped default and says so only in a worker log
nobody tails. The resolver records the degradation beside the control DB; this is the
surface that makes an operator see it without reading that log.

Fail-open is pinned here too. A health check that reddens because it could not read its
own evidence teaches operators to ignore it, which costs more than the fault it reports.
"""

import json
import time
from pathlib import Path
from unittest import mock

from teatree.cli.doctor.checks_cold_hooks import _check_config_override_tier_healthy


def _marker(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "config-read-degraded.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, encoding="utf-8")
    return path


class TestTheDegradedTierIsReported:
    def test_a_recent_degradation_fails_the_check(self, tmp_path: Path, capsys) -> None:
        marker = _marker(
            tmp_path,
            {"scopes": ["global"], "occurrences": 4, "first_seen": time.time(), "last_seen": time.time()},
        )
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        # The operator needs the blast radius, not just the fact: while degraded the gates
        # run MORE restrictively than configured, which looks like the factory stalling.
        assert "restrictive" in out.lower()
        assert "global" in out

    def test_no_marker_passes_silently(self, tmp_path: Path, capsys) -> None:
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=tmp_path / "absent.json"):
            assert _check_config_override_tier_healthy() is True
        assert capsys.readouterr().out == ""

    def test_a_stale_marker_is_not_a_current_fault(self, tmp_path: Path) -> None:
        marker = _marker(tmp_path, {"scopes": ["global"], "occurrences": 1, "first_seen": 0.0, "last_seen": 0.0})
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is True

    def test_an_unparsable_marker_fails_open(self, tmp_path: Path) -> None:
        # The foil for the case above: unreadable evidence is not evidence of a fault.
        marker = _marker(tmp_path, "{not json")
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is True

    def test_a_raising_reader_warns_rather_than_reddening_the_run(self, tmp_path: Path, capsys) -> None:
        with mock.patch("teatree.config.override_read_health.degraded_read_report", side_effect=RuntimeError("boom")):
            assert _check_config_override_tier_healthy() is True
        assert "WARN" in capsys.readouterr().out
