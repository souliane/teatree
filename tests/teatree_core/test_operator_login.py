from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import TestCase
from django.utils import timezone

from teatree.core.operator_login import create_local_login_link


class LocalOperatorLoginTestCase(TestCase):
    def setUp(self) -> None:
        self.operator = get_user_model().objects.create_superuser("operator", email="", password="secret")

    def test_link_logs_in_once_and_returns_to_the_requested_page(self) -> None:
        path = create_local_login_link(self.operator, "/dash/board/")
        assert self.client.get("/admin/").status_code == 302

        first = self.client.get(path)
        assert first.status_code == 302
        assert first.headers["Location"] == "/dash/board/"
        assert self.client.get("/dash/board/").status_code == 200
        assert self.client.get(path).status_code == 403

    def test_copying_the_link_session_cookie_does_not_authenticate(self) -> None:
        path = create_local_login_link(self.operator, "/admin/")
        key = path.split("/")[3]
        self.client.cookies["sessionid"] = key
        assert self.client.get("/admin/").status_code == 302

    def test_expired_link_fails(self) -> None:
        path = create_local_login_link(self.operator, "/admin/")
        key = path.split("/")[3]
        Session.objects.filter(session_key=key).update(expire_date=timezone.now() - timedelta(seconds=1))
        assert self.client.get(path).status_code == 403

    def test_link_for_non_staff_user_fails(self) -> None:
        user = get_user_model().objects.create_user("reader", password="secret")
        path = create_local_login_link(user, "/admin/")
        assert self.client.get(path).status_code == 403

    def test_external_next_is_replaced_with_admin(self) -> None:
        path = create_local_login_link(self.operator, "https://evil.example/")
        response = self.client.get(path)
        assert response.headers["Location"] == "/admin/"

    def test_malformed_next_is_replaced_with_admin(self) -> None:
        path = create_local_login_link(self.operator, "/admin/")
        row = Session.objects.get(session_key=path.split("/")[3])
        data = row.get_decoded()
        data["local_link_next"] = 42
        row.session_data = SessionStore().encode(data)
        row.save(update_fields=["session_data"])
        assert self.client.get(path).headers["Location"] == "/admin/"
