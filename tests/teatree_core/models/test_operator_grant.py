from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import OnBehalfApproval, OperatorIdentity
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

    def test_token_for_approval_requires_the_grant_owner_and_exact_consent(self) -> None:
        OperatorIdentity.objects.create(user=self.alice, platform="gitlab", handle="alice", external_id="1")
        OperatorIdentity.objects.create(user=self.bob, platform="gitlab", handle="bob", external_id="2")
        grant = OperatorGrant.objects.store(
            user=self.alice,
            forge="gitlab",
            tokens=OperatorGrantTokens("access-alice", "refresh-alice", self.expires_at, scope="api"),
            key=self.key,
        )
        OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")

        assert grant.token_for("approve", "org/repo!7", "gitlab:alice", key=self.key) == "access-alice"
        with pytest.raises(PermissionError):
            grant.token_for("approve", "org/repo!8", "gitlab:alice", key=self.key)
        with pytest.raises(PermissionError):
            grant.token_for("approve", "org/repo!7", "gitlab:bob", key=self.key)

        OnBehalfApproval.record("org/repo!7", "approve", "gitlab:bob")
        with pytest.raises(PermissionError, match="does not belong"):
            grant.token_for("approve", "org/repo!7", "gitlab:bob", key=self.key)

        grant.scope = "read_user"
        with pytest.raises(PermissionError, match="approval scope"):
            grant.token_for("approve", "org/repo!7", "gitlab:alice", key=self.key)

        grant.scope = "api"
        grant.expires_at = timezone.now() - timedelta(seconds=1)
        with pytest.raises(PermissionError, match="cannot authorize"):
            grant.token_for("approve", "org/repo!7", "gitlab:alice", key=self.key)

    def test_token_for_approval_requires_the_grant_owners_forge_identity(self) -> None:
        OperatorIdentity.objects.create(user=self.alice, platform="slack", handle="U-ALICE")
        grant = OperatorGrant.objects.store(
            user=self.alice,
            forge="gitlab",
            tokens=OperatorGrantTokens("access-alice", "refresh-alice", self.expires_at, scope="api"),
            key=self.key,
        )
        OnBehalfApproval.record("org/repo!7", "approve", "slack:U-ALICE")

        with pytest.raises(PermissionError, match="does not belong"):
            grant.token_for("approve", "org/repo!7", "slack:U-ALICE", key=self.key)

    def _gitlab_grant_with_consent(self, action: str = "approve") -> OperatorGrant:
        OperatorIdentity.objects.create(user=self.alice, platform="gitlab", handle="alice", external_id="1")
        OnBehalfApproval.record("org/repo!7", action, "gitlab:alice")
        return OperatorGrant.objects.store(
            user=self.alice,
            forge="gitlab",
            tokens=OperatorGrantTokens("access-alice", "refresh-alice", self.expires_at, scope="api"),
            key=self.key,
        )

    def test_token_for_approval_refuses_a_demoted_operator(self) -> None:
        grant = self._gitlab_grant_with_consent()
        self.alice.is_superuser = False
        self.alice.save(update_fields=["is_superuser"])

        with pytest.raises(PermissionError, match="does not belong"):
            grant.token_for("approve", "org/repo!7", "gitlab:alice", key=self.key)

    def test_token_for_approval_spends_its_consent_once(self) -> None:
        grant = self._gitlab_grant_with_consent()
        OnBehalfApproval.objects.update(consumed_at=timezone.now())

        with pytest.raises(PermissionError, match="no consent"):
            grant.token_for("approve", "org/repo!7", "gitlab:alice", key=self.key)

    def test_token_for_serves_only_approve_and_unapprove(self) -> None:
        grant = self._gitlab_grant_with_consent("post_comment")

        with pytest.raises(PermissionError, match="cannot authorize"):
            grant.token_for("post_comment", "org/repo!7", "gitlab:alice", key=self.key)
