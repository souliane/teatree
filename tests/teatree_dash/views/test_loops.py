"""Loop-control POSTs drive the paired atomic verbs + are CSRF-protected + audited (#3162)."""

import re

from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.dash.loop_control import AVAILABILITY_ACTIONS, LOOP_ACTIONS
from teatree.core.mode_resolution import clear_mode_override, resolve_active_mode


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
        self.addCleanup(clear_mode_override)

    def test_switch_to_away_sets_the_offline_mode_override(self) -> None:
        # The standalone availability modes are gone: "away" now sets the merged
        # holiday 'offline' mode (seeded by migration 0022) as the override.
        self.client.post(self.url, {"mode": "away"})
        resolved = resolve_active_mode()
        assert resolved.name == "offline"
        assert resolved.defers_questions is True
        assert resolved.pauses_self_pump is True

    def test_auto_clears_the_override(self) -> None:
        self.client.post(self.url, {"mode": "away"})
        self.client.post(self.url, {"mode": "auto"})
        assert resolve_active_mode().source == "default"

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


class LoopsTableContextualVerbsTestCase(TestCase):
    """The loops table shows only the applicable verb of each pair (#3162 redesign).

    pause XOR resume by the LoopState hold; disable XOR enable by ``Loop.enabled`` —
    never all four, so the affordance always says what actually applies.
    """

    def _row_for(self, name: str) -> str:
        body = self.client.get(reverse("dash:loops_table")).content.decode()
        rows = re.findall(r"<tr>.*?</tr>", body, re.DOTALL)
        matching = [row for row in rows if f">{name}<" in row]
        assert matching, f"no loops-table row for {name!r}"
        return matching[0]

    def test_paused_loop_offers_resume_not_pause(self) -> None:
        _make_loop("ctxpaused")
        LoopState.objects.pause("ctxpaused")
        row = self._row_for("ctxpaused")
        assert 'value="resume"' in row
        assert 'value="pause"' not in row

    def test_disabled_loop_offers_enable_not_disable(self) -> None:
        _make_loop("ctxdisabled")
        Loop.objects.disable("ctxdisabled")
        row = self._row_for("ctxdisabled")
        assert 'value="enable"' in row
        assert 'value="disable"' not in row

    def test_running_loop_offers_pause_and_disable_only(self) -> None:
        _make_loop("ctxrunning")
        row = self._row_for("ctxrunning")
        assert 'value="pause"' in row
        assert 'value="disable"' in row
        assert 'value="resume"' not in row
        assert 'value="enable"' not in row
