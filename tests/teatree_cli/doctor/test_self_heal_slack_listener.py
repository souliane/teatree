"""The Socket Mode listener heartbeat detector.

Functional: writes a real heartbeat JSON to a tmp ``DATA_DIR``, seeds the DB overlay
registry and runs the check, so the parse, the per-overlay judgement and the FAIL
logic are exercised together.
"""

import io
import json
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase

from teatree.cli.doctor.self_heal_slack_listener import check_slack_listener_alive
from teatree.core.models import ConfigSetting
from teatree.core.slack_first.factory_slack_liveness import factory_slack_live
from teatree.core.slack_first.slack_listener_heartbeat import FILENAME
from tests._loop_principal_env import pinned_loop_principal


@pytest.fixture(autouse=True)
def _no_ambient_loop_principal() -> Generator[None]:
    with pinned_loop_principal():
        yield


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


class SlackListenerCheckTest(TestCase):
    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self._patch = mock.patch("teatree.core.slack_first.slack_listener_heartbeat.DATA_DIR", self._dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _configure(self, *names: str) -> None:
        ConfigSetting.objects.set_value(
            "overlays", {name: {"messaging_backend": "slack", "slack_token_ref": f"slack/{name}"} for name in names}
        )

    def _write_beat(self, stamps: dict[str, int], *, interval: int = 15) -> None:
        now = int(time.time())
        overlays = {name: now - age for name, age in stamps.items()}
        beat = {"updated_at": max(overlays.values()), "interval_seconds": interval, "overlays": overlays}
        (self._dir / FILENAME).write_text(json.dumps(beat), encoding="utf-8")

    def test_no_slack_overlay_passes(self) -> None:
        ConfigSetting.objects.set_value("overlays", {"ov": {"messaging_backend": "noop"}})

        ok, out = _echoes(check_slack_listener_alive)

        assert ok is True
        assert out == ""

    def test_absent_heartbeat_with_a_slack_overlay_fails(self) -> None:
        self._configure("ov")

        ok, out = _echoes(check_slack_listener_alive)

        assert ok is False
        assert "FAIL" in out
        assert "ov has no heartbeat" in out

    def test_fresh_heartbeat_is_ok(self) -> None:
        self._configure("ov")
        self._write_beat({"ov": 5})

        ok, out = _echoes(check_slack_listener_alive)

        assert ok is True
        assert out == ""

    def test_stale_heartbeat_fails(self) -> None:
        self._configure("ov")
        self._write_beat({"ov": 600})

        ok, out = _echoes(check_slack_listener_alive)

        assert ok is False
        assert "stale" in out

    def test_a_dead_second_overlay_fails_while_the_first_is_fresh(self) -> None:
        self._configure("alive", "dead")
        self._write_beat({"alive": 5, "dead": 600})

        ok, out = _echoes(check_slack_listener_alive)

        assert ok is False
        assert "dead heartbeat is stale" in out
        assert "alive" not in out

    def test_a_beat_inside_the_floor_is_ok(self) -> None:
        self._configure("ov")
        self._write_beat({"ov": 100})

        ok, out = _echoes(check_slack_listener_alive)

        assert ok is True
        assert out == ""

    def test_a_crash_reading_the_registry_degrades_to_pass(self) -> None:
        with mock.patch(
            "teatree.cli.doctor.self_heal_slack_listener.read_overlay_registry",
            side_effect=RuntimeError("db gone"),
        ):
            ok, out = _echoes(check_slack_listener_alive)

        assert ok is True
        assert "WARN" in out
        assert "crashed" in out


def test_absent_heartbeat_still_passes_the_lenient_doctor_probe(db: None, tmp_path: Path) -> None:
    ConfigSetting.objects.set_value("overlays", {"ov": {"messaging_backend": "noop"}})

    with (
        mock.patch("teatree.core.slack_first.slack_listener_heartbeat.DATA_DIR", tmp_path),
        mock.patch("teatree.core.backend_factory.OverlayConfigResolver.all_names", return_value=[]),
    ):
        doctor_ok, out = _echoes(check_slack_listener_alive)
        verdict = factory_slack_live("ov")

    assert doctor_ok is True
    assert out == ""
    assert not verdict.live
