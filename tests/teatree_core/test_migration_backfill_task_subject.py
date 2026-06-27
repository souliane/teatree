"""Regression test for ``core.0005_backfill_task_subject``.

A Task created before the ``subject`` field existed has a blank subject; the
statusline then fell back to the bare phase token (the unreadable
``Task NN (short_describe)`` the operator reported). ``0005`` backfills a
human-readable subject derived from the work item, leaving any explicit
subject untouched.
"""

import importlib

from django.apps import apps
from django.db import connection
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket

_migration = importlib.import_module("teatree.core.migrations.0005_backfill_task_subject")


class BackfillTaskSubjectTest(TestCase):
    def test_blank_subject_backfilled_from_ticket_title(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/issues/77",
            extra={"issue_title": "Export pipeline rework"},
        )
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="short_describe", subject="")

        _migration.backfill_subject(apps, connection.schema_editor())

        task.refresh_from_db()
        assert task.subject == "#77 Export pipeline rework"

    def test_falls_back_to_phase_when_no_title(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="dogfood-smoke://acme")
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="dogfood_smoke", subject="")

        _migration.backfill_subject(apps, connection.schema_editor())

        task.refresh_from_db()
        assert task.subject == f"#{ticket.pk} dogfood_smoke"

    def test_does_not_overwrite_an_explicit_subject(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/5")
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", subject="Hand-written")

        _migration.backfill_subject(apps, connection.schema_editor())

        task.refresh_from_db()
        assert task.subject == "Hand-written"
