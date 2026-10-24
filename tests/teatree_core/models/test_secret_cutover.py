import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models.secret_cutover import SecretCutover


class TestSecretCutover(TestCase):
    def test_marker_is_singleton_and_has_timestamp(self) -> None:
        marker = SecretCutover.objects.create()
        assert marker.pk == 1
        assert marker.applied_at is not None
        assert str(marker) == "secret-cutover<1>"
        with transaction.atomic(), pytest.raises(IntegrityError):
            SecretCutover.objects.create(id=2)
