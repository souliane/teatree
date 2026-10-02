"""Same-repo-vs-fork merge-provenance trust — the strict fork-holds model.

The owner decision (BLUEPRINT §17.4.3): a PR whose head branch lives in a FORK
/ cross-repo ALWAYS requires human approval, even authored by the operator; a
same-repo head branch is trusted; unknown provenance fails closed to the
identity+visibility author check. These tests pin the hardest case — a fork PR
authored by a TRUSTED identity is still refused (today's author gate would let
it through), plus the fail-closed unknown-provenance fallback.
"""

from contextlib import AbstractContextManager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import TrustedIdentity
from teatree.core.review import author_trust

# Imported by NAME so a revert of the production symbol turns these RED — the
# anti-vacuity contract the per-diff coverage gate enforces (BLUEPRINT §17.6.3).
from teatree.core.review.author_trust import classify_pr_provenance

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PUBLIC = "souliane/teatree"


def _seed_trusted() -> None:
    TrustedIdentity.objects.get_or_create(platform="github", handle="souliane")


def _public() -> AbstractContextManager[object]:
    return patch.object(author_trust, "repo_is_internal", return_value=False)


class TestClassifyPrProvenanceStrictFork(TestCase):
    def setUp(self) -> None:
        _seed_trusted()

    def test_same_repo_untrusted_author_holds_on_public_repo(self) -> None:
        # Medium finding: same-repo is NOT trusted unconditionally on a PUBLIC repo —
        # a push-access account not in the trust set (an added collaborator, a
        # compromised token) still holds for human approval.
        with _public():
            result = classify_pr_provenance(_PUBLIC, "app/github-actions", same_repo=True)
        assert result.untrusted is True
        assert result.trusted is False

    def test_same_repo_trusted_author_is_trusted(self) -> None:
        with _public():
            result = classify_pr_provenance(_PUBLIC, "souliane", same_repo=True)
        assert result.trusted is True
        assert result.untrusted is False

    def test_same_repo_on_internal_repo_is_trusted(self) -> None:
        # On a private/internal repo the user owns access control — same-repo trusts
        # any author (the internal-repo branch of classify_author).
        with patch.object(author_trust, "repo_is_internal", return_value=True):
            result = classify_pr_provenance(_PUBLIC, "app/github-actions", same_repo=True)
        assert result.trusted is True
        assert result.internal_repo is True

    def test_fork_holds_even_for_a_trusted_author(self) -> None:
        with _public():
            result = classify_pr_provenance(_PUBLIC, "souliane", same_repo=False)
        assert result.untrusted is True
        assert result.trusted is False

    def test_unknown_provenance_falls_back_to_trusted_author(self) -> None:
        with _public():
            result = classify_pr_provenance(_PUBLIC, "souliane", same_repo=None)
        assert result.trusted is True

    def test_unknown_provenance_fails_closed_on_unknown_author(self) -> None:
        with _public():
            result = classify_pr_provenance(_PUBLIC, "evilhacker", same_repo=None)
        assert result.untrusted is True
