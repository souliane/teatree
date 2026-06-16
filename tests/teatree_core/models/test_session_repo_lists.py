"""Session repo-list methods use the locked RMW pattern (#748 class).

``mark_repo_modified`` / ``mark_repo_tested`` previously did an unlocked
read-modify-write: read the JSON list from the in-memory instance, append in
Python, ``save()``.  Two concurrent appends on the same Session row would each
read the stale list and clobber the other's write.

The fix routes both methods through the same locked-RMW primitive used by
``visit_phase``: ``select_for_update()`` re-read inside ``transaction.atomic()``
then ``filter(pk=…).update(…)`` — so neither uses ``.save()`` on an in-memory
instance for the write.
"""

from unittest.mock import patch

import pytest

from teatree.core.models import Session, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestMarkRepoModifiedLockedRMW:
    def test_adds_new_repo(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())
        session.mark_repo_modified("backend")
        session.refresh_from_db()
        assert session.repos_modified == ["backend"]

    def test_does_not_duplicate(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())
        session.mark_repo_modified("backend")
        session.mark_repo_modified("backend")
        session.refresh_from_db()
        assert session.repos_modified == ["backend"]

    def test_does_not_call_save_on_instance(self) -> None:
        """Structural gate: the locked-RMW path uses filter().update(), not save().

        A ``save()`` call on the in-memory instance means the buggy
        read-modify-write is still in place; the fixed code must not call it.
        """
        session = Session.objects.create(ticket=Ticket.objects.create())
        with patch.object(Session, "save") as mock_save:
            session.mark_repo_modified("backend")
            mock_save.assert_not_called()


class TestMarkRepoTestedLockedRMW:
    def test_adds_new_repo(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())
        session.mark_repo_tested("backend")
        session.refresh_from_db()
        assert session.repos_tested == ["backend"]

    def test_does_not_duplicate(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())
        session.mark_repo_tested("backend")
        session.mark_repo_tested("backend")
        session.refresh_from_db()
        assert session.repos_tested == ["backend"]

    def test_does_not_call_save_on_instance(self) -> None:
        """Structural gate: the locked-RMW path uses filter().update(), not save()."""
        session = Session.objects.create(ticket=Ticket.objects.create())
        with patch.object(Session, "save") as mock_save:
            session.mark_repo_tested("backend")
            mock_save.assert_not_called()
