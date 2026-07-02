"""Regression test for ``core.0014_ticket_repo_namespaced_key`` (#2293).

A ``Ticket`` row created before ``repo_namespaced_key`` existed carries a
blank key even though its ``issue_url`` is a parseable GitHub/GitLab issue
URL. ``0014`` backfills the collision-free key for every existing row it
can parse, and is a no-op for rows it cannot (PR/MR-shaped, bare-number,
or non-forge ``issue_url``).
"""

import importlib

from django.apps import apps
from django.db import connection
from django.test import TestCase

from teatree.core.models import Ticket

_migration = importlib.import_module("teatree.core.migrations.0014_ticket_repo_namespaced_key")


class BackfillRepoNamespacedKeyTest(TestCase):
    def test_backfills_key_from_a_parseable_issue_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://github.com/acme-eng/widgets/issues/42")
        Ticket.objects.filter(pk=ticket.pk).update(repo_namespaced_key="")

        _migration.backfill_repo_namespaced_key(apps, connection.schema_editor())

        ticket.refresh_from_db()
        assert ticket.repo_namespaced_key == "acme-eng/widgets#42"

    def test_noop_for_a_non_issue_shaped_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="dogfood-smoke://acme")

        _migration.backfill_repo_namespaced_key(apps, connection.schema_editor())

        ticket.refresh_from_db()
        assert ticket.repo_namespaced_key == ""

    def test_noop_for_a_blank_issue_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme")

        _migration.backfill_repo_namespaced_key(apps, connection.schema_editor())

        ticket.refresh_from_db()
        assert ticket.repo_namespaced_key == ""

    def test_does_not_overwrite_an_already_set_key(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://github.com/acme-eng/widgets/issues/42")
        assert ticket.repo_namespaced_key == "acme-eng/widgets#42"
        Ticket.objects.filter(pk=ticket.pk).update(repo_namespaced_key="hand-set")

        _migration.backfill_repo_namespaced_key(apps, connection.schema_editor())

        ticket.refresh_from_db()
        assert ticket.repo_namespaced_key == "hand-set"

    def test_skips_a_would_be_duplicate_key_instead_of_raising(self) -> None:
        """The #2293 no-clobber regression.

        Two rows whose ``issue_url`` differ byte-for-byte but parse to the
        same key must never raise IntegrityError from this backfill — the
        second row is left blank.

        Both tickets are created blank and their ``issue_url`` set via a raw
        queryset ``update()`` — going through ``Ticket.objects.create(...)``
        with the forge URL directly would trip ``save()``'s own key
        computation and hit the real unique constraint before the migration
        ever runs, which is not what this test is pinning.
        """
        first = Ticket.objects.create(overlay="acme")
        second = Ticket.objects.create(overlay="acme")
        Ticket.objects.filter(pk=first.pk).update(issue_url="https://github.com/acme-eng/widgets/issues/42")
        Ticket.objects.filter(pk=second.pk).update(issue_url="https://github.com/acme-eng/widgets/issues/42/")

        _migration.backfill_repo_namespaced_key(apps, connection.schema_editor())

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.repo_namespaced_key == "acme-eng/widgets#42"
        assert second.repo_namespaced_key == ""
