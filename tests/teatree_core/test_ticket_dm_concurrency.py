import threading
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.db import connections

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Ticket, TicketDmThread
from teatree.core.notify import NotifyKind, notify_user_outcome
from teatree.core.notify_types import NotifyOptions
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS
from teatree.types import RawAPIDict
from tests.db_alias import register_migrated_sqlite_alias, run_racing_threads, teardown_sqlite_alias, wal_control_db

pytestmark = pytest.mark.usefixtures(wal_control_db.__name__)


@contextmanager
def _default_on(db_file: Path) -> Generator[None]:
    wrapper = connections["default"]
    original_name = wrapper.settings_dict["NAME"]
    original_options = wrapper.settings_dict["OPTIONS"]
    original_mode = wrapper.transaction_mode
    original_raw = wrapper.connection
    wrapper.connection = None
    wrapper.settings_dict["NAME"] = str(db_file)
    wrapper.settings_dict["OPTIONS"] = dict(SQLITE_WRITE_SERIALIZATION_OPTIONS)
    wrapper.transaction_mode = "IMMEDIATE"
    try:
        yield
    finally:
        if wrapper.connection is not None:
            wrapper.connection.close()
        wrapper.connection = original_raw
        wrapper.settings_dict["NAME"] = original_name
        wrapper.settings_dict["OPTIONS"] = original_options
        wrapper.transaction_mode = original_mode


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
@pytest.mark.timeout(300)
class TestTicketFirstSendConcurrency:
    def test_two_first_sends_open_one_card_root(self, tmp_path: Path, migrated_db_template: Path | None) -> None:
        alias = f"ticketdm_{uuid.uuid4().hex}"
        db_file = tmp_path / f"{alias}.sqlite3"
        register_migrated_sqlite_alias(alias, db_file, migrated_db_template, options=SQLITE_WRITE_SERIALIZATION_OPTIONS)
        try:
            ticket = Ticket.objects.using(alias).create(short_description="Concurrent")
            connections[alias].close()
            backend = MagicMock()
            opened = threading.Barrier(2)
            second_header = threading.Event()
            calls_lock = threading.Lock()
            posted: list[tuple[str, str, str]] = []

            def open_dm(_user_id: str) -> str:
                opened.wait(timeout=10)
                return "D-USER"

            def post_message(*, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
                with calls_lock:
                    ts = f"1700000000.{len(posted) + 1:06d}"
                    posted.append((text, thread_ts, ts))
                    header_count = sum(not entry[1] for entry in posted)
                    if not thread_ts and header_count == 2:
                        second_header.set()
                if not thread_ts and header_count == 1:
                    second_header.wait(timeout=1)
                return {"ok": True, "ts": ts}

            backend.open_dm.side_effect = open_dm
            backend.post_message.side_effect = post_message
            backend.fetch_thread_replies.side_effect = lambda *, channel, thread_ts: [{"ts": thread_ts}]
            backend.get_permalink.return_value = "https://slack.example/archives/D-USER/p1700000000000001"

            def send(index: int) -> bool:
                return notify_user_outcome(
                    f"note {index}",
                    kind=NotifyKind.INFO,
                    idempotency_key=f"concurrent-ticket-{index}",
                    audience=NotifyAudience.OWNER_QUESTION,
                    options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
                ).sent

            with _default_on(db_file):
                outcomes = run_racing_threads(send, 2, timeout=30)
            thread = TicketDmThread.objects.using(alias).get(ticket=ticket)
            roots = [entry for entry in posted if not entry[1]]

            assert outcomes == [True, True]
            assert len(posted) == 2
            assert len(roots) == 1
            assert "note " in roots[0][0]
            assert thread.root_ts == roots[0][2]
            assert [entry[1] for entry in posted if entry[1]] == [thread.root_ts]
        finally:
            teardown_sqlite_alias(alias)
