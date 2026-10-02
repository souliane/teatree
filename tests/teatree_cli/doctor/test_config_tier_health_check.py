"""``t3 doctor check`` surfaces a degraded ConfigSetting override tier (#3873).

The fault the check exists for is invisible by construction: a runtime read failure
resolves every DB-home setting to a shipped default and says so only in a worker log
nobody tails. The resolver records the degradation beside the control DB; this is the
surface that makes an operator see it without reading that log.

An ABSENT marker is healthy and silent; a marker that exists and does not parse is not.
The two look identical to the reader that resolves a value (both yield "no report"), which
is how the recorded degradation stayed unreported — so the check separates them here.
"""

import datetime as dt
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

    def test_the_first_failure_dates_the_fault(self, tmp_path: Path, capsys) -> None:
        # Occurrences alone cannot separate a transient lock from a degradation
        # that has been resolving every gate to a shipped default for days.
        first_seen = dt.datetime(2026, 8, 2, 9, 30, tzinfo=dt.UTC)
        marker = _marker(
            tmp_path,
            {
                "scopes": ["global"],
                "occurrences": 639,
                "first_seen": first_seen.timestamp(),
                "last_seen": time.time(),
            },
        )
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is False
        out = capsys.readouterr().out
        assert "639 time(s)" in out
        assert f"first seen {first_seen.isoformat(timespec='seconds')}" in out

    def test_the_recorded_caller_is_named(self, tmp_path: Path, capsys) -> None:
        # #3980: the fault this tier actually hit is deterministic — a sync ORM read from an
        # async frame — so "which call site" is the whole diagnosis, and the traceback at the
        # read never carries it.
        marker = _marker(
            tmp_path,
            {
                "scopes": ["global"],
                "callers": ["runner.py:198 in _run_agent"],
                "occurrences": 4,
                "first_seen": time.time(),
                "last_seen": time.time(),
            },
        )
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is False
        out = capsys.readouterr().out
        assert "runner.py:198 in _run_agent" in out
        assert "async frame" in out

    def test_a_marker_without_callers_still_reports(self, tmp_path: Path, capsys) -> None:
        # The foil: a marker written before the field existed must not break the surface.
        marker = _marker(
            tmp_path,
            {"scopes": ["global"], "occurrences": 1, "first_seen": time.time(), "last_seen": time.time()},
        )
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is False
        assert "Called from" not in capsys.readouterr().out

    def test_no_marker_passes_silently(self, tmp_path: Path, capsys) -> None:
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=tmp_path / "absent.json"):
            assert _check_config_override_tier_healthy() is True
        assert capsys.readouterr().out == ""

    def test_a_stale_marker_is_not_a_current_fault(self, tmp_path: Path) -> None:
        marker = _marker(tmp_path, {"scopes": ["global"], "occurrences": 1, "first_seen": 0.0, "last_seen": 0.0})
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is True

    def test_an_unparsable_marker_is_reported_rather_than_read_as_healthy(self, tmp_path: Path, capsys) -> None:
        # The foil for the case above is ABSENCE, not corruption: a file teatree wrote and
        # cannot parse leaves the tier's health unknown, and unknown must not read as healthy.
        marker = _marker(tmp_path, "{not json")
        with mock.patch("teatree.config.override_read_health.marker_path", return_value=marker):
            assert _check_config_override_tier_healthy() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert str(marker) in out
        assert "could not be read" in out

    def test_a_raising_reader_warns_rather_than_reddening_the_run(self, tmp_path: Path, capsys) -> None:
        with mock.patch("teatree.config.override_read_health.degraded_read_report", side_effect=RuntimeError("boom")):
            assert _check_config_override_tier_healthy() is True
        assert "WARN" in capsys.readouterr().out
