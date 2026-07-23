"""The slack-drain sidecar heartbeat detector.

Functional: writes a real heartbeat JSON to a tmp ``DATA_DIR`` and runs the check,
so the parse and the FAIL/degrade logic are exercised together. An absent or
unparsable heartbeat degrades to a pass — a self-heal detector must never itself
abort the doctor run.
"""

import io
import json
import tempfile
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.cli.doctor import self_heal_slack_drain
from teatree.cli.doctor.self_heal_slack_drain import check_slack_drain_alive

_MOD = "teatree.cli.doctor.self_heal_slack_drain"


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


class SlackDrainCheckTest(TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self._patch = mock.patch(f"{_MOD}.DATA_DIR", Path(self._dir))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write_beat(self, *, age_seconds: int, consecutive: int, interval: int = 15) -> None:
        beat = {
            "updated_at": int(time.time()) - age_seconds,
            "interval_seconds": interval,
            "consecutive_failures": consecutive,
            "last_ok_at": int(time.time()) - age_seconds,
        }
        (Path(self._dir) / "slack-drain-heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")

    def test_absent_heartbeat_degrades_to_pass(self) -> None:
        ok, out = _echoes(check_slack_drain_alive)
        assert ok is True
        assert out == ""

    def test_fresh_healthy_heartbeat_is_ok(self) -> None:
        self._write_beat(age_seconds=5, consecutive=0)
        ok, out = _echoes(check_slack_drain_alive)
        assert ok is True
        assert out == ""

    def test_stale_heartbeat_fails(self) -> None:
        # No refresh for well past max(4x interval, 120s) — the drain loop died/hung.
        self._write_beat(age_seconds=600, consecutive=0)
        ok, out = _echoes(check_slack_drain_alive)
        assert ok is False
        assert "FAIL" in out
        assert "stale" in out

    def test_consecutive_failures_fail(self) -> None:
        self._write_beat(age_seconds=5, consecutive=self_heal_slack_drain._MAX_CONSECUTIVE_FAILURES)
        ok, out = _echoes(check_slack_drain_alive)
        assert ok is False
        assert "FAIL" in out
        assert "failed" in out

    def test_a_few_failures_but_fresh_is_ok(self) -> None:
        # Below the threshold and freshly beating — a transient blip, not a break.
        self._write_beat(age_seconds=5, consecutive=self_heal_slack_drain._MAX_CONSECUTIVE_FAILURES - 1)
        ok, out = _echoes(check_slack_drain_alive)
        assert ok is True
        assert out == ""

    def test_unparsable_heartbeat_degrades_to_pass(self) -> None:
        (Path(self._dir) / "slack-drain-heartbeat.json").write_text("{not json", encoding="utf-8")
        ok, out = _echoes(check_slack_drain_alive)
        assert ok is True
        assert out == ""
