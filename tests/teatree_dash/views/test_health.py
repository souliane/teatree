"""The health page renders the four bands and the fail-open red banner (#3162)."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from teatree.core.models.config_setting import ConfigSetting
from teatree.dash.health_bands import build_health_view

_NON_LOOPBACK = "203.0.113.7"


class HealthPageTestCase(TestCase):
    def test_health_page_renders_bands(self) -> None:
        resp = self.client.get(reverse("dash:health"))
        assert resp.status_code == 200
        body = resp.content.decode()
        # The verdict is the page lead (status word + open-issue count), not a
        # "Verdict" heading; the other three bands keep their headings.
        assert 'class="verdict ' in body
        assert "open issue" in body
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
        body = resp.content.decode()
        assert 'class="verdict ' in body
        assert "open issue" in body

    def test_command_buttons_present_on_page(self) -> None:
        resp = self.client.get(reverse("dash:health"))
        body = resp.content.decode()
        assert "t3 doctor check" in body


class HealthAccessGateTestCase(TestCase):
    """DASH-2: the health page + its bands poll partial carry the same loopback gate.

    An off-loopback anonymous GET is refused with 403, exactly like every other dash view.
    """

    def test_off_loopback_anonymous_health_page_is_refused(self) -> None:
        resp = self.client.get(reverse("dash:health"), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 403

    def test_off_loopback_anonymous_bands_poll_is_refused(self) -> None:
        resp = self.client.get(reverse("dash:health_bands"), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 403

    def test_loopback_health_page_still_serves(self) -> None:
        assert self.client.get(reverse("dash:health")).status_code == 200

    def test_off_loopback_staff_user_passes_the_gate(self) -> None:
        staff = get_user_model().objects.create_user("healthstaff", password="x", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse("dash:health"), REMOTE_ADDR=_NON_LOOPBACK)
        assert resp.status_code == 200


class HealthViewModelTestCase(TestCase):
    def test_gate_fail_open_reflects_config_setting(self) -> None:
        assert build_health_view().mode.gate_fail_open is False
        ConfigSetting.objects.set_value("danger_gate_fail_open", value=True)
        assert build_health_view().mode.gate_fail_open is True
