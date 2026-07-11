"""Tests for the loopback admin auto-login middleware."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from teatree.core.middleware import LocalAdminAutoLoginMiddleware
from teatree.core.models import ConfigSetting

_LOOPBACK = "127.0.0.1"
_NON_LOOPBACK = "10.0.0.7"


class LocalAdminAutoLoginTestCase(TestCase):
    def _run(self, path: str = "/admin/", *, remote_addr: str = _LOOPBACK, user=None, **extra: str):
        request = RequestFactory().get(path, REMOTE_ADDR=remote_addr, **extra)
        request.user = user if user is not None else AnonymousUser()
        middleware = LocalAdminAutoLoginMiddleware(lambda _req: "ok")
        with patch("teatree.core.middleware.login") as login_mock:
            result = middleware(request)
        assert result == "ok"
        return login_mock

    def test_logs_in_superuser_on_loopback_admin_when_flag_on(self) -> None:
        superuser = get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run()
        login_mock.assert_called_once()
        assert login_mock.call_args.args[1] == superuser

    def test_logs_in_for_ipv6_loopback(self) -> None:
        get_user_model().objects.create_superuser("admin", password="x")
        self._run(remote_addr="::1").assert_called_once()

    def test_logs_in_superuser_on_loopback_dash_prefix(self) -> None:
        # #3162: the dashboard at /dash/ rides the same loopback auto-login as
        # /admin/. Fails RED if the prefix gate narrows back to admin-only.
        superuser = get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run("/dash/board")
        login_mock.assert_called_once()
        assert login_mock.call_args.args[1] == superuser

    def test_dash_not_logged_in_for_non_loopback(self) -> None:
        # SECURITY: the loopback boundary covers /dash/ exactly as it covers
        # /admin/ — a non-loopback dashboard request is never auto-logged-in.
        get_user_model().objects.create_superuser("admin", password="x")
        self._run("/dash/board", remote_addr=_NON_LOOPBACK).assert_not_called()

    def test_not_logged_in_for_non_loopback_even_with_flag_on(self) -> None:
        # SECURITY: the flag is on (default), but a non-loopback client must
        # NEVER be auto-logged-in — the loopback check is the hard boundary that
        # keeps an off-loopback admin port from silently opening the dashboard.
        get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run(remote_addr=_NON_LOOPBACK)
        login_mock.assert_not_called()

    def test_forwarded_header_cannot_spoof_loopback(self) -> None:
        # SECURITY: the check reads REMOTE_ADDR, never a forwarded header. A
        # non-loopback client sending `X-Forwarded-For: 127.0.0.1` must NOT be
        # auto-logged-in — locks the property against a future regression that
        # starts trusting forwarded headers.
        get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run(remote_addr=_NON_LOOPBACK, HTTP_X_FORWARDED_FOR=_LOOPBACK)
        login_mock.assert_not_called()

    def test_not_logged_in_when_flag_off(self) -> None:
        # The flag is the deliberate off-switch for the loopback convenience —
        # this fails RED if the flag gate is dropped and always-on auto-login
        # returns.
        get_user_model().objects.create_superuser("admin", password="x")
        ConfigSetting.objects.set_value("admin_autologin_enabled", value=False)
        login_mock = self._run()
        login_mock.assert_not_called()

    def test_ignores_non_admin_paths(self) -> None:
        get_user_model().objects.create_superuser("admin", password="x")
        self._run("/").assert_not_called()

    def test_skips_when_already_authenticated(self) -> None:
        superuser = get_user_model().objects.create_superuser("admin", password="x")
        self._run(user=superuser).assert_not_called()

    def test_noop_when_no_superuser_exists(self) -> None:
        self._run().assert_not_called()
