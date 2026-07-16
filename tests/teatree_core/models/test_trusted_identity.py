"""Tests for the ``TrustedIdentity`` manager (mirrors ``models.trusted_identity``)."""

import pytest
from django.test import TestCase

from teatree.core.models import TrustedIdentity

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestOrderedHandles(TestCase):
    def setUp(self) -> None:
        TrustedIdentity.objects.all().delete()

    def test_distinct_handles_in_platform_order(self) -> None:
        TrustedIdentity.objects.create(platform="gitlab", handle="adrien.cossa")
        TrustedIdentity.objects.create(platform="github", handle="souliane")
        TrustedIdentity.objects.create(platform="github", handle="souliane-alt")

        assert TrustedIdentity.objects.ordered_handles() == ["souliane", "souliane-alt", "adrien.cossa"]

    def test_dedupes_same_handle_across_platforms_case_insensitively(self) -> None:
        TrustedIdentity.objects.create(platform="github", handle="Souliane")
        TrustedIdentity.objects.create(platform="gitlab", handle="souliane")

        assert TrustedIdentity.objects.ordered_handles() == ["Souliane"]

    def test_empty_when_no_rows(self) -> None:
        assert TrustedIdentity.objects.ordered_handles() == []
