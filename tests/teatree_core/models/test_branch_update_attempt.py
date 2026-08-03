"""Tests for :class:`BranchUpdateAttempt` — the merge-update idempotency ledger (#4063).

The claim bounds the sweep to ONE merge-update per ``(slug, pr_id, head_sha)``:
a PR that comes back red at the CURRENT base is never touched again, and a
failed update is not retried. A new commit re-arms exactly one fresh attempt.
"""

import pytest

from teatree.core.models import BranchUpdateAttempt

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"


class TestClaimOncePerHead:
    def test_first_claim_returns_a_row(self) -> None:
        row = BranchUpdateAttempt.claim(slug=SLUG, pr_id=4029, head_sha=HEAD, overlay="teatree")

        assert row is not None
        assert row.slug == SLUG
        assert row.pr_id == 4029
        assert row.head_sha == HEAD
        assert row.overlay == "teatree"

    def test_second_claim_same_head_returns_none(self) -> None:
        first = BranchUpdateAttempt.claim(slug=SLUG, pr_id=4029, head_sha=HEAD)
        second = BranchUpdateAttempt.claim(slug=SLUG, pr_id=4029, head_sha=HEAD)

        assert first is not None
        assert second is None
        assert BranchUpdateAttempt.objects.count() == 1

    def test_new_head_re_arms_exactly_one_claim(self) -> None:
        BranchUpdateAttempt.claim(slug=SLUG, pr_id=4029, head_sha=HEAD)
        re_armed = BranchUpdateAttempt.claim(slug=SLUG, pr_id=4029, head_sha=NEW_HEAD)

        assert re_armed is not None
        assert BranchUpdateAttempt.objects.count() == 2

    def test_blank_slug_or_head_does_not_claim(self) -> None:
        assert BranchUpdateAttempt.claim(slug="", pr_id=1, head_sha=HEAD) is None
        assert BranchUpdateAttempt.claim(slug=SLUG, pr_id=1, head_sha="") is None
        assert BranchUpdateAttempt.objects.count() == 0

    def test_str_renders_slug_pr_and_short_head(self) -> None:
        row = BranchUpdateAttempt.claim(slug=SLUG, pr_id=4029, head_sha=HEAD)
        assert row is not None
        assert str(row) == f"branch-update-attempt<{row.pk}:{SLUG}#4029@{HEAD[:8]}>"
