"""Defense-in-depth: dashboard control views refuse off-loopback anonymous callers (#3164).

HARDENING #4: security rested only on the gunicorn loopback bind + the
loopback-only auto-login middleware. A ``--host 0.0.0.0`` misconfig would expose
loop-control mutations, the gate toggle, FSM transitions, and command output to
an anonymous off-loopback caller. The view-level ``require_loopback_or_staff``
gate closes that gap.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

_NON_LOOPBACK = "203.0.113.7"


class DashboardAccessGateTestCase(TestCase):
    def test_off_loopback_anonymous_mutation_is_refused(self) -> None:
        response = self.client.post(
            reverse("dash:loop_action"),
            {"name": "review", "action": "pause"},
            REMOTE_ADDR=_NON_LOOPBACK,
        )
        assert response.status_code == 403

    def test_off_loopback_anonymous_gate_toggle_is_refused(self) -> None:
        response = self.client.post(
            reverse("dash:gate_toggle"),
            {"enable": "0"},
            REMOTE_ADDR=_NON_LOOPBACK,
        )
        assert response.status_code == 403

    def test_loopback_anonymous_request_passes_the_gate(self) -> None:
        # 127.0.0.1 is the default test-client REMOTE_ADDR — the loopback bind the
        # deploy relies on. The gate lets it through (an unknown loop then 400s,
        # proving we reached the view, not the 403 gate).
        response = self.client.post(reverse("dash:loop_action"), {"name": "nope", "action": "pause"})
        assert response.status_code == 400

    def test_off_loopback_staff_user_passes_the_gate(self) -> None:
        staff = get_user_model().objects.create_user("dashstaff", password="x", is_staff=True)
        self.client.force_login(staff)
        # gate-toggle disable needs no confirm; a 302 redirect proves the gate passed.
        response = self.client.post(
            reverse("dash:gate_toggle"),
            {"enable": "0"},
            REMOTE_ADDR=_NON_LOOPBACK,
        )
        assert response.status_code == 302
