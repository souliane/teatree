"""Loop-control POSTs drive the paired atomic verbs + are CSRF-protected + audited (#3162)."""

import re

import pytest
from django.http import HttpResponse
from django.test import Client, TestCase
from django.urls import NoReverseMatch, reverse

from teatree.core.mode_resolution import clear_mode_override, resolve_active_mode
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.loop import Loop
from teatree.core.models.loop_preset import Mode
from teatree.dash.loop_control import MODE_SWITCH_AUTO


def _make_loop(name: str = "dashloop") -> Loop:
    return Loop.objects.create(name=name, script="teatree.loops.review", delay_seconds=60)


def test_auto_is_the_one_switch_value_that_is_not_a_mode_name() -> None:
    assert MODE_SWITCH_AUTO == "auto"


class ModeSwitchPostTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:mode-switch")
        Mode.objects.get_or_create(name="off", defaults={"entries": {}})
        self.addCleanup(clear_mode_override)

    def test_naming_a_mode_sets_the_override(self) -> None:
        self.client.post(self.url, {"mode": "off"})
        resolved = resolve_active_mode()
        assert resolved.source == "override"
        assert resolved.name == "off"

    def test_auto_clears_the_override(self) -> None:
        self.client.post(self.url, {"mode": "off"})
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


class LoopsTableTagsTestCase(TestCase):
    """The declared reach/determinism tags render as chips on each loop's row."""

    def _row_for(self, name: str) -> str:
        body = self.client.get(reverse("dash:loops_table")).content.decode()
        rows = re.findall(r"<tr>.*?</tr>", body, re.DOTALL)
        matching = [row for row in rows if f">{name}<" in row]
        assert matching, f"no loops-table row for {name!r}"
        return matching[0]

    def test_colleague_reaching_ai_loop_shows_every_tag(self) -> None:
        row = self._row_for("review")
        for tag in ("ingress", "egress", "colleague", "ai"):
            assert f'<span class="chip small">{tag}</span>' in row

    def test_local_only_loop_shows_deterministic_alone(self) -> None:
        row = self._row_for("db_backup")
        assert '<span class="chip small">deterministic</span>' in row
        assert ">ingress<" not in row


class LoopsHtmxSwapTestCase(TestCase):
    """A mutating POST on this page answers the page body, not a full-document redirect.

    Every mutating POST on this page ended in ``redirect("dash:loops")``, so the
    browser navigated and landed at scroll 0 — the same defect #3760 fixed for the
    settings rows. Pinned on the preset switch now that per-loop enablement is
    read-only here (C2); the property is the SWAP, not which verb produced it.
    """

    def setUp(self) -> None:
        # ``enabled=None`` is NO manual opinion, so the preset is what decides — a manual
        # ``True`` would beat the switch and the swap would show nothing changing.
        self.loop = Loop.objects.create(name="demo", delay_seconds=60, script="run.py", enabled=None)
        Mode.objects.create(name="afk", entries={"demo": False})

    def _post(self, name: str, data: dict[str, str], *, htmx: bool = True) -> HttpResponse:
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(reverse(name), data, **headers)

    def test_an_htmx_switch_answers_the_body_fragment(self) -> None:
        response = self._post("dash:mode-switch", {"mode": "afk"})
        assert response.status_code == 200
        body = response.content.decode()
        assert "<!doctype html>" not in body.lower()
        assert "loops-table" in body

    def test_the_answered_body_carries_the_state_the_switch_just_produced(self) -> None:
        """The swap-in must SHOW the new state — a 200 carrying the stale one swaps nothing."""
        response = self._post("dash:mode-switch", {"mode": "afk"})
        row = next(r for r in re.findall(r"<tr>.*?</tr>", response.content.decode(), re.DOTALL) if ">demo<" in r)
        assert "held" in row, "the swapped row still shows the pre-switch verdict"

    def test_a_registry_script_loop_renders_in_the_answered_body(self) -> None:
        """The browser lane's loop carries a REGISTRY script name — reproduce it exactly."""
        Loop.objects.create(name="e2e_loop", script="teatree.loops.review", delay_seconds=60)
        response = self._post("dash:mode-switch", {"mode": "afk"})
        assert ">e2e_loop<" in response.content.decode()

    def test_a_no_js_switch_keeps_the_redirect(self) -> None:
        response = self._post("dash:mode-switch", {"mode": "afk"}, htmx=False)
        assert response.status_code == 302

    def test_every_mutating_form_on_the_page_is_wired_to_swap(self) -> None:
        body = self.client.get(reverse("dash:loops")).content.decode()
        for action in ("dash:loop_cadence", "dash:mode-switch", "dash:gate_toggle"):
            marker = f'hx-post="{reverse(action)}"'
            assert marker in body, f"{action} form is not wired to an htmx swap"

    def test_a_refused_write_answers_the_body_with_its_reason_not_a_dead_end(self) -> None:
        response = self._post("dash:mode-switch", {"mode": "not-a-preset"})
        assert response.status_code == 400
        body = response.content.decode()
        assert "loops-table" in body
        assert "not-a-preset" in body

    def test_a_no_js_refusal_renders_a_page_with_navigation(self) -> None:
        response = self._post("dash:mode-switch", {"mode": "not-a-preset"}, htmx=False)
        assert response.status_code == 400
        body = response.content.decode()
        assert "<!doctype html>" in body.lower()
        assert reverse("dash:loops") in body


class TheLoopsPageEditsNoEnablementTestCase(TestCase):
    """C2: enablement is set top-down through presets and schedules, never per loop here.

    The page's job is to SHOW the effective verdict and which layer decided it. A per-loop
    verb on this page is a second surface for a question the preset already answers, and two
    surfaces that can disagree about whether a loop runs is one too many. Django-admin stays
    the break-glass, per C3.
    """

    def setUp(self) -> None:
        super().setUp()
        Loop.objects.create(name="dashloop", script="teatree.loops.review", delay_seconds=60)
        self.client = Client()

    def _page(self) -> HttpResponse:
        return self.client.get(reverse("dash:loops"), headers={"x-forwarded-for": "127.0.0.1"})

    def test_the_page_offers_no_per_loop_enable_control(self) -> None:
        body = self._page().content.decode()
        assert "loops/action/" not in body
        for verb in ("pause", "resume", "disable", "enable"):
            assert f'value="{verb}"' not in body, f"the page still posts a {verb!r} verb"

    def test_the_break_glass_is_named_rather_than_left_to_be_guessed(self) -> None:
        # Removing the verbs without naming what replaces them is how an operator in an
        # incident concludes the handle is gone rather than moved.
        assert "/admin/" in self._page().content.decode()

    def test_the_route_itself_is_gone_not_merely_unlinked(self) -> None:
        with pytest.raises(NoReverseMatch):
            reverse("dash:loop_action")

    def test_the_page_still_edits_what_it_is_allowed_to(self) -> None:
        # The control: cadence and the preset switch stay, so the removal is scoped to
        # enablement rather than making the page read-only wholesale.
        body = self._page().content.decode()
        assert reverse("dash:loop_cadence") in body
        assert reverse("dash:mode-switch") in body
