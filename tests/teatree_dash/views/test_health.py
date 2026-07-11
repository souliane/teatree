"""The health page renders the four bands and the fail-open red banner (#3162)."""

from django.test import TestCase
from django.urls import reverse

from teatree.core.models.config_setting import ConfigSetting
from teatree.dash.health_bands import build_health_view


class HealthPageTestCase(TestCase):
    def test_health_page_renders_bands(self) -> None:
        resp = self.client.get(reverse("dash:health"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Verdict" in body
        assert "Loops" in body
        assert "Capacity" in body
        assert "Mode" in body

    def test_fail_open_banner_absent_by_default(self) -> None:
        resp = self.client.get(reverse("dash:health"))
        assert "danger_gate_fail_open is ON" not in resp.content.decode()

    def test_fail_open_banner_shown_when_switch_on(self) -> None:
        ConfigSetting.objects.set_value("danger_gate_fail_open", value=True)
        resp = self.client.get(reverse("dash:health"))
        assert "danger_gate_fail_open is ON" in resp.content.decode()

    def test_bands_partial_renders(self) -> None:
        resp = self.client.get(reverse("dash:health_bands"))
        assert resp.status_code == 200
        assert "Verdict" in resp.content.decode()

    def test_command_buttons_present_on_page(self) -> None:
        resp = self.client.get(reverse("dash:health"))
        body = resp.content.decode()
        assert "t3 doctor check" in body


class HealthViewModelTestCase(TestCase):
    def test_gate_fail_open_reflects_config_setting(self) -> None:
        assert build_health_view().mode.gate_fail_open is False
        ConfigSetting.objects.set_value("danger_gate_fail_open", value=True)
        assert build_health_view().mode.gate_fail_open is True
