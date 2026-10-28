from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.operator_grant import OperatorGrant, OperatorGrantTokens
from teatree.utils.sealing import MasterKey, SealedValueTamperedError


class OperatorGrantTestCase(TestCase):
    def setUp(self) -> None:
        self.key = MasterKey(b"k" * 32)
        self.alice = get_user_model().objects.create_superuser("alice", email="", password="unused")
        self.bob = get_user_model().objects.create_superuser("bob", email="", password="unused")
        self.expires_at = timezone.now() + timedelta(hours=1)

    def test_tokens_are_sealed_to_the_user_and_forge(self) -> None:
        grant = OperatorGrant.objects.store(
            user=self.alice,
            forge="gitlab",
            tokens=OperatorGrantTokens("access-alice", "refresh-alice", self.expires_at),
            key=self.key,
        )

        assert b"access-alice" not in grant.ciphertext
        assert b"refresh-alice" not in grant.ciphertext
        assert grant.reveal(self.key).access_token == "access-alice"
        assert grant.reveal(self.key).refresh_token == "refresh-alice"
        copied = OperatorGrant(
            user=self.bob,
            forge="gitlab",
            key_id=grant.key_id,
            nonce=grant.nonce,
            ciphertext=grant.ciphertext,
            expires_at=grant.expires_at,
        )
        with pytest.raises(SealedValueTamperedError):
            copied.reveal(self.key)

    def test_rotating_one_users_grant_does_not_touch_another(self) -> None:
        for user, token in ((self.alice, "old"), (self.bob, "bob")):
            OperatorGrant.objects.store(
                user=user,
                forge="github",
                tokens=OperatorGrantTokens(f"access-{token}", f"refresh-{token}", self.expires_at),
                key=self.key,
            )
        OperatorGrant.objects.store(
            user=self.alice,
            forge="github",
            tokens=OperatorGrantTokens("access-new", "refresh-new", self.expires_at),
            key=self.key,
        )

        assert OperatorGrant.objects.count() == 2
        assert OperatorGrant.objects.get(user=self.alice).reveal(self.key).refresh_token == "refresh-new"
        assert OperatorGrant.objects.get(user=self.bob).reveal(self.key).refresh_token == "refresh-bob"

    def test_unsupported_forge_is_refused(self) -> None:
        with pytest.raises(ValueError, match="supported forge"):
            OperatorGrant.objects.store(
                user=self.alice,
                forge="slack",
                tokens=OperatorGrantTokens("access", "refresh", self.expires_at),
                key=self.key,
            )
