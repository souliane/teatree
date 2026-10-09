"""A live DB failure is not an un-migrated install.

``IncomingEventsScanner`` swallows the queue read so a pre-migration install
does not spam a per-tick traceback. Catching the whole ``OperationalError`` /
``ProgrammingError`` classes swallowed a lock timeout and a dropped connection
too — the tick reported a clean empty scan while the queue was unreadable,
which is exactly the silent-empty the contract in its own comment forbids.

Matching the missing-relation *wording* alone still swallowed a live failure
that merely shares it — an absent database, another table's absence — so the
scanner's own table name is what separates the two.
"""

from unittest.mock import patch

import pytest
from django.db import OperationalError, ProgrammingError
from django.test import TestCase

from teatree.core.models import IncomingEvent
from teatree.loop.scanners.incoming_events import EVENT_TABLE, IncomingEventsScanner

_QUEUE_READ = "teatree.core.managers_inbound.IncomingEventQuerySet.unprocessed"


class TestQueueReadFailureClassification(TestCase):
    """Only the missing-relation wording degrades to a silent skip."""

    def test_missing_table_is_a_silent_skip(self) -> None:
        with patch(_QUEUE_READ, side_effect=OperationalError("no such table: teatree_incoming_event")):
            assert IncomingEventsScanner().scan() == []

    def test_postgres_missing_relation_is_a_silent_skip(self) -> None:
        error = ProgrammingError('relation "teatree_incoming_event" does not exist')
        with patch(_QUEUE_READ, side_effect=error):
            assert IncomingEventsScanner().scan() == []

    def test_lock_timeout_propagates(self) -> None:
        with (
            patch(_QUEUE_READ, side_effect=OperationalError("database is locked")),
            pytest.raises(OperationalError),
        ):
            IncomingEventsScanner().scan()

    def test_absent_database_propagates(self) -> None:
        error = OperationalError('FATAL:  database "teatree" does not exist')
        with patch(_QUEUE_READ, side_effect=error), pytest.raises(OperationalError):
            IncomingEventsScanner().scan()

    def test_a_different_missing_table_propagates(self) -> None:
        error = OperationalError("no such table: django_migrations")
        with patch(_QUEUE_READ, side_effect=error), pytest.raises(OperationalError):
            IncomingEventsScanner().scan()

    def test_the_pinned_table_name_matches_the_model(self) -> None:
        assert IncomingEvent._meta.db_table == EVENT_TABLE

    def test_a_readable_queue_is_unaffected(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.CI,
            actor="alice",
            channel_ref="C-eng",
            thread_ref="thread-1",
            body="pipeline succeeded",
            payload_json={"status": "success"},
            idempotency_key="ci:probe",
        )

        assert [signal.kind for signal in IncomingEventsScanner().scan()] == ["incoming_event.recorded"]
