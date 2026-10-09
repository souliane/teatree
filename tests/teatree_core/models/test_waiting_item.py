"""Tests for the manual-entry :class:`WaitingItem` model (PR-21, M7)."""

import pytest

from teatree.core.models.waiting_item import WaitingItem, WaitingItemError


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestWaitingItemManager:
    def test_add_creates_open_row(self) -> None:
        item = WaitingItem.objects.add("chase the finance sign-off")
        assert item.pk is not None
        assert item.resolved_at is None
        assert item.is_open

    def test_add_refuses_empty_text(self) -> None:
        with pytest.raises(WaitingItemError):
            WaitingItem.objects.add("   ")

    def test_add_strips_text(self) -> None:
        item = WaitingItem.objects.add("  reply to the vendor  ")
        assert item.text == "reply to the vendor"

    def test_open_excludes_resolved(self) -> None:
        kept = WaitingItem.objects.add("still open")
        gone = WaitingItem.objects.add("about to resolve")
        WaitingItem.objects.resolve(gone.pk)
        open_pks = {row.pk for row in WaitingItem.objects.open()}
        assert kept.pk in open_pks
        assert gone.pk not in open_pks

    def test_resolve_is_single_use(self) -> None:
        item = WaitingItem.objects.add("resolve me once")
        assert WaitingItem.objects.resolve(item.pk) is True
        assert WaitingItem.objects.resolve(item.pk) is False

    def test_resolve_absent_returns_false(self) -> None:
        assert WaitingItem.objects.resolve(999_999) is False
