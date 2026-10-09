"""The Socket Mode listener heartbeat detector.

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

from teatree.cli.doctor.self_heal_slack_listener import (
    _HEARTBEAT_FILENAME,
    ListenerBeat,
    check_slack_listener_alive,
    read_heartbeat,
)

_MOD = "teatree.cli.doctor.self_heal_slack_listener"


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


class SlackListenerCheckTest(TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self._patch = mock.patch(f"{_MOD}.DATA_DIR", Path(self._dir))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write_beat(self, *, age_seconds: int, interval: int = 15) -> None:
        beat = {"updated_at": int(time.time()) - age_seconds, "interval_seconds": interval}
        (Path(self._dir) / "slack-listener-heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")

    def test_absent_heartbeat_degrades_to_pass(self) -> None:
        ok, out = _echoes(check_slack_listener_alive)
        assert ok is True
        assert out == ""

    def test_fresh_heartbeat_is_ok(self) -> None:
        self._write_beat(age_seconds=5)
        ok, out = _echoes(check_slack_listener_alive)
        assert ok is True
        assert out == ""

    def test_stale_heartbeat_fails(self) -> None:
        # No refresh for well past max(4x interval, 120s) — every WebSocket is gone.
        self._write_beat(age_seconds=600)
        ok, out = _echoes(check_slack_listener_alive)
        assert ok is False
        assert "FAIL" in out
        assert "stale" in out

    def test_a_beat_inside_the_floor_is_ok(self) -> None:
        # The floor, not the multiplier, governs the 15s cadence the receiver writes at.
        self._write_beat(age_seconds=100)
        ok, out = _echoes(check_slack_listener_alive)
        assert ok is True
        assert out == ""

    def test_unparsable_heartbeat_degrades_to_pass(self) -> None:
        (Path(self._dir) / "slack-listener-heartbeat.json").write_text("{not json", encoding="utf-8")
        ok, out = _echoes(check_slack_listener_alive)
        assert ok is True
        assert out == ""

    def test_a_crash_reading_the_clock_degrades_to_pass(self) -> None:
        # A self-heal probe must never itself abort the doctor run: an unexpected
        # error inside the check's try-block WARNs and passes rather than crashing.
        self._write_beat(age_seconds=5)
        with mock.patch("django.utils.timezone.now", side_effect=RuntimeError("clock gone")):
            ok, out = _echoes(check_slack_listener_alive)
        assert ok is True
        assert "WARN" in out
        assert "crashed" in out

    def test_read_heartbeat_parses_a_real_beat(self) -> None:
        self._write_beat(age_seconds=5, interval=30)
        beat = read_heartbeat()
        assert isinstance(beat, ListenerBeat)
        assert beat.interval_seconds == 30

    def test_read_heartbeat_returns_none_when_absent(self) -> None:
        assert read_heartbeat() is None

    def test_the_filename_is_the_one_the_receiver_writes(self) -> None:
        # The doctor reads this from ANOTHER container, so a filename that drifts from
        # the writer's reports a live receiver as dead.
        from teatree.backends.slack.receiver import default_heartbeat_path  # noqa: PLC0415 — test-local

        assert default_heartbeat_path().name == _HEARTBEAT_FILENAME
