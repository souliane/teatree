"""Every route of the production URLconf requires a staff session, except the explicit public set."""

import re
from collections.abc import Iterable, Iterator

from django.contrib.admindocs.views import simplify_regex
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

_PUBLIC_PATHS = frozenset({"/login/", "/admin/login/", "/favicon.ico", "/hooks/gitlab/", "/hooks/github/"})
_PUBLIC_PREFIXES = ("/static/", "/login/local/", "/login/oauth/")
_REQUEST_SHAPES: dict[str, dict[str, object]] = {
    "plain": {},
    "forwarded": {"headers": {"x-forwarded-for": "127.0.0.1"}},
    "htmx": {"headers": {"hx-request": "true"}},
}


def _paths(patterns: Iterable[URLPattern | URLResolver], prefix: str = "") -> Iterator[str]:
    for entry in patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _paths(entry.url_patterns, route)
        else:
            yield re.sub(r"<[^>]+>", "1", simplify_regex(route))


def _production_paths() -> list[str]:
    return list(dict.fromkeys(_paths(get_resolver().url_patterns)))


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _sent_to_login(response: HttpResponse) -> bool:
    destination = response.headers.get("Location") or response.headers.get("HX-Redirect", "")
    return destination.startswith("/login/?next=")


class EveryRouteRequiresAStaffSessionTestCase(TestCase):
    def test_the_sweep_reaches_the_dashboard_the_admin_and_the_webhooks(self) -> None:
        paths = _production_paths()
        assert {"/dash/board/", "/admin/", "/hooks/github/", "/login/"} <= set(paths)
        assert len(paths) > 100

    def test_an_anonymous_caller_is_sent_to_login_on_every_route_outside_the_public_set(self) -> None:
        for path in _production_paths():
            for shape, extra in _REQUEST_SHAPES.items():
                with self.subTest(path=path, shape=shape):
                    response = self.client.get(path, REMOTE_ADDR="127.0.0.1", **extra)
                    assert _sent_to_login(response) is not _is_public(path)

    def test_a_signed_in_caller_without_staff_is_sent_to_login_on_every_route_outside_the_public_set(self) -> None:
        self.client.force_login(get_user_model().objects.create_user("viewer", password="x"))
        for path in _production_paths():
            with self.subTest(path=path):
                assert _sent_to_login(self.client.get(path)) is not _is_public(path)

    def test_a_staff_session_reaches_the_dashboard_and_the_admin(self) -> None:
        self.client.force_login(get_user_model().objects.create_superuser("operator", email="", password="x"))
        for path in ("/dash/board/", "/admin/"):
            with self.subTest(path=path):
                assert self.client.get(path).status_code == 200
