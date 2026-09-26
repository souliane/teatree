"""The ``0119`` state rename writes through the schema editor's connection, never ``default``.

A ``RunPython`` body that queries an unscoped manager lands on whatever the router picks,
so a migrate aimed at one database would rewrite another database's live rows. The
migrated connection here is a throwaway SQLite alias; ``default`` holds a sentinel carrying
the same retired names, and it must come through both directions untouched.
"""

# test-path: cross-cutting -- the subject is teatree.core.migrations (loaded by name below), not a tests/ mirror

import importlib
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.apps import apps
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test import TestCase

from teatree.core.models import Session, Ticket, TicketTransition
from tests.db_alias import register_sqlite_alias, teardown_sqlite_alias
from tests.factories import TicketFactory, TicketTransitionFactory

_MIGRATION = importlib.import_module("teatree.core.migrations.0119_rename_ticket_fsm_states")


@dataclass(frozen=True)
class _StubSchemaEditor:
    alias: str

    @property
    def connection(self) -> BaseDatabaseWrapper:
        return connections[self.alias]


@pytest.fixture
def migrated_alias(django_db_blocker: pytest.FixtureRequest) -> Iterator[str]:
    alias = f"rename_{uuid.uuid4().hex}"
    with django_db_blocker.unblock(), tempfile.TemporaryDirectory() as tmp:
        register_sqlite_alias(alias, Path(tmp) / f"{alias}.sqlite3")
        with connections[alias].schema_editor() as editor:
            for model in (Ticket, Session, TicketTransition):
                editor.create_model(model)
        try:
            yield alias
        finally:
            teardown_sqlite_alias(alias)


def _seed(alias: str) -> None:
    Ticket.objects.using(alias).bulk_create(
        [
            TicketFactory.build(pk=1, state="started"),
            TicketFactory.build(pk=2, state="review_posted", extra={"ignored_from": "in_review"}),
            TicketFactory.build(pk=3, state="ignored", extra={"reopened_from": "retrospected", "note": "kept"}),
        ]
    )
    TicketTransition.objects.using(alias).bulk_create(
        [TicketTransitionFactory.build(ticket_id=1, from_state="planned", to_state="shipped")]
    )


def _snapshot(alias: str) -> tuple[list[tuple[int, str, dict]], list[tuple[str, str]]]:
    tickets = [(t.pk, t.state, t.extra) for t in Ticket.objects.using(alias).order_by("pk")]
    edges = list(TicketTransition.objects.using(alias).values_list("from_state", "to_state"))
    return tickets, edges


class TestRenameUsesTheSchemaEditorConnection(TestCase):
    migrated_alias: str

    @pytest.fixture(autouse=True)
    def _bind_alias(self, migrated_alias: str) -> None:
        self.migrated_alias = migrated_alias

    def test_forward_renames_the_migrated_connection_and_leaves_default_alone(self) -> None:
        _seed(self.migrated_alias)
        _seed("default")
        default_before = _snapshot("default")

        _MIGRATION._rename_forward(apps, _StubSchemaEditor(self.migrated_alias))

        assert _snapshot(self.migrated_alias) == (
            [
                (1, "work_started", {}),
                (2, "review_delivered", {"ignored_from": "review_requested"}),
                (3, "ignored", {"reopened_from": "retro_recorded", "note": "kept"}),
            ],
            [("plan_recorded", "pr_opened")],
        )
        assert _snapshot("default") == default_before

    def test_reverse_restores_the_migrated_connection_and_leaves_default_alone(self) -> None:
        _seed(self.migrated_alias)
        seeded = _snapshot(self.migrated_alias)
        _seed("default")
        default_before = _snapshot("default")

        _MIGRATION._rename_forward(apps, _StubSchemaEditor(self.migrated_alias))
        _MIGRATION._rename_reverse(apps, _StubSchemaEditor(self.migrated_alias))

        assert _snapshot(self.migrated_alias) == seeded
        assert _snapshot("default") == default_before
