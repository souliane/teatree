"""The visual QA seed must make every declared route renderable."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from e2e.dash.seed import seed_board


class TestSeed(TestCase):
    def test_seed_creates_an_admin_for_loopback_auto_login(self) -> None:
        seed_board()

        user = get_user_model().objects.get(is_superuser=True)
        assert not user.has_usable_password()
