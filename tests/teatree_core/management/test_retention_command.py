"""``t3 <overlay> retention prune`` — the operator surface for #3693.

Integration-first via ``call_command`` against the real DB: the default is a dry
run that deletes nothing and reports the plan; ``--apply`` deletes only the
terminal-owned rows past the window. ``--json`` round-trips the machine payload.
"""

import datetime as dt
import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import IncomingEvent, Session, Task, TaskAttempt, Ticket

_OLD = timezone.now() - dt.timedelta(days=60)


def _old_terminal_attempt() -> TaskAttempt:
    ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.MERGED)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
    attempt = TaskAttempt.objects.create(task=task)
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=_OLD)
    return attempt


def _old_processed_event(key: str) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=IncomingEvent.Source.SLACK,
        idempotency_key=key,
        received_at=_OLD,
        processed_at=timezone.now(),
    )


class RetentionPruneCommandTestCase(TestCase):
    def test_dry_run_reports_but_deletes_nothing(self) -> None:
        _old_terminal_attempt()
        _old_processed_event("k1")
        err = StringIO()
        call_command("retention", "prune", stderr=err)
        assert TaskAttempt.objects.count() == 1
        assert IncomingEvent.objects.count() == 1
        # The human view routes to stderr (machine JSON owns stdout).
        assert "dry run" in err.getvalue().lower()

    def test_apply_deletes_prunable_rows(self) -> None:
        _old_terminal_attempt()
        _old_processed_event("k1")
        call_command("retention", "prune", "--apply")
        assert TaskAttempt.objects.count() == 0
        assert IncomingEvent.objects.count() == 0

    def test_apply_spares_live_ticket_rows(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        live = TaskAttempt.objects.create(task=task)
        TaskAttempt.objects.filter(pk=live.pk).update(started_at=_OLD)
        call_command("retention", "prune", "--apply")
        assert TaskAttempt.objects.filter(pk=live.pk).exists()

    def test_json_payload_round_trips(self) -> None:
        _old_terminal_attempt()
        out = StringIO()
        call_command("retention", "prune", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        assert payload["applied"] is False
        assert payload["total_rows"] == 1
        tables = {row["table"]: row for row in payload["tables"]}
        assert tables["TaskAttempt"]["rows"] == 1
        assert tables["TaskAttempt"]["retention_days"] == 30
