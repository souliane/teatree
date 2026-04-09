"""Tests for teatree.core.middleware.LocalhostOnlyMiddleware."""

from django.test import TestCase, override_settings


@override_settings(
    MIDDLEWARE=[
        "teatree.core.middleware.LocalhostOnlyMiddleware",
        "django.middleware.common.CommonMiddleware",
    ]
)
class TestLocalhostOnlyMiddleware(TestCase):
    def test_allows_get_from_any_address(self) -> None:
        response = self.client.get("/", REMOTE_ADDR="203.0.113.1")
        assert response.status_code != 403

    def test_allows_post_from_localhost_ipv4(self) -> None:
        response = self.client.post("/dashboard/sync/", REMOTE_ADDR="127.0.0.1")
        assert response.status_code != 403

    def test_allows_post_from_localhost_ipv6(self) -> None:
        response = self.client.post("/dashboard/sync/", REMOTE_ADDR="::1")
        assert response.status_code != 403

    def test_blocks_post_from_remote_address(self) -> None:
        response = self.client.post("/dashboard/sync/", REMOTE_ADDR="203.0.113.1")
        assert response.status_code == 403
        assert response.json()["error"] == "Dashboard actions are restricted to localhost"

    def test_blocks_put_from_remote_address(self) -> None:
        response = self.client.put("/dashboard/sync/", REMOTE_ADDR="203.0.113.1")
        assert response.status_code == 403

    def test_blocks_delete_from_remote_address(self) -> None:
        response = self.client.delete("/dashboard/sync/", REMOTE_ADDR="203.0.113.1")
        assert response.status_code == 403

    def test_allows_head_from_remote_address(self) -> None:
        response = self.client.head("/", REMOTE_ADDR="203.0.113.1")
        assert response.status_code != 403

    def test_allows_options_from_remote_address(self) -> None:
        response = self.client.options("/", REMOTE_ADDR="203.0.113.1")
        assert response.status_code != 403
