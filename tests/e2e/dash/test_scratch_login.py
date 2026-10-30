"""The throwaway visual-QA server signs its browser in as the seeded operator; production never does."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from e2e.dash import settings as scratch_settings
from e2e.dash.seed import seed_board


class ScratchLoginTestCase(TestCase):
    @override_settings(MIDDLEWARE=scratch_settings.MIDDLEWARE)
    def test_the_scratch_server_renders_the_dashboard_for_the_seeded_operator(self) -> None:
        seed_board()

        response = self.client.get("/dash/board/")

        assert response.status_code == 200
        assert get_user_model().objects.filter(is_superuser=True).count() == 1

    @override_settings(MIDDLEWARE=scratch_settings.MIDDLEWARE)
    def test_the_scratch_server_creates_the_operator_when_nothing_seeded_it(self) -> None:
        assert self.client.get("/dash/board/").status_code == 200
        assert get_user_model().objects.get(username="visual_qa").is_superuser

    def test_the_production_middleware_still_sends_the_same_request_to_login(self) -> None:
        seed_board()

        response = self.client.get("/dash/board/")

        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login/?next=")
