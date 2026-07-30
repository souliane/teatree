"""The unified loop table: deciding layer per layer, plus cadence editing via the seam (#3559)."""

import datetime as dt
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.models import Loop, Mode, ModeOverride
from teatree.core.models.loop_state import LoopState
from teatree.dash.loop_control import build_loop_rows


def _loop(name: str, *, enabled: bool = True, delay_seconds: int = 60) -> Loop:
    loop, _ = Loop.objects.update_or_create(
        name=name,
        defaults={
            "script": f"src/teatree/loops/{name}/loop.py",
            "delay_seconds": delay_seconds,
            "daily_at": None,
            "enabled": enabled,
        },
    )
    return loop


def _row(name: str) -> object:
    return next(row for row in build_loop_rows() if row.name == name)


class DecidingLayerTestCase(TestCase):
    """One loop decided at each layer renders the layer that actually decided it."""

    def setUp(self) -> None:
        _loop("review", enabled=False)
        _loop("inbox", enabled=True)
        _loop("dream", enabled=True)
        self.addCleanup(ModeOverride.objects.clear)

    def test_base_layer_when_no_preset_holds_an_opinion(self) -> None:
        assert _row("review").deciding_layer.startswith("L1")

    def test_preset_layer_when_the_active_preset_masks_the_loop(self) -> None:
        Mode.objects.update_or_create(name="engaged", defaults={"entries": {"inbox": False}})
        ModeOverride.objects.set_override("engaged")
        row = _row("inbox")
        assert row.deciding_layer.startswith("L3 override")
        assert row.effective is False

    def test_hold_layer_when_a_loop_state_hold_is_in_force(self) -> None:
        LoopState.objects.pause("dream")
        assert _row("dream").deciding_layer.startswith("L4 hold")

    def test_rows_carry_the_cadence_and_schedule_columns(self) -> None:
        row = _row("inbox")
        assert row.cadence_label == "every 60s"
        assert row.delay_seconds == 60
        assert row.bounds.min_interval_seconds > 0


class CadencePostTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:loop_cadence")
        _loop("inbox", delay_seconds=60)

    def test_setting_an_interval_persists(self) -> None:
        self.client.post(self.url, {"name": "inbox", "delay_seconds": "600"})
        assert Loop.objects.get(name="inbox").delay_seconds == 600

    def test_setting_a_wall_clock_time_persists_and_clears_the_interval_mode(self) -> None:
        self.client.post(self.url, {"name": "inbox", "daily_at": "08:15"})
        row = Loop.objects.get(name="inbox")
        assert row.daily_at == dt.time(8, 15)
        assert row.cadence_label == "daily 08:15"

    def test_supplying_both_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"name": "inbox", "delay_seconds": "600", "daily_at": "08:15"})
        assert resp.status_code == 400
        assert Loop.objects.get(name="inbox").delay_seconds == 60

    def test_a_zero_interval_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"name": "inbox", "delay_seconds": "0"})
        assert resp.status_code == 400
        assert Loop.objects.get(name="inbox").delay_seconds == 60

    def test_below_the_registry_floor_is_rejected(self) -> None:
        _loop("resource_pressure", delay_seconds=60)
        resp = self.client.post(self.url, {"name": "resource_pressure", "delay_seconds": "86400"})
        assert resp.status_code == 400
        assert Loop.objects.get(name="resource_pressure").delay_seconds == 60

    def test_write_goes_through_the_service_seam(self) -> None:
        with mock.patch("teatree.dash.views.loops.set_loop_cadence") as seam:
            self.client.post(self.url, {"name": "inbox", "delay_seconds": "600"})
        seam.assert_called_once_with("inbox", delay_seconds=600, daily_at="")

    def test_csrf_is_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        assert csrf_client.post(self.url, {"name": "inbox", "delay_seconds": "600"}).status_code == 403


class UnifiedLoopsPageTestCase(TestCase):
    def setUp(self) -> None:
        _loop("inbox")

    def test_page_carries_one_loop_table_plus_a_distinct_infra_section(self) -> None:
        content = self.client.get(reverse("dash:loops")).content
        assert b"Infra slots" in content
        assert b"deciding layer" in content

    def test_page_steers_to_the_preset_editor_as_the_normal_handle(self) -> None:
        content = self.client.get(reverse("dash:loops")).content
        assert b"emergency handle" in content
        assert reverse("dash:presets").encode() in content
