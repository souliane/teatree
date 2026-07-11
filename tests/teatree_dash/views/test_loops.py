"""Loop-control POSTs drive the paired atomic verbs + are CSRF-protected + audited (#3162)."""

from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.availability import clear_override, resolve_mode
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.dash.loop_control import AVAILABILITY_ACTIONS, LOOP_ACTIONS


def _make_loop(name: str = "dashloop") -> Loop:
    return Loop.objects.create(name=name, script="teatree.loops.review", delay_seconds=60)


def test_control_verbs_are_the_four_paired_actions() -> None:
    assert {"pause", "resume", "disable", "enable"} == LOOP_ACTIONS


def test_availability_actions_cover_the_switch() -> None:
    assert {"present", "away", "autonomous_away", "auto"} == AVAILABILITY_ACTIONS


class LoopActionPostTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:loop_action")
        self.loop = _make_loop()

    def test_pause_sets_paused_hold_without_disabling(self) -> None:
        self.client.post(self.url, {"name": "dashloop", "action": "pause"})
        assert LoopState.objects.status_of("dashloop") is LoopStatus.PAUSED
        # pause is the reversible hold — Loop.enabled must stay True.
        assert Loop.objects.get(name="dashloop").enabled is True

    def test_disable_moves_both_planes(self) -> None:
        self.client.post(self.url, {"name": "dashloop", "action": "disable"})
        assert LoopState.objects.status_of("dashloop") is LoopStatus.DISABLED
        assert Loop.objects.get(name="dashloop").enabled is False

    def test_enable_returns_both_planes_to_enabled(self) -> None:
        Loop.objects.disable("dashloop")
        self.client.post(self.url, {"name": "dashloop", "action": "enable"})
        assert LoopState.objects.status_of("dashloop") is LoopStatus.ENABLED
        assert Loop.objects.get(name="dashloop").enabled is True

    def test_resume_clears_a_pause(self) -> None:
        LoopState.objects.pause("dashloop")
        self.client.post(self.url, {"name": "dashloop", "action": "resume"})
        assert LoopState.objects.status_of("dashloop") is LoopStatus.ENABLED

    def test_unknown_action_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"name": "dashloop", "action": "nuke"})
        assert resp.status_code == 400

    def test_unknown_loop_is_rejected(self) -> None:
        resp = self.client.post(self.url, {"name": "nope", "action": "pause"})
        assert resp.status_code == 400

    def test_action_is_audited(self) -> None:
        with self.assertLogs("teatree.dash.audit", level="INFO") as logs:
            self.client.post(self.url, {"name": "dashloop", "action": "disable"})
        assert any("action=loop:disable" in line and "target=dashloop" in line for line in logs.output)

    def test_csrf_is_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.post(self.url, {"name": "dashloop", "action": "pause"})
        assert resp.status_code == 403


class AvailabilityPostTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:availability")
        self.addCleanup(clear_override)

    def test_switch_to_away_writes_override(self) -> None:
        self.client.post(self.url, {"mode": "away"})
        assert resolve_mode().mode == "away"

    def test_auto_clears_the_override(self) -> None:
        self.client.post(self.url, {"mode": "away"})
        self.client.post(self.url, {"mode": "auto"})
        assert resolve_mode().source == "default"

    def test_unknown_mode_rejected(self) -> None:
        resp = self.client.post(self.url, {"mode": "banana"})
        assert resp.status_code == 400


class GateTogglePostTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:gate_toggle")

    def test_enable_requires_typed_confirm(self) -> None:
        resp = self.client.post(self.url, {"enable": "1", "confirm": "wrong"})
        assert resp.status_code == 400
        assert ConfigSetting.objects.get_effective("danger_gate_fail_open") is None

    def test_enable_with_correct_confirm_sets_the_switch(self) -> None:
        self.client.post(self.url, {"enable": "1", "confirm": "fail-open"})
        assert ConfigSetting.objects.get_effective("danger_gate_fail_open") is True

    def test_disable_needs_no_confirm(self) -> None:
        ConfigSetting.objects.set_value("danger_gate_fail_open", value=True)
        self.client.post(self.url, {"enable": "0"})
        assert ConfigSetting.objects.get_effective("danger_gate_fail_open") is False

    def test_toggle_is_audited(self) -> None:
        with self.assertLogs("teatree.dash.audit", level="INFO") as logs:
            self.client.post(self.url, {"enable": "1", "confirm": "fail-open"})
        assert any("action=gate:danger_gate_fail_open" in line for line in logs.output)
