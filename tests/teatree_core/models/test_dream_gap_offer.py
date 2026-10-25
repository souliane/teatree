from datetime import UTC, datetime

from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.dream_gap_ledger import pending_entries, queue_pending
from teatree.core.models.dream_gap_offer import mark_offered


class TestMarkOffered(TestCase):
    def test_stamps_only_selected_pending_gaps(self) -> None:
        umbrella = Ticket.objects.create(issue_url="https://github.com/o/r/issues/249")
        queue_pending(umbrella, [{"gap_key": "a"}, {"gap_key": "b"}])

        mark_offered(umbrella, {"b"}, at=datetime(2026, 10, 25, tzinfo=UTC))

        umbrella.refresh_from_db()
        assert pending_entries(umbrella) == [
            {"gap_key": "a"},
            {"gap_key": "b", "offered_at": "2026-10-25T00:00:00+00:00"},
        ]
