"""Defense-in-depth: dashboard control views refuse off-loopback anonymous callers (#3164).

HARDENING #4: security rested only on the gunicorn loopback bind + the
loopback-only auto-login middleware. A ``--host 0.0.0.0`` misconfig would expose
loop-control mutations, the gate toggle, FSM transitions, and command output to
an anonymous off-loopback caller. The view-level ``require_loopback_or_staff``
gate closes that gap.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, reverse
from django.views.generic.base import RedirectView

from teatree.dash import urls as dash_urls

_NON_LOOPBACK = "203.0.113.7"
#: A placeholder per path converter, so every route can be reversed without fixtures.
_ARG_FOR_CONVERTER = {"IntConverter": 1, "StringConverter": "x", "PathConverter": "x", "SlugConverter": "x"}


def _gated_routes() -> list[str]:
    """Every dashboard URL that serves a view — a bare redirect hands out no data, so it is exempt."""
    routes = []
    for pattern in dash_urls.urlpatterns:
        assert isinstance(pattern, URLPattern)
        if getattr(pattern.callback, "view_class", None) is RedirectView:
            continue
        converters = pattern.pattern.converters
        kwargs = {name: _ARG_FOR_CONVERTER[type(converter).__name__] for name, converter in converters.items()}
        routes.append(reverse(f"dash:{pattern.name}", kwargs=kwargs))
    return routes


class DashboardAccessGateTestCase(TestCase):
    def test_off_loopback_anonymous_mutation_is_refused(self) -> None:
        response = self.client.post(
            reverse("dash:mode-switch"),
            {"mode": "afk"},
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
        # deploy relies on. The gate lets it through (an unknown preset then 400s,
        # proving we reached the view, not the 403 gate).
        response = self.client.post(reverse("dash:mode-switch"), {"mode": "not-a-preset"})
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


class EveryDashboardRouteIsGatedTestCase(TestCase):
    """The gate is per-view, so a view that forgets it must redden here.

    Asked with GET, so the gate must sit above the method decorator, as on every view today.
    """

    def test_an_off_loopback_anonymous_caller_is_refused_on_every_route(self) -> None:
        routes = _gated_routes()
        assert len(routes) > 30
        for route in routes:
            with self.subTest(route=route):
                assert self.client.get(route, REMOTE_ADDR=_NON_LOOPBACK).status_code == 403
