"""Every dashboard route requires a staff session, including loopback requests."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, reverse

from teatree.dash import urls as dash_urls

_NON_LOOPBACK = "203.0.113.7"
#: A placeholder per path converter, so every route can be reversed without fixtures.
_ARG_FOR_CONVERTER = {"IntConverter": 1, "StringConverter": "x", "PathConverter": "x", "SlugConverter": "x"}


def _gated_routes() -> list[str]:
    """Every dashboard URL, including redirects."""
    routes = []
    for pattern in dash_urls.urlpatterns:
        assert isinstance(pattern, URLPattern)
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
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/?next=")

    def test_off_loopback_anonymous_gate_toggle_is_refused(self) -> None:
        response = self.client.post(
            reverse("dash:gate_toggle"),
            {"enable": "0"},
            REMOTE_ADDR=_NON_LOOPBACK,
        )
        assert response.status_code == 302

    def test_loopback_anonymous_request_requires_login(self) -> None:
        response = self.client.post(reverse("dash:mode-switch"), {"mode": "not-a-preset"})
        assert response.status_code == 302

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
    """The middleware must cover every URL, including bare redirects."""

    def test_an_off_loopback_anonymous_caller_is_refused_on_every_route(self) -> None:
        routes = _gated_routes()
        assert len(routes) > 30
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route, REMOTE_ADDR=_NON_LOOPBACK)
                assert response.status_code == 302
                assert response.headers["Location"].startswith("/login/?next=")
