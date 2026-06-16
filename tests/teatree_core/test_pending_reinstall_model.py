"""Tests for :class:`teatree.core.models.pending_reinstall.PendingReinstall` (#1760)."""

import pytest
from django.test import TestCase

from teatree.core.models.pending_reinstall import PendingReinstall

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestPendingReinstallManager(TestCase):
    def test_upsert_pending_creates_a_pending_row(self) -> None:
        row = PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="abc123")

        assert row.state == PendingReinstall.State.PENDING
        assert row.target_sha == "abc123"
        assert row.attempts == 0

    def test_upsert_pending_resets_an_existing_row_to_pending(self) -> None:
        row = PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="old")
        row.mark_failed("boom")

        reset = PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="new")

        assert reset.pk == row.pk  # one row per repo_label (unique key)
        assert reset.state == PendingReinstall.State.PENDING
        assert reset.target_sha == "new"
        assert reset.attempts == 0
        assert reset.last_error == ""

    def test_pending_orders_oldest_first_and_excludes_terminal(self) -> None:
        PendingReinstall.objects.create(repo_label="a", state=PendingReinstall.State.DONE)
        b = PendingReinstall.objects.upsert_pending(repo_label="b", target_sha="b")
        c = PendingReinstall.objects.upsert_pending(repo_label="c", target_sha="c")

        assert list(PendingReinstall.objects.pending()) == [b, c]


class TestPendingReinstallTransitions(TestCase):
    def setUp(self) -> None:
        self.row = PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="abc")

    def test_mark_done(self) -> None:
        self.row.mark_done()

        self.row.refresh_from_db()
        assert self.row.state == PendingReinstall.State.DONE
        assert self.row.attempts == 1
        assert self.row.last_error == ""

    def test_mark_failed_records_and_truncates_error(self) -> None:
        self.row.mark_failed("x" * 300)

        self.row.refresh_from_db()
        assert self.row.state == PendingReinstall.State.FAILED
        assert self.row.attempts == 1
        assert len(self.row.last_error) == 200

    def test_str_is_descriptive(self) -> None:
        assert str(self.row) == "pending-reinstall<teatree:pending@abc>"
