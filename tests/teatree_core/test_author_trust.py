"""Author-trust classifier + DB model golden corpus (#1773).

The shared :func:`teatree.core.review.author_trust.classify_author` is the single
seam the merge keystone and the four reviewing scanners consume. These tests
pin both directions of the trust decision (must-ALLOW / must-DENY), the DB
model tolerance, the empty-table config fallback, and the pre-migration
database-error tolerance — the real models, only the visibility probe stubbed.
"""

from contextlib import AbstractContextManager
from unittest.mock import patch

import pytest
from django.db import OperationalError, ProgrammingError
from django.test import TestCase

from teatree.core.models import TrustedIdentity
from teatree.core.review import author_trust

# Imported by NAME (not merely module-qualified) so a revert of either production
# symbol turns TestExtraTrustedUnion red — the anti-vacuity contract the per-diff
# coverage gate enforces (BLUEPRINT §17.6.3).
from teatree.core.review.author_trust import classify_author, is_trusted_author

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PUBLIC = "souliane/teatree"


def _seed_known() -> None:
    TrustedIdentity.objects.get_or_create(platform="github", handle="souliane")
    TrustedIdentity.objects.get_or_create(platform="github", handle="trusted-bot")
    TrustedIdentity.objects.get_or_create(platform="gitlab", handle="adrien.cossa")


def _public() -> AbstractContextManager[object]:
    return patch.object(author_trust, "repo_is_internal", return_value=False)


def _private() -> AbstractContextManager[object]:
    return patch.object(author_trust, "repo_is_internal", return_value=True)


class TestTrustedIdentityManager(TestCase):
    def setUp(self) -> None:
        _seed_known()

    def test_is_trusted_true_cases(self) -> None:
        for handle, platform in (
            ("souliane", ""),
            ("Souliane", ""),
            ("SOULIANE", "github"),
            ("trusted-bot", "github"),
            ("adrien.cossa", "gitlab"),
            ("adrien.cossa", ""),
            ("adrien.cossa", "github"),  # platform-tolerant: handle trusted on any forge
        ):
            with self.subTest(handle=handle, platform=platform):
                assert TrustedIdentity.objects.is_trusted(handle, platform) is True

    def test_is_trusted_false_cases(self) -> None:
        for handle in ("evilhacker", "", "   ", "souliane2"):
            with self.subTest(handle=handle):
                assert TrustedIdentity.objects.is_trusted(handle) is False

    def test_trusted_handles_union_lowercased(self) -> None:
        assert TrustedIdentity.objects.trusted_handles() == {"souliane", "trusted-bot", "adrien.cossa"}


class TestClassifyAuthorPublicRepo(TestCase):
    def setUp(self) -> None:
        _seed_known()

    def test_seeded_identities_trusted(self) -> None:
        cases = (
            (_PUBLIC, "souliane", "github"),
            (_PUBLIC, "trusted-bot", "github"),
            ("adrien.cossa/proj", "adrien.cossa", "gitlab"),
        )
        for slug, author, host_kind in cases:
            with self.subTest(author=author), _public():
                verdict = author_trust.classify_author(slug, author, host_kind=host_kind)
                assert verdict.trusted is True
                assert verdict.untrusted is False
                assert verdict.internal_repo is False

    def test_untrusted_and_empty_author_are_untrusted(self) -> None:
        for author in ("evilhacker", ""):
            with self.subTest(author=author), _public():
                verdict = author_trust.classify_author(_PUBLIC, author)
                assert verdict.untrusted is True
                assert verdict.trusted is False


class TestClassifyAuthorPrivateRepo(TestCase):
    def test_any_author_allowed_no_check(self) -> None:
        for author in ("souliane", "evilhacker", ""):
            with self.subTest(author=author), _private():
                verdict = author_trust.classify_author("souliane/private-repo", author)
                assert verdict.internal_repo is True
                assert verdict.trusted is True
                assert verdict.untrusted is False


class TestEmptyTableConfigFallback(TestCase):
    def setUp(self) -> None:
        # The empty-table fallback path only fires when the table is empty;
        # there is no data-migration seed, so the table starts empty.
        TrustedIdentity.objects.all().delete()

    def test_empty_db_falls_back_to_user_identity_aliases(self) -> None:
        assert not TrustedIdentity.objects.exists()
        with patch("teatree.config.get_effective_settings") as mock_settings:
            mock_settings.return_value.user_identity_aliases = ["souliane", "adrien.cossa"]
            assert author_trust.trusted_handles() == {"souliane", "adrien.cossa"}

    def test_config_fallback_classifies_public_author(self) -> None:
        with _public(), patch("teatree.config.get_effective_settings") as mock_settings:
            mock_settings.return_value.user_identity_aliases = ["souliane"]
            assert author_trust.classify_author(_PUBLIC, "souliane").trusted is True
            assert author_trust.classify_author(_PUBLIC, "evilhacker").untrusted is True

    def test_db_rows_take_precedence_over_config(self) -> None:
        _seed_known()
        with patch("teatree.config.get_effective_settings") as mock_settings:
            mock_settings.return_value.user_identity_aliases = ["someone-else"]
            handles = author_trust.trusted_handles()
        assert "souliane" in handles
        assert "someone-else" not in handles


class TestPreMigrationTolerance(TestCase):
    def test_db_error_falls_back_to_config(self) -> None:
        for exc in (
            OperationalError("no such table: teatree_trusted_identity"),
            ProgrammingError("relation does not exist"),
        ):
            with (
                self.subTest(exc=type(exc).__name__),
                patch.object(TrustedIdentity.objects, "trusted_handles", side_effect=exc),
                patch("teatree.config.get_effective_settings") as mock_settings,
            ):
                mock_settings.return_value.user_identity_aliases = ["souliane"]
                assert author_trust.trusted_handles() == {"souliane"}


class TestExtraTrustedUnion(TestCase):
    """``extra_trusted`` unions caller-supplied handles into the trust set (#3235).

    The issue-implementer's intake set is a UNION of three sources — the owner's
    ``user_identity_aliases``, the ``trusted_issue_authors`` allowlist, and the
    ``TrustedIdentity`` rows. The first two live in config (which cannot reach the
    DB), so the caller resolves them and hands them to this seam. Default-empty, so
    every existing consumer (the keystone, the four reviewing scanners) is byte-for-byte
    unchanged.
    """

    def test_extra_trusted_defaults_to_empty_so_existing_callers_are_unchanged(self) -> None:
        _seed_known()
        with _public():
            assert classify_author(_PUBLIC, "adrien-oper").untrusted is True

    def test_extra_trusted_handle_is_trusted(self) -> None:
        _seed_known()
        with _public():
            classification = classify_author(_PUBLIC, "adrien-oper", extra_trusted=frozenset({"adrien-oper"}))
        assert classification.trusted is True
        assert classification.untrusted is False

    def test_extra_trusted_is_a_union_not_a_replacement(self) -> None:
        """A DB-trusted handle stays trusted when the caller supplies its own extras."""
        _seed_known()
        with _public():
            assert classify_author(_PUBLIC, "souliane", extra_trusted=frozenset({"adrien-oper"})).trusted

    def test_extra_trusted_match_is_case_insensitive(self) -> None:
        assert is_trusted_author("Adrien-Oper", extra_trusted=frozenset({"adrien-oper"})) is True

    def test_extra_trusted_never_admits_an_unlisted_author(self) -> None:
        assert is_trusted_author("evilhacker", extra_trusted=frozenset({"adrien-oper"})) is False

    def test_empty_author_is_untrusted_even_with_extras(self) -> None:
        assert is_trusted_author("", extra_trusted=frozenset({"adrien-oper"})) is False
