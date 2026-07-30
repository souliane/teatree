"""``t3 <overlay> retention prune`` — the operator surface for #3693.

Integration-first via ``call_command`` against the real DB: the default is a dry
run that deletes nothing and reports the plan; ``--apply`` deletes only the
terminal-owned rows past the window. ``--json`` round-trips the machine payload.

``--apply`` also VACUUMs (#3852) — deleting rows on SQLite reclaims no disk on its
own, so a prune that only drops rows leaves the file exactly as large. The vacuum
itself is exercised against a real file in ``tests/teatree_utils/django_db/test_vacuum.py``;
here it is stubbed so the wiring (called on apply, never on a dry run, reported
either way) is what is under test.
"""

import datetime as dt
import json
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.management.commands.retention import Command, RetentionReport
from teatree.core.models import IncomingEvent, Session, Task, TaskAttempt, Ticket
from teatree.utils.django_db.vacuum import VacuumOutcome

_OLD = timezone.now() - dt.timedelta(days=60)
_COMMAND = "teatree.core.management.commands.retention"
_RECLAIMED = VacuumOutcome(ran=True, reason="rebuilt", bytes_before=1_200_000, bytes_after=600_000)


def _prune_json(outcome: VacuumOutcome, *args: str) -> tuple[dict[str, Any], MagicMock]:
    out = StringIO()
    with patch(f"{_COMMAND}.vacuum_control_db", return_value=outcome) as vacuum:
        call_command("retention", "prune", *args, "--json", stdout=out)
    return json.loads(out.getvalue()), vacuum


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


class RetentionCommandStructureTestCase(TestCase):
    def test_command_exposes_prune(self) -> None:
        assert callable(Command.prune)

    def test_report_payload_keys(self) -> None:
        report: RetentionReport = {
            "applied": False,
            "total_rows": 0,
            "tables": [],
            "vacuum": {"ran": False, "reason": "dry run", "bytes_reclaimed": 0},
        }
        assert set(report) == {"applied", "total_rows", "tables", "vacuum"}


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


class RetentionPruneVacuumTestCase(TestCase):
    """Rows are only half a prune — the freed pages must reach the filesystem (#3852)."""

    def test_apply_vacuums_and_reports_the_reclaimed_bytes(self) -> None:
        payload, vacuum = _prune_json(_RECLAIMED, "--apply")

        vacuum.assert_called_once_with()
        assert payload["vacuum"] == {"ran": True, "reason": "rebuilt", "bytes_reclaimed": 600_000}

    def test_dry_run_never_vacuums(self) -> None:
        """VACUUM rewrites the whole file — a preview that mutates 1.12 GB is not a preview."""
        payload, vacuum = _prune_json(_RECLAIMED)

        vacuum.assert_not_called()
        assert payload["vacuum"]["ran"] is False

    def test_a_vacuum_that_could_not_run_reports_its_reason(self) -> None:
        """A stated reason is what separates "nothing to reclaim" from "never happened"."""
        blocked = VacuumOutcome(ran=False, reason="postgresql is not SQLite — VACUUM is not applicable")

        payload, _ = _prune_json(blocked, "--apply")

        assert payload["vacuum"]["ran"] is False
        assert "not applicable" in payload["vacuum"]["reason"]
        assert payload["vacuum"]["bytes_reclaimed"] == 0
