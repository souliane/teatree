"""Tests for :class:`MergeableNotified` — the mergeable-DM idempotency ledger.

The ledger fires the "mergeable, ready to request review" DM ONCE per
``(slug, pr_id, head_sha)`` and re-fires only on a new commit (new head).
Mirrors :class:`teatree.core.models.ScannedFailedE2E` (insert-once
``record`` keyed on a unique constraint).
"""

import pytest

from teatree.core.models import MergeableNotified

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"


class TestRecordOncePerHead:
    def test_first_record_returns_a_row(self) -> None:
        row = MergeableNotified.record(slug=SLUG, pr_id=6230, head_sha=HEAD, overlay="teatree")

        assert row is not None
        assert row.slug == SLUG
        assert row.pr_id == 6230
        assert row.head_sha == HEAD
        assert row.overlay == "teatree"

    def test_second_record_same_head_returns_none(self) -> None:
        first = MergeableNotified.record(slug=SLUG, pr_id=6230, head_sha=HEAD, overlay="teatree")
        second = MergeableNotified.record(slug=SLUG, pr_id=6230, head_sha=HEAD, overlay="teatree")

        assert first is not None
        assert second is None
        assert MergeableNotified.objects.count() == 1

    def test_new_head_refires_exactly_one_new_row(self) -> None:
        MergeableNotified.record(slug=SLUG, pr_id=6230, head_sha=HEAD, overlay="teatree")
        refired = MergeableNotified.record(slug=SLUG, pr_id=6230, head_sha=NEW_HEAD, overlay="teatree")

        assert refired is not None
        assert MergeableNotified.objects.count() == 2

    def test_blank_slug_or_head_does_not_record(self) -> None:
        assert MergeableNotified.record(slug="", pr_id=1, head_sha=HEAD) is None
        assert MergeableNotified.record(slug=SLUG, pr_id=1, head_sha="") is None
        assert MergeableNotified.objects.count() == 0

    def test_str_renders_slug_pr_and_short_head(self) -> None:
        row = MergeableNotified.record(slug=SLUG, pr_id=6230, head_sha=HEAD)
        assert row is not None
        assert str(row) == f"mergeable-notified<{row.pk}:{SLUG}#6230@{HEAD[:8]}>"
