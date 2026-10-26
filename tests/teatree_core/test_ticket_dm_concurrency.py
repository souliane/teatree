import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from django.test import TransactionTestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Ticket
from teatree.core.notify import NotifyKind, notify_user_outcome
from teatree.core.notify_types import NotifyOptions
from teatree.types import RawAPIDict
from teatree.utils.thread_db import close_thread_db_connections


class TestTicketFirstSendConcurrency(TransactionTestCase):
    def test_two_first_sends_open_one_header(self) -> None:
        ticket = Ticket.objects.create(short_description="Concurrent")
        backend = MagicMock()
        second_opened = threading.Event()
        first_header = threading.Event()
        calls_lock = threading.Lock()
        posted: list[tuple[str, str, str]] = []
        opens = 0

        def open_dm(_user_id: str) -> str:
            nonlocal opens
            with calls_lock:
                opens += 1
                if opens == 2:
                    second_opened.set()
            return "D-USER"

        def post_message(*, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
            if not thread_ts and not first_header.is_set():
                first_header.set()
                second_opened.wait(timeout=5)
            with calls_lock:
                ts = f"1700000000.{len(posted) + 1:06d}"
                posted.append((text, thread_ts, ts))
            return {"ok": True, "ts": ts}

        backend.open_dm.side_effect = open_dm
        backend.post_message.side_effect = post_message
        backend.fetch_thread_replies.side_effect = lambda *, channel, thread_ts: [{"ts": thread_ts}]
        backend.get_permalink.return_value = "https://slack.example/archives/D-USER/p1700000000000001"

        def send(index: int) -> bool:
            try:
                return notify_user_outcome(
                    f"note {index}",
                    kind=NotifyKind.INFO,
                    idempotency_key=f"concurrent-ticket-{index}",
                    audience=NotifyAudience.OWNER_QUESTION,
                    options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
                ).sent
            finally:
                close_thread_db_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(send, 1)
            assert first_header.wait(timeout=5)
            second = pool.submit(send, 2)
            outcomes = [first.result(timeout=10), second.result(timeout=10)]

        if not all(outcomes):
            send(3)
        assert len([entry for entry in posted if not entry[1]]) == 1
