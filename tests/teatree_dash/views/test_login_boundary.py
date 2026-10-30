from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class DashboardLoginBoundaryTestCase(TestCase):
    def test_password_login_is_reachable_and_establishes_a_session(self) -> None:
        get_user_model().objects.create_superuser("operator", email="", password="secret")
        assert self.client.get("/login/").status_code == 200
        assert self.client.get("/admin/login/").status_code == 200
        response = self.client.post("/login/?next=/dash/board/", {"username": "operator", "password": "secret"})
        assert response.status_code == 302
        assert response.headers["Location"] == "/dash/board/"
        assert self.client.get(reverse("dash:board")).status_code == 200

    def test_loopback_anonymous_request_requires_login(self) -> None:
        get_user_model().objects.create_superuser("operator", email="", password="secret")
        response = self.client.get(reverse("dash:board"), REMOTE_ADDR="127.0.0.1")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/?next=")

    def test_forwarded_loopback_anonymous_request_requires_login(self) -> None:
        response = self.client.get(
            reverse("dash:board"), REMOTE_ADDR="127.0.0.1", headers={"x-forwarded-for": "127.0.0.1"}
        )
        assert response.status_code == 302

    def test_expired_htmx_session_gets_redirect_header(self) -> None:
        response = self.client.get(reverse("dash:board"), headers={"hx-request": "true"})
        assert response.status_code == 401
        assert response.headers["HX-Redirect"].startswith("/login/?next=")
