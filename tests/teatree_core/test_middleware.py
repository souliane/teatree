"""Tests for the local admin auto-login middleware."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings

from teatree.core.middleware import LocalAdminAutoLoginMiddleware


class LocalAdminAutoLoginTestCase(TestCase):
    def _run(self, path: str, *, debug: bool, user=None):
        request = RequestFactory().get(path)
        request.user = user if user is not None else AnonymousUser()
        middleware = LocalAdminAutoLoginMiddleware(lambda _req: "ok")
        with override_settings(DEBUG=debug), patch("teatree.core.middleware.login") as login_mock:
            result = middleware(request)
        assert result == "ok"
        return login_mock

    def test_logs_in_superuser_on_admin_path_in_debug(self) -> None:
        superuser = get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run("/admin/", debug=True)
        login_mock.assert_called_once()
        assert login_mock.call_args.args[1] == superuser

    def test_inert_when_debug_off(self) -> None:
        # The DEBUG gate is the only thing standing between local convenience
        # and an open admin off-local — this fails RED if the gate is removed.
        get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run("/admin/", debug=False)
        login_mock.assert_not_called()

    def test_ignores_non_admin_paths(self) -> None:
        get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run("/", debug=True)
        login_mock.assert_not_called()

    def test_skips_when_already_authenticated(self) -> None:
        superuser = get_user_model().objects.create_superuser("admin", password="x")
        login_mock = self._run("/admin/", debug=True, user=superuser)
        login_mock.assert_not_called()

    def test_noop_when_no_superuser_exists(self) -> None:
        login_mock = self._run("/admin/", debug=True)
        login_mock.assert_not_called()
