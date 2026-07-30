"""The global loop kill-switch is a confirmed, audited toggle — never a one-click flip (#3623)."""

import logging

from django.test import TestCase
from django.urls import reverse

from teatree.core.models import ConfigSetting
from teatree.dash.loop_control import RUNNER_CONFIRM_PHRASE

_KEY = "loop_runner_enabled"


class TestRunnerToggle(TestCase):
    def test_disabling_the_fleet_requires_the_typed_confirm_phrase(self) -> None:
        ConfigSetting.objects.set_value(_KEY, value=True)

        response = self.client.post(
            reverse("dash:runner_toggle"), {"enable": "0", "confirm": "yes"}, REMOTE_ADDR="127.0.0.1"
        )

        assert response.status_code == 400
        assert ConfigSetting.objects.get_effective(_KEY) is True

    def test_the_typed_confirm_phrase_stops_the_fleet(self) -> None:
        ConfigSetting.objects.set_value(_KEY, value=True)

        self.client.post(
            reverse("dash:runner_toggle"),
            {"enable": "0", "confirm": RUNNER_CONFIRM_PHRASE},
            REMOTE_ADDR="127.0.0.1",
        )

        assert ConfigSetting.objects.get_effective(_KEY) is False

    def test_re_enabling_needs_no_phrase(self) -> None:
        ConfigSetting.objects.set_value(_KEY, value=False)

        self.client.post(reverse("dash:runner_toggle"), {"enable": "1"}, REMOTE_ADDR="127.0.0.1")

        assert ConfigSetting.objects.get_effective(_KEY) is True

    def test_both_directions_are_audited(self) -> None:
        ConfigSetting.objects.set_value(_KEY, value=True)

        with self.assertLogs("teatree.dash.audit", level=logging.INFO) as captured:
            self.client.post(
                reverse("dash:runner_toggle"),
                {"enable": "0", "confirm": RUNNER_CONFIRM_PHRASE},
                REMOTE_ADDR="127.0.0.1",
            )
            self.client.post(reverse("dash:runner_toggle"), {"enable": "1"}, REMOTE_ADDR="127.0.0.1")

        audited = [r.getMessage() for r in captured.records if "kill-switch:loop_runner_enabled" in r.getMessage()]
        assert len(audited) == 2

    def test_a_get_is_refused(self) -> None:
        assert self.client.get(reverse("dash:runner_toggle"), REMOTE_ADDR="127.0.0.1").status_code == 405

    def test_the_loops_page_offers_the_toggle_instead_of_a_read_only_note(self) -> None:
        body = self.client.get(reverse("dash:loops"), REMOTE_ADDR="127.0.0.1").content.decode()

        assert reverse("dash:runner_toggle") in body
        assert "read-only" not in body
