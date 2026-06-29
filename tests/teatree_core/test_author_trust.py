"""Author-trust classifier + DB model golden corpus (#1773).

The shared :func:`teatree.core.author_trust.classify_author` is the single
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

from teatree.core import author_trust
from teatree.core.models import TrustedIdentity

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
