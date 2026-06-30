"""Regression test for ``core.0010_worktree_compose_project``.

A worktree provisioned before ``compose_project`` was stored has a blank
project; deriving it live under the new pk scheme would rename the running
docker stack out from under its containers (the #2774-deferred orphan risk).
The migration freezes each existing worktree's project at the name its stack
already runs under (``<repo_path>-wt<ticket_number>``), so the cutover never
orphans a live stack. New worktrees adopt the pk scheme at provision time.
"""

import importlib

from django.apps import apps
from django.db import connection
from django.test import TestCase

from teatree.core.models import Ticket, Worktree

_migration = importlib.import_module("teatree.core.migrations.0010_worktree_compose_project")


class BackfillComposeProjectTest(TestCase):
    def test_blank_project_backfilled_to_running_ticket_number_scheme(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/42")
        wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="42-x")

        _migration.backfill_compose_project(apps, connection.schema_editor())

        wt.refresh_from_db()
        assert wt.compose_project == "backend-wt42"

    def test_does_not_overwrite_an_explicit_project(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/42")
        wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="42-x", compose_project="custom-name")

        _migration.backfill_compose_project(apps, connection.schema_editor())

        wt.refresh_from_db()
        assert wt.compose_project == "custom-name"

    def test_no_trailing_number_falls_back_to_pk(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="dogfood-smoke://acme")
        wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="x")

        _migration.backfill_compose_project(apps, connection.schema_editor())

        wt.refresh_from_db()
        assert wt.compose_project == f"backend-wt{ticket.pk}"
