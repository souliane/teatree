"""core.provenance (#116): the trust classifier the ingestion chokepoint stamps.

``OWNER`` only for an operator identity in the ``TrustedIdentity`` set; every other
actor — colleague, stranger, blank — resolves to the fail-closed ``PUBLIC``.
"""

from django.test import TestCase

from teatree.core.models import TrustedIdentity
from teatree.core.models.provenance import Provenance, classify_provenance


class TestProvenanceVocabulary:
    def test_only_owner_is_trusted(self) -> None:
        # The floor's UNTRUSTED set is "everything except owner" — pin the members.
        untrusted = {Provenance.PUBLIC, Provenance.WEB, Provenance.TRUSTED_COLLEAGUE}
        assert Provenance.OWNER not in untrusted
        assert set(Provenance) == untrusted | {Provenance.OWNER}


class TestClassifyProvenance(TestCase):
    def test_a_trusted_identity_actor_is_owner(self) -> None:
        TrustedIdentity.objects.create(platform=TrustedIdentity.Platform.SLACK, handle="operator")
        assert classify_provenance("slack", "operator") is Provenance.OWNER

    def test_an_unknown_actor_is_public_fail_closed(self) -> None:
        assert classify_provenance("slack", "some-stranger") is Provenance.PUBLIC

    def test_a_blank_actor_is_public(self) -> None:
        assert classify_provenance("github", "") is Provenance.PUBLIC

    def test_owner_match_is_case_insensitive_and_platform_tolerant(self) -> None:
        TrustedIdentity.objects.create(platform=TrustedIdentity.Platform.GITHUB, handle="Owner")
        # cross-platform tolerant match (the TrustedIdentity contract) still resolves owner
        assert classify_provenance("slack", "owner") is Provenance.OWNER
