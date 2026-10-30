import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase


class StaffDashboardTestCase(TestCase):
    @classmethod
    def _pre_setup(cls) -> None:
        super()._pre_setup()
        user = get_user_model().objects.create_user(f"dash-{uuid.uuid4().hex}", is_staff=True)
        cls.client.force_login(user)

    def csrf_client(self) -> Client:
        user = get_user_model().objects.get(pk=self.client.session["_auth_user_id"])
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        return client
