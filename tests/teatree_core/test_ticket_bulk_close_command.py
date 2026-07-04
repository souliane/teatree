"""``t3 <overlay> ticket bulk-close`` — the PR-08 no-bulk-close chokepoint CLI.

A batch over ``bulk_close_threshold`` (default 5) tickets is refused unless every
id is echoed in ``--confirm``; a batch at or under the threshold closes without
confirmation. Closing = the ``ignore`` FSM transition.
"""

from contextlib import AbstractContextManager
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.models import Ticket


def _tickets(n: int) -> list[Ticket]:
    return [Ticket.objects.create(overlay="test", state=Ticket.State.CODED) for _ in range(n)]


def _threshold(value: int) -> AbstractContextManager[object]:
    return patch(
        "teatree.core.gates.bulk_close_gate.get_effective_settings",
        return_value=UserSettings(bulk_close_threshold=value),
    )


class TicketBulkCloseTest(TestCase):
    def test_batch_at_threshold_closes_without_confirmation(self) -> None:
        tickets = _tickets(2)
        ids = ",".join(str(t.pk) for t in tickets)
        with _threshold(2):
            result = cast("dict[str, object]", call_command("ticket", "bulk-close", "--ids", ids))
        assert result["closed"] is True
        for t in tickets:
            t.refresh_from_db()
            assert t.state == Ticket.State.IGNORED

    def test_batch_above_threshold_refused_without_tokens(self) -> None:
        tickets = _tickets(3)
        ids = ",".join(str(t.pk) for t in tickets)
        with _threshold(2):
            result = cast("dict[str, object]", call_command("ticket", "bulk-close", "--ids", ids))
        assert result["refused"] is True
        # Nothing was closed.
        for t in tickets:
            t.refresh_from_db()
            assert t.state == Ticket.State.CODED

    def test_batch_above_threshold_closes_with_all_tokens(self) -> None:
        tickets = _tickets(3)
        ids = ",".join(str(t.pk) for t in tickets)
        with _threshold(2):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "bulk-close", "--ids", ids, "--confirm", ids),
            )
        assert result["closed"] is True
        assert len(result["closed_ids"]) == 3
        for t in tickets:
            t.refresh_from_db()
            assert t.state == Ticket.State.IGNORED

    def test_missing_ids_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "bulk-close")

    def test_uncloseable_ticket_refused_cleanly_and_rolls_back(self) -> None:
        # A ticket already in a terminal state (IGNORED) cannot transition to
        # IGNORED again — the FSM raises TransitionNotAllowed. bulk-close must
        # surface that as a clean refusal (not a traceback), and the atomic
        # block must roll back so no sibling ticket in the batch is closed.
        closeable = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)
        already_closed = Ticket.objects.create(overlay="test", state=Ticket.State.IGNORED)
        ids = f"{closeable.pk},{already_closed.pk}"
        with _threshold(5):
            result = cast("dict[str, object]", call_command("ticket", "bulk-close", "--ids", ids))
        assert result["refused"] is True
        assert "cannot be closed" in cast("str", result["reason"])
        closeable.refresh_from_db()
        assert closeable.state == Ticket.State.CODED  # rolled back, not closed


class TicketIntegrationReviewOverrideTest(TestCase):
    def test_records_override_reason(self) -> None:
        ticket = Ticket.objects.create(overlay="test", repos=["org/a", "org/b"])
        result = cast(
            "dict[str, object]",
            call_command("ticket", "integration-review-override", str(ticket.pk), "--reason", "coordinated hotfix"),
        )
        ticket.refresh_from_db()
        assert ticket.extra["integration_review_override"]["reason"] == "coordinated hotfix"
        assert result["ticket_id"] == int(ticket.pk)

    def test_blank_reason_exits_nonzero(self) -> None:
        ticket = Ticket.objects.create(overlay="test", repos=["org/a", "org/b"])
        with pytest.raises(SystemExit):
            call_command("ticket", "integration-review-override", str(ticket.pk), "--reason", "  ")
        ticket.refresh_from_db()
        assert "integration_review_override" not in (ticket.extra or {})
