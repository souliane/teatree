"""Tests for dashboard authentication and request read caching."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from teatree import request_cache
from teatree.core.middleware import OperatorLoginRequiredMiddleware, RequestScopedReadCacheMiddleware
from teatree.core.models import ConfigSetting

_LOOPBACK = "127.0.0.1"
_NON_LOOPBACK = "10.0.0.7"


class OperatorLoginRequiredTestCase(TestCase):
    def _run(self, path: str, *, user=None, remote_addr: str = _LOOPBACK):
        request = RequestFactory().get(path, REMOTE_ADDR=remote_addr)
        request.user = user if user is not None else AnonymousUser()
        return OperatorLoginRequiredMiddleware(lambda _request: HttpResponse("ok"))(request)

    def test_superuser_in_database_does_not_authenticate_loopback_request(self) -> None:
        get_user_model().objects.create_superuser("admin", email="", password="x")
        response = self._run("/dash/board/")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/?next=")

    def test_anonymous_remote_and_forwarded_request_is_denied(self) -> None:
        response = self._run("/admin/", remote_addr=_NON_LOOPBACK)
        assert response.status_code == 302

    def test_staff_session_is_allowed(self) -> None:
        staff = get_user_model().objects.create_user("staff", is_staff=True)
        assert self._run("/dash/board/", user=staff, remote_addr=_NON_LOOPBACK).status_code == 200

    def test_non_staff_session_is_denied(self) -> None:
        user = get_user_model().objects.create_user("viewer")
        assert self._run("/dash/board/", user=user).status_code == 302

    def test_public_login_webhooks_and_static_remain_reachable(self) -> None:
        for path in ("/login/", "/admin/login/", "/hooks/gitlab/", "/hooks/github/", "/favicon.ico", "/static/x.css"):
            with self.subTest(path=path):
                assert self._run(path).status_code == 200


@override_settings(ALLOWED_HOSTS=["testserver", "127.0.0.1"])
class PortCookieIsolationTestCase(TestCase):
    def test_out_of_range_host_port_is_a_bad_request(self) -> None:
        response = self.client.get("/login/", headers={"host": "127.0.0.1:99999"})
        assert response.status_code == 400

    def test_sign_in_on_one_forward_does_not_authenticate_another(self) -> None:
        get_user_model().objects.create_superuser("admin", email="", password="secret")
        first = self.client.post(
            "/login/", {"username": "admin", "password": "secret"}, headers={"host": "127.0.0.1:8000"}
        )

        assert first.status_code == 302
        assert len([name for name in first.cookies if name.startswith("sessionid_")]) == 1
        assert self.client.get("/admin/", headers={"host": "127.0.0.1:8000"}).status_code == 200
        assert self.client.get("/admin/", headers={"host": "127.0.0.1:9401"}).status_code == 302


class RequestScopedReadCacheTestCase(TestCase):
    """The memo lives for one request and never outlives a write inside it."""

    def setUp(self) -> None:
        self.calls: list[int] = []

        @request_cache.cached_per_request
        def read() -> int:
            self.calls.append(1)
            return len(self.calls)

        self.read = read

    def _run(self, view) -> object:
        request = RequestFactory().get("/dash/board/", REMOTE_ADDR=_LOOPBACK)
        return RequestScopedReadCacheMiddleware(view)(request)

    def test_repeated_reads_within_one_request_run_once(self) -> None:
        self._run(lambda _req: [self.read(), self.read(), self.read()])
        assert len(self.calls) == 1

    def test_a_write_during_the_request_invalidates_the_memo(self) -> None:
        def mutate_then_read(_req: object) -> int:
            self.read()
            ConfigSetting.objects.set_value("mode", "auto")
            return self.read()

        assert self._run(mutate_then_read) == 2

    def test_the_memo_does_not_outlive_the_request(self) -> None:
        self._run(lambda _req: self.read())
        self._run(lambda _req: self.read())
        assert len(self.calls) == 2

    def test_a_raising_view_still_closes_the_scope(self) -> None:
        with pytest.raises(RuntimeError):
            self._run(lambda _req: (_ for _ in ()).throw(RuntimeError("boom")))
        self.read()
        assert len(self.calls) == 1
