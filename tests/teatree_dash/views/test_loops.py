"""Loop-control POSTs drive the paired atomic verbs + are CSRF-protected + audited (#3162)."""

import re

from django.http import HttpResponse
from django.test import Client, TestCase
from django.urls import reverse

from teatree.core.mode_resolution import clear_mode_override, resolve_active_mode
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.dash.loop_control import LOOP_ACTIONS, POSTURE_ACTIONS


def _make_loop(name: str = "dashloop") -> Loop:
    return Loop.objects.create(name=name, script="teatree.loops.review", delay_seconds=60)


def test_control_verbs_are_the_four_paired_actions() -> None:
    assert {"pause", "resume", "disable", "enable"} == LOOP_ACTIONS


def test_posture_actions_cover_the_switch() -> None:
    assert {"reachable", "defer-questions", "pause-everything", "auto"} == POSTURE_ACTIONS


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


class PosturePostTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:posture")
        self.addCleanup(clear_mode_override)

    def test_pause_everything_sets_the_offline_mode_override(self) -> None:
        # The posture token is resolved to the mode carrying it BY ROW: "pause
        # everything" lands on the seeded holiday 'offline' mode (migration 0022).
        self.client.post(self.url, {"posture": "pause-everything"})
        resolved = resolve_active_mode()
        assert resolved.name == "offline"
        assert resolved.defers_questions is True
        assert resolved.pauses_self_pump is True

    def test_auto_clears_the_override(self) -> None:
        self.client.post(self.url, {"posture": "pause-everything"})
        self.client.post(self.url, {"posture": "auto"})
        assert resolve_active_mode().source == "default"

    def test_unknown_posture_rejected(self) -> None:
        resp = self.client.post(self.url, {"posture": "banana"})
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


class LoopsHtmxSwapTestCase(TestCase):
    """A loop control POST answers the page body, not a full-document redirect.

    Every mutating POST on this page ended in ``redirect("dash:loops")``, so the
    browser navigated and landed at scroll 0 — the same defect #3760 fixed for the
    settings rows, still present on all five.
    """

    def setUp(self) -> None:
        self.loop = Loop.objects.create(name="demo", delay_seconds=60, script="run.py", enabled=True)

    def _post(self, name: str, data: dict[str, str], *, htmx: bool = True) -> HttpResponse:
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(reverse(name), data, **headers)

    def test_an_htmx_loop_action_answers_the_body_fragment(self) -> None:
        response = self._post("dash:loop_action", {"name": "demo", "action": "pause"})
        assert response.status_code == 200
        body = response.content.decode()
        assert "<!doctype html>" not in body.lower()
        assert "loops-table" in body

    def test_the_answered_body_carries_the_verb_the_action_just_produced(self) -> None:
        """The swap-in must SHOW the new state — a 200 carrying the stale verb swaps nothing."""
        response = self._post("dash:loop_action", {"name": "demo", "action": "pause"})
        row = next(r for r in re.findall(r"<tr>.*?</tr>", response.content.decode(), re.DOTALL) if ">demo<" in r)
        assert 'value="resume"' in row
        assert 'value="pause"' not in row

    def test_the_e2e_fixture_loop_shape_also_swaps_to_the_resume_verb(self) -> None:
        """The browser lane's loop carries a REGISTRY script name — reproduce it exactly."""
        Loop.objects.create(name="e2e_loop", script="teatree.loops.review", delay_seconds=60)
        response = self._post("dash:loop_action", {"name": "e2e_loop", "action": "pause"})
        row = next(r for r in re.findall(r"<tr>.*?</tr>", response.content.decode(), re.DOTALL) if ">e2e_loop<" in r)
        assert 'value="resume"' in row

    def test_a_no_js_loop_action_keeps_the_redirect(self) -> None:
        response = self._post("dash:loop_action", {"name": "demo", "action": "pause"}, htmx=False)
        assert response.status_code == 302

    def test_every_mutating_form_on_the_page_is_wired_to_swap(self) -> None:
        body = self.client.get(reverse("dash:loops")).content.decode()
        for action in ("dash:loop_action", "dash:posture", "dash:gate_toggle", "dash:runner_toggle"):
            marker = f'hx-post="{reverse(action)}"'
            assert marker in body, f"{action} form is not wired to an htmx swap"

    def test_a_refused_write_answers_the_body_with_its_reason_not_a_dead_end(self) -> None:
        response = self._post("dash:loop_action", {"name": "demo", "action": "not-a-verb"})
        assert response.status_code == 400
        body = response.content.decode()
        assert "loops-table" in body
        assert "not-a-verb" in body

    def test_a_no_js_refusal_renders_a_page_with_navigation(self) -> None:
        response = self._post("dash:loop_action", {"name": "demo", "action": "not-a-verb"}, htmx=False)
        assert response.status_code == 400
        body = response.content.decode()
        assert "<!doctype html>" in body.lower()
        assert reverse("dash:loops") in body
